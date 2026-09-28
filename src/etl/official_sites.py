"""
Official-site discovery (no search engine): the city's and its venues' official websites come
from Wikidata (CC0), each site's events page is found on the site itself, and every page goes
through the same automatic checks, terms check and AI approval as other discovery. Approved pages
are remembered in event_source_sites (via = "wikidata"); nothing stored comes from a search API.
"""
import logging
import os
import re
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from src.etl.web_extract import soup_of
from src.etl.web_search_discovery import DiscoveryBase, domain_matches, registrable_domain

logger = logging.getLogger(__name__)

EVENTS_TEXT_RE = re.compile(r"^(upcoming\s+)?(events?|calendar|event calendar|what'?s on|whats on|programs?( & events)?|happenings|activities)$", re.I)
EVENTS_HREF_RE = re.compile(r"/(events?|calendar|whats-on|what-s-on|programs?|happenings)(/|$|\?)", re.I)
COMMON_PATHS = ("/events", "/calendar", "/whats-on")
PAGES_PER_SITE = 2


def find_event_pages(html: str, site_url: str, limit: int = PAGES_PER_SITE) -> List[str]:
    """Links on a site's home page that lead to its events/calendar page (same site only)."""
    site = registrable_domain(site_url)
    scored = []
    for a in soup_of(html).find_all("a", href=True):
        href = urljoin(site_url, a["href"].strip())
        if not href.startswith(("http://", "https://")) or not registrable_domain(href).endswith(site):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        path = urlsplit(href).path or "/"
        score = (3 if EVENTS_TEXT_RE.match(text) else 0) + (2 if EVENTS_HREF_RE.search(path + "/") else 0)
        if score:
            scored.append((score, -len(path), href.split("#")[0]))
    pages = []
    for _, _, href in sorted(scored, reverse=True):
        if href not in pages:
            pages.append(href)
        if len(pages) >= limit:
            break
    return pages


class OfficialSitesDiscoverySource(DiscoveryBase):
    source_name = "Official"
    via = "wikidata"
    remember_sites = True
    origin_kind = "official"
    id_prefix = "site"

    def __init__(self, city: str, wikidata=None, max_sites: Optional[int] = None, **kwargs):
        super().__init__(city, **kwargs)
        self._wikidata = wikidata
        self.max_sites = int(max_sites if max_sites is not None else os.getenv("OFFICIAL_SITES_MAX_PER_CITY", "30"))

    @property
    def wikidata(self):
        if self._wikidata is None:
            from src.net.wikidata import WikidataClient

            self._wikidata = WikidataClient()
        return self._wikidata

    def _candidates(self) -> Iterator[Tuple[str, Optional[str]]]:
        try:
            venues = self.wikidata.official_sites(self.city)
        except Exception as err:
            logger.warning(f"[OFFICIAL] Wikidata lookup failed for '{self.city}': {err}")
            return
        self.metrics["wikidata_sites"] += len(venues)
        for venue in venues[: self.max_sites]:
            website = venue.website if venue.website.startswith("http") else f"https://{venue.website}"
            website = website.replace("http://", "https://", 1)
            domain = registrable_domain(website)
            if domain in self._seen_domains:
                self.metrics["skipped_remembered"] += 1
                continue
            if self._skip_reason(website):
                self.metrics[self._skip_reason(website)] += 1
                continue
            self._seen_domains.add(domain)
            for page in self._event_pages(website):
                yield page, venue.label

    def _event_pages(self, website: str) -> List[str]:
        try:
            home = self.http.get(website)
        except Exception:
            self.metrics["homepage_unavailable"] += 1
            return []
        if home.status != 200:
            self.metrics["homepage_unavailable"] += 1
            return []
        pages = find_event_pages(home.text, home.url)
        if pages:
            return pages
        for path in COMMON_PATHS:  # no link found: try the usual addresses, stop at the first that exists
            url = urljoin(home.url, path)
            try:
                page = self.http.get(url)
            except Exception:
                continue
            if page.status == 200:
                return [page.url]
        self.metrics["no_events_page"] += 1
        return []


def official_sites_enabled() -> bool:
    return os.getenv("OFFICIAL_SITES_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
