"""
Facebook events from search snippets only (spec 6.4). Uses nothing but the Brave API response
(title, description, extra snippets, URL, page age). Facebook itself is never requested: this
module has no HTTP client, and the shared web client refuses facebook.com anyway.
Screened with Jev, extracted with Gemini (date, time and place required). No images, no names.
Shipped behind FACEBOOK_SNIPPETS_ENABLED=false until GATE 2b.
"""
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.etl.base import BaseEventScraper
from src.etl.places import city_timezone
from src.etl.web_search_discovery import load_queries

logger = logging.getLogger(__name__)

FB_EVENT_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)?facebook\.com/events/(\d+)", re.I)
SOURCE_LABEL = "Facebook (via search)"


def facebook_event_id(url: str) -> Optional[str]:
    match = FB_EVENT_RE.match(url or "")
    return match.group(1) if match else None


def snippet_text(result) -> str:
    parts = [result.title, result.description, *result.extra_snippets]
    if result.page_age:
        parts.append(f"(page date: {result.page_age})")
    text = "\n".join(p for p in parts if p)
    return re.sub(r"<[^>]+>", "", text)  # Brave wraps matches in <strong>


class FacebookSnippetSource(BaseEventScraper):
    source_name = "FacebookSnippets"

    def __init__(self, city: str, search=None, screener=None, extractor=None, queries: Optional[List[str]] = None, **kwargs):
        super().__init__(city, **kwargs)
        self.city_name = city.split(",")[0].strip()
        self.tz = city_timezone(city)
        self._search, self._screener, self._extractor = search, screener, extractor
        self.queries = queries if queries is not None else load_queries("facebook_snippets")
        self.metrics: Dict[str, int] = {}

    def _count(self, key: str, n: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + n

    @property
    def search(self):
        if self._search is None:
            from src.net.brave import BraveSearchClient

            self._search = BraveSearchClient()
        return self._search

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

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        from src.classify.screen import SNIPPET_IS_EVENT
        from src.net.brave import BudgetExhausted

        if self.screener is None:
            logger.warning("[FBSNIP] No Jev screener; Facebook snippets skipped.")
            return []
        raw, seen = [], set()
        for template in self.queries:
            try:
                results = self.search.search(template.format(city=self.city_name), freshness="pm")
            except BudgetExhausted:
                break
            except Exception as err:
                logger.warning(f"[FBSNIP] Search failed: {err}")
                continue
            self._count("queries")
            for result in results:
                fb_id = facebook_event_id(result.url)
                if not fb_id or fb_id in seen:
                    continue
                seen.add(fb_id)
                self._count("results")
                text = snippet_text(result)
                if not self.screener.passes(text, SNIPPET_IS_EVENT):
                    continue
                self._count("screened_in")
                for event in self.extractor.extract(text, datetime.now(self.tz), self.city, self.tz, source_url=None):
                    if event["start_date"].astimezone(self.tz).strftime("%H:%M") == "00:00":
                        self._count("skipped_no_time")  # date, time and place are all required
                        continue
                    self._count("extracted")
                    raw.append(dict(event, _fb_id=fb_id, _fb_url=f"https://www.facebook.com/events/{fb_id}/"))
        logger.info(f"[FBSNIP] '{self.city}': {self.metrics}")
        return raw

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out, per_id = [], {}
        for item in raw_events:
            n = per_id.get(item["_fb_id"], 0)
            per_id[item["_fb_id"]] = n + 1
            suffix = f"_{n}" if n else ""
            out.append({
                "event_id": f"fbsnip_{item['_fb_id']}{suffix}",
                "city": self.city,
                "title": item["title"],
                "source": SOURCE_LABEL,
                "url": item["_fb_url"] + (f"#fl-{n}" if n else ""),
                "start_date": item["start_date"],
                "end_date": item.get("end_date"),
                "description": item.get("description"),  # the extractor's own neutral summary
                "location_summary": item.get("location_summary"),
                "status": "live",
                "is_canceled": False,
                "pictureurl": None,
                "origin": "facebook_snippet",
            })
        return out


def facebook_snippets_enabled() -> bool:
    return os.getenv("FACEBOOK_SNIPPETS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def fallback_event_id(url: str) -> str:
    return "fbsnip_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
