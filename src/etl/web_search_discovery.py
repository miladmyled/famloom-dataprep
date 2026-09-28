"""
Web discovery (decisions 9 and 14): pages found through Brave Search are used only if they pass
every check below; anything else is rejected on the spot and nothing is stored about it.

  1. skip rules      Facebook/Instagram, curated domains (enabled = read by the curated source,
                     disabled = blocked), config/sources/blocked_domains.yaml, page budgets
  2. automatic       https only, robots.txt allows us, HTTP 200 without login redirect or password
                     form, no noindex/noai robots meta, size cap
  3. terms of use    the site's terms page (if linked) must not forbid automated access or
                     republishing (Jev)
  4. AI approval     Jev: lists dated events near the city, genuine organizer/venue, family relevant
                     (all >= WEB_APPROVAL_THRESHOLD)
Approved pages give events (schema.org JSON-LD, else Gemini extraction) and are remembered per
city in event_source_sites, so later runs read them directly without searching again. A
remembered page that fails the automatic checks 3 runs in a row becomes inactive.
"""
import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import yaml

from src.etl.base import BaseEventScraper
from src.etl.curated_calendars import load_calendars, parse_jsonld, sha16
from src.etl.places import city_timezone
from src.etl.web_extract import find_terms_link, has_password_form, main_text, robots_meta, site_name

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "sources"
MAX_PAGES_PER_DOMAIN = 3
FAILURES_BEFORE_INACTIVE = 3

TERMS_QUESTION = (
    "These terms of use forbid automated access to the website (bots, scraping, crawling or automated "
    "collection), or forbid copying or republishing its content without permission."
)


def approval_questions(city: str) -> Dict[str, str]:
    return {
        "lists_events": f"This page lists one or more specific upcoming events with dates in or near {city}.",
        "real_organizer": (
            "The page is published by a genuine organizer or venue (library, city, school, community "
            "organization, or a venue or business hosting its own event), not an advertisement, spam, a "
            "copied listing aggregator or a ticket-resale page."
        ),
        "family_relevant": (
            "At least some events on this page are ones a family could attend together: parents with "
            "children, or a couple on a leisure outing."
        ),
    }


def registrable_domain(url_or_host: str) -> str:
    host = (urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host) or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def load_blocked_domains() -> set:
    path = CONFIG_DIR / "blocked_domains.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    return {registrable_domain(str(d)) for d in (data or [])}


def load_queries(section: str) -> List[str]:
    path = CONFIG_DIR / "search_queries.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return list((data or {}).get(section) or [])


def domain_matches(host: str, domains: set) -> bool:
    host = registrable_domain(host)
    return any(host == d or host.endswith("." + d) for d in domains)


class SiteStore:
    """event_source_sites access; disables itself if the table is missing (migration not applied)."""

    def __init__(self, pool):
        self.pool = pool
        self.enabled = pool is not None

    def _run(self, sql: str, params: Dict[str, Any], fetch: bool = False):
        if not self.enabled:
            return [] if fetch else None
        from psycopg import errors as pg_errors

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    return cursor.fetchall() if fetch else None
        except (pg_errors.UndefinedTable, pg_errors.InsufficientPrivilege) as err:
            logger.warning(f"[WEB] event_source_sites unavailable ({type(err).__name__}); discovered sites are not remembered.")
            self.enabled = False
        except Exception as err:
            logger.warning(f"[WEB] event_source_sites query failed: {err}")
        return [] if fetch else None

    def active_sites(self, city: str) -> List[Dict[str, Any]]:
        rows = self._run(
            "SELECT id, url, domain, source_label, kind FROM event_source_sites WHERE city = %(city)s AND status = 'active' ORDER BY id",
            {"city": city}, fetch=True,
        )
        return [dict(r) for r in rows or []]

    def remember(self, city: str, url: str, label: Optional[str], kind: str, scores: Dict[str, float], terms_url: Optional[str]) -> None:
        self._run(
            """
            INSERT INTO event_source_sites (city, url, domain, source_label, kind, approval_scores, terms_url, last_success_at)
            VALUES (%(city)s, %(url)s, %(domain)s, %(label)s, %(kind)s, %(scores)s::jsonb, %(terms)s, NOW())
            ON CONFLICT (city, url) DO UPDATE SET
                status = 'active', source_label = EXCLUDED.source_label, kind = EXCLUDED.kind,
                approval_scores = EXCLUDED.approval_scores, terms_url = EXCLUDED.terms_url,
                last_checked_at = NOW(), last_success_at = NOW(), consecutive_failures = 0
            """,
            {"city": city, "url": url, "domain": registrable_domain(url), "label": label, "kind": kind,
             "scores": json.dumps(scores), "terms": terms_url},
        )

    def record_success(self, site_id: int) -> None:
        self._run(
            "UPDATE event_source_sites SET last_checked_at = NOW(), last_success_at = NOW(), consecutive_failures = 0 WHERE id = %(id)s",
            {"id": site_id},
        )

    def record_failure(self, site_id: int, reason: str) -> None:
        self._run(
            f"""
            UPDATE event_source_sites SET last_checked_at = NOW(), consecutive_failures = consecutive_failures + 1,
                notes = %(reason)s,
                status = CASE WHEN consecutive_failures + 1 >= {FAILURES_BEFORE_INACTIVE} THEN 'inactive' ELSE status END
            WHERE id = %(id)s
            """,
            {"id": site_id, "reason": reason[:300]},
        )


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class WebSearchDiscoverySource(BaseEventScraper):
    source_name = "Web"

    def __init__(self, city: str, search=None, http=None, screener=None, extractor=None, store: Optional[SiteStore] = None,
                 queries: Optional[List[str]] = None, blocked: Optional[set] = None, calendars=None, **kwargs):
        super().__init__(city, **kwargs)
        self.city_name = city.split(",")[0].strip()
        self.tz = city_timezone(city)
        self._search, self._http, self._screener, self._extractor = search, http, screener, extractor
        self.store = store if store is not None else SiteStore(None)
        self.queries = queries if queries is not None else load_queries("discovery")
        cal = calendars if calendars is not None else load_calendars()
        self.curated_enabled = {registrable_domain(c["url"]) for c in cal if c.get("enabled")}
        self.curated_blocked = {registrable_domain(c["url"]) for c in cal if not c.get("enabled")}
        self.blocked = (blocked if blocked is not None else load_blocked_domains()) | self.curated_blocked
        self.max_pages = int(os.getenv("WEB_MAX_PAGES_PER_RUN", "150"))
        self.approval_threshold = float(os.getenv("WEB_APPROVAL_THRESHOLD", "0.75"))
        self.metrics: Counter = Counter()
        self._pages_per_domain: Counter = Counter()

    # ---- lazily built dependencies ----------------------------------------------------------

    @property
    def search(self):
        if self._search is None:
            from src.net.brave import BraveSearchClient

            self._search = BraveSearchClient()
        return self._search

    @property
    def http(self):
        if self._http is None:
            from src.net.http import PoliteHttpClient

            self._http = PoliteHttpClient()
        return self._http

    @property
    def screener(self):
        if self._screener is None:
            from src.classify.screen import get_screener

            self._screener = get_screener() or False
        return self._screener or None

    @property
    def extractor(self):
        if self._extractor is None:
            from src.classify.gemini import GeminiExtractor

            self._extractor = GeminiExtractor()
        return self._extractor

    # ---- pipeline ---------------------------------------------------------------------------

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        if self.screener is None:
            logger.warning("[WEB] No Jev screener: AI approval impossible, web discovery skipped.")
            return []
        raw: List[Dict[str, Any]] = []
        remembered = self.store.active_sites(self.city)
        seen_urls = {s["url"] for s in remembered}
        for site in remembered:
            raw.extend(self._read_remembered(site))

        from src.net.brave import BudgetExhausted

        for template in self.queries:
            try:
                results = self.search.search(template.format(city=self.city_name))
            except BudgetExhausted:
                logger.info("[WEB] Brave query budget reached for this run.")
                break
            except Exception as err:
                logger.warning(f"[WEB] Search failed for '{template}': {err}")
                continue
            self.metrics["queries"] += 1
            for result in results:
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                if self.metrics["pages_checked"] >= self.max_pages:
                    break
                raw.extend(self._consider(result.url))
        logger.info(f"[WEB] '{self.city}': {dict(self.metrics)}")
        return raw

    def _skip_reason(self, url: str) -> Optional[str]:
        from src.net.http import is_blocked_domain

        host = urlsplit(url).hostname or ""
        if is_blocked_domain(host):
            return "skipped_social"
        if domain_matches(host, self.curated_enabled):
            return "skipped_curated"
        if domain_matches(host, self.blocked):
            return "skipped_blocked"
        if self._pages_per_domain[registrable_domain(host)] >= MAX_PAGES_PER_DOMAIN:
            return "skipped_domain_budget"
        return None

    def _consider(self, url: str) -> List[Dict[str, Any]]:
        reason = self._skip_reason(url)
        if reason:
            self.metrics[reason] += 1
            return []
        self._pages_per_domain[registrable_domain(url)] += 1
        self.metrics["pages_checked"] += 1
        try:
            page = self._automatic_checks(url)
            terms_url = self._terms_check(page)
            scores = self._ai_approval(page)
            events, kind = self._events_from(page)
        except Rejected as rej:
            self.metrics[rej.reason] += 1
            logger.debug(f"[WEB] rejected {registrable_domain(url)}: {rej.reason}")
            return []
        self.metrics["approved"] += 1
        label = site_name(page.text) or registrable_domain(page.url)
        self.store.remember(self.city, page.url, label, kind, scores, terms_url)
        return [dict(e, _page_url=page.url, _label=label) for e in events]

    def _read_remembered(self, site: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            page = self._automatic_checks(site["url"])
            events, _ = self._events_from(page, require_screen=True)
        except Rejected as rej:
            self.metrics[f"remembered_{rej.reason}"] += 1
            self.store.record_failure(site["id"], rej.reason)
            return []
        self.metrics["remembered_read"] += 1
        self.store.record_success(site["id"])
        label = site.get("source_label") or registrable_domain(site["url"])
        return [dict(e, _page_url=page.url, _label=label) for e in events]

    def _automatic_checks(self, url: str):
        from src.net.http import BlockedDomainError, ResponseTooLargeError, RobotsDisallowedError

        if not url.lower().startswith("https://"):
            raise Rejected("rejected_not_https")
        try:
            page = self.http.get(url)
        except RobotsDisallowedError:
            raise Rejected("rejected_robots")
        except BlockedDomainError:
            raise Rejected("skipped_social")
        except ResponseTooLargeError:
            raise Rejected("rejected_too_large")
        except Exception:
            raise Rejected("rejected_fetch_error")
        if page.status in (401, 402, 403):
            raise Rejected("rejected_login")
        if page.status != 200:
            raise Rejected("rejected_fetch_error")
        final = page.url.lower()
        if any(k in final for k in ("/login", "/signin", "/sign-in", "/account/", "/subscribe", "paywall")) or has_password_form(page.text):
            raise Rejected("rejected_login")
        meta = robots_meta(page.text)
        if "noindex" in meta or "noai" in meta or "none" in meta.split(","):
            raise Rejected("rejected_robots_meta")
        return page

    def _terms_check(self, page) -> Optional[str]:
        terms_url = find_terms_link(page.text, page.url)
        if not terms_url or registrable_domain(terms_url) != registrable_domain(page.url):
            return None
        try:
            terms = self.http.get(terms_url)
        except Exception:
            return None  # unreadable terms page: robots.txt already allowed us
        if terms.status != 200:
            return None
        verdict = self.screener.ask(main_text(terms.text, 20000), {"forbids": TERMS_QUESTION}).get("forbids", 0.0)
        if verdict >= 0.5:
            raise Rejected("rejected_terms")
        return terms_url

    def _ai_approval(self, page) -> Dict[str, float]:
        scores = self.screener.ask(main_text(page.text), approval_questions(self.city_name))
        if not scores:
            raise Rejected("rejected_ai_error")
        for key, reason in (("lists_events", "rejected_ai_not_events"), ("real_organizer", "rejected_ai_not_organizer"),
                            ("family_relevant", "rejected_ai_not_family")):
            if scores.get(key, 0.0) < self.approval_threshold:
                raise Rejected(reason)
        return scores

    def _events_from(self, page, require_screen: bool = False):
        events, kind = self._raw_events_from(page, require_screen)
        from src.etl.enrich import enrich_events

        return enrich_events(events, page.text, page.url, self.http), kind

    def _raw_events_from(self, page, require_screen: bool = False):
        found = parse_jsonld(page.text, page.url, self.tz)
        if found:
            return found, "jsonld"
        text = main_text(page.text)
        if require_screen:
            from src.classify.screen import PAGE_LISTS_EVENTS

            if not self.screener.passes(text, PAGE_LISTS_EVENTS):
                return [], "html"
        return self.extractor.extract(text, datetime.now(self.tz), self.city, self.tz, source_url=page.url), "html"

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for item in raw_events:
            start = item.get("start_date")
            if not start or not item.get("title"):
                continue
            page_url = item["_page_url"]
            digest = sha16(item.get("event_url") or f"{page_url}|{start.isoformat()}|{item['title']}")
            out.append({
                "event_id": f"web_{digest}",
                "city": self.city,
                "title": item["title"],
                "source": item["_label"],
                "url": item.get("event_url") or f"{page_url}#fl-{digest}",
                "start_date": start,
                "end_date": item.get("end_date"),
                "description": item.get("description"),
                "location_summary": item.get("location_summary"),
                "status": "canceled" if item.get("canceled") else "live",
                "is_canceled": bool(item.get("canceled")),
                "pictureurl": item.get("picture"),
                "origin": "web",
            })
        return out


def web_search_enabled() -> bool:
    return os.getenv("WEB_SEARCH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
