"""
Curated calendars (config/sources/curated_calendars.yaml): human-approved pages read every run.
Parsing order per page: iCal -> RSS/Atom -> schema.org Event JSON-LD -> page text, which is
screened with Jev ("lists dated events?") and only then extracted with Gemini.
"""
import hashlib
import logging
import os
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.etl.base import BaseEventScraper
from src.etl.places import city_key, city_timezone
from src.etl.web_extract import jsonld_events, main_text, normalize_jsonld_event

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "sources" / "curated_calendars.yaml"


def load_calendars(path: Path = CONFIG_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or []
    return [entry for entry in data if isinstance(entry, dict)]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "calendar"


def sha16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def stable_event_id_and_url(source_label: str, page_url: str, event_url: Optional[str], start_iso: str, title: str):
    """event_id from the event's own URL when present, else page + start + title; listing-only
    events get a unique fragment URL because city_events.url must be unique."""
    key = event_url or f"{page_url}|{start_iso}|{title}"
    digest = sha16(key)
    return f"{slug(source_label)}_{digest}", (event_url or f"{page_url}#fl-{digest}")


def _to_utc(value: Any, tz) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tz)).astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return _to_utc(parsed, tz)
    return None


def parse_ical(text: str, tz) -> List[Dict[str, Any]]:
    from icalendar import Calendar

    events = []
    for component in Calendar.from_ical(text).walk("VEVENT"):
        start = component.get("DTSTART")
        title = str(component.get("SUMMARY") or "").strip()
        if not start or not title:
            continue
        end = component.get("DTEND")
        events.append({
            "title": title[:240],
            "start_date": _to_utc(start.dt, tz),
            "end_date": _to_utc(end.dt, tz) if end else None,
            "location_summary": str(component.get("LOCATION") or "").strip() or None,
            "description": str(component.get("DESCRIPTION") or "").strip() or None,
            "event_url": str(component.get("URL") or "").strip() or None,
            "canceled": str(component.get("STATUS") or "").upper() == "CANCELLED",
        })
    return events


def parse_jsonld(html: str, page_url: str, tz) -> List[Dict[str, Any]]:
    events = []
    for node in jsonld_events(html):
        item = normalize_jsonld_event(node, page_url)
        if item:
            events.append({
                "title": item["title"],
                "start_date": _to_utc(item["start"], tz),
                "end_date": _to_utc(item["end"], tz),
                "location_summary": item["location_summary"],
                "description": item["description"],
                "event_url": item["event_url"],
                "canceled": item["canceled"],
            })
    return [e for e in events if e["start_date"]]


class CuratedCalendarSource(BaseEventScraper):
    """All enabled curated calendars whose city matches this scraper's city."""

    source_name = "Curated"

    def __init__(self, city: str, http=None, screener=None, extractor=None, calendars: Optional[List[Dict[str, Any]]] = None, **kwargs):
        super().__init__(city, **kwargs)
        self.calendars = [
            c for c in (calendars if calendars is not None else load_calendars())
            if c.get("enabled") and city_key(c.get("city", "")) == city_key(city)
        ]
        self._http, self._screener, self._extractor = http, screener, extractor
        self.tz = city_timezone(city)
        self.metrics: Dict[str, int] = {}

    # lazily built so a disabled source costs nothing
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
            try:
                from src.classify.gemini import GeminiExtractor

                self._extractor = GeminiExtractor()
            except Exception as err:
                logger.warning(f"[CURATED] Gemini extractor unavailable ({err}); HTML calendars skipped.")
                self._extractor = False
        return self._extractor or None

    def _count(self, key: str, n: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + n

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        raw: List[Dict[str, Any]] = []
        for cal in self.calendars:
            try:
                items = self._read_calendar(cal)
                self._count("pages_ok")
            except Exception as err:
                self._count("pages_failed")
                logger.warning(f"[CURATED] {cal.get('name')}: {type(err).__name__}: {err}")
                continue
            for item in items:
                item["_calendar"] = cal
            raw.extend(items)
            logger.info(f"[CURATED] {cal.get('name')}: {len(items)} event(s)")
        return raw

    def _read_calendar(self, cal: Dict[str, Any]) -> List[Dict[str, Any]]:
        url, kind = cal["url"], cal.get("kind", "html")
        page = self.http.get_rendered(url) if cal.get("render") == "js" else self.http.get(url)
        if page.status != 200:
            raise RuntimeError(f"HTTP {page.status}")
        if kind == "ical":
            return parse_ical(page.text, self.tz)
        if kind == "rss":
            import feedparser

            feed = feedparser.parse(page.text)
            text = "\n\n".join(f"{e.get('title', '')}\n{e.get('summary', '')}\n{e.get('link', '')}" for e in feed.entries)
            return self._screen_and_extract(text, url)
        found = parse_jsonld(page.text, url, self.tz)
        if found or kind == "jsonld":
            return found
        return self._screen_and_extract(main_text(page.text), url)

    def _screen_and_extract(self, text: str, page_url: str) -> List[Dict[str, Any]]:
        from src.classify.screen import PAGE_LISTS_EVENTS

        if not text.strip() or self.extractor is None:
            return []
        if self.screener is not None and not self.screener.passes(text, PAGE_LISTS_EVENTS):
            self._count("screened_out")
            return []
        self._count("extracted_pages")
        return self.extractor.extract(text, datetime.now(self.tz), self.city, self.tz, source_url=page_url)

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for item in raw_events:
            cal = item.get("_calendar") or {}
            start = item.get("start_date")
            if not start:
                continue
            label = cal.get("source_label") or cal.get("name") or "Curated calendar"
            event_id, url = stable_event_id_and_url(label, cal.get("url", ""), item.get("event_url"), start.isoformat(), item["title"])
            normalized.append({
                "event_id": event_id,
                "city": self.city,
                "title": item["title"],
                "source": label,
                "url": url,
                "start_date": start,
                "end_date": item.get("end_date"),
                "description": item.get("description"),
                "location_summary": item.get("location_summary"),
                "status": "canceled" if item.get("canceled") else "live",
                "is_canceled": bool(item.get("canceled")),
                "pictureurl": None,
                "origin": "curated",
            })
        return normalized


def curated_enabled() -> bool:
    return os.getenv("CURATED_CALENDARS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
