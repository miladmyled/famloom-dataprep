"""
Cross-source de-duplication. Two events are the same when they are in the same city, start within
±30 minutes and their titles are similar (rapidfuzz token_set_ratio >= 85), or share a url.
The higher-priority source wins:
  Eventbrite / Meetup / submissions (1) > curated and official sites (2) > web search (4)
Applied within the run and against the events already in city_events for the city.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

from src.etl.places import city_key
from src.models.event import CityEvent

logger = logging.getLogger(__name__)

PRIORITY = {"eventbrite": 1, "meetup": 1, "submission": 1, "curated": 2, "official": 2, "web": 4}
TITLE_SIMILARITY = 85
START_TOLERANCE = timedelta(minutes=30)


def origin_of(event: CityEvent) -> str:
    if event.origin:
        return event.origin
    label = (event.source or "").lower()
    if label in ("eventbrite", "meetup"):
        return label
    return "eventbrite"  # unknown legacy sources are treated as primary


def priority(origin: str) -> int:
    return PRIORITY.get(origin, 1)


@dataclass(frozen=True)
class ExistingEvent:
    """An event already in city_events (only the persisted columns exist)."""
    url: str
    title: str
    start: Optional[datetime]
    city: str
    source: str

    @property
    def priority(self) -> int:
        label = (self.source or "").lower()
        if label in ("eventbrite", "meetup"):
            return 1
        return 2  # curated labels and anything else already published


def _same(title_a: str, start_a: Optional[datetime], title_b: str, start_b: Optional[datetime]) -> bool:
    if start_a is None or start_b is None or abs(start_a - start_b) > START_TOLERANCE:
        return False
    return fuzz.token_set_ratio(title_a.lower(), title_b.lower()) >= TITLE_SIMILARITY


def dedupe_events(
    events: List[CityEvent], existing: Optional[Iterable[ExistingEvent]] = None
) -> Tuple[List[CityEvent], Dict[str, int]]:
    """Returns (events to keep, duplicates removed per source kind)."""
    duplicates: Dict[str, int] = {}

    def drop(event: CityEvent) -> None:
        key = origin_of(event)
        duplicates[key] = duplicates.get(key, 0) + 1

    kept: List[CityEvent] = []
    seen_urls = set()
    # stable: higher-priority origins first, original order within a priority
    for event in sorted(events, key=lambda e: priority(origin_of(e))):
        url = str(event.url)
        if url in seen_urls:
            drop(event)
            continue
        key = city_key(event.city)
        if any(city_key(k.city) == key and _same(event.title, event.start_date, k.title, k.start_date) for k in kept):
            drop(event)
            continue
        seen_urls.add(url)
        kept.append(event)

    if existing:
        existing = list(existing)
        result = []
        for event in kept:
            p = priority(origin_of(event))
            key = city_key(event.city)
            clash = next(
                (x for x in existing
                 if x.url != str(event.url) and city_key(x.city) == key and x.priority <= p
                 and _same(event.title, event.start_date, x.title, x.start)),
                None,
            )
            if clash is not None and p > 1:
                drop(event)  # a same-or-better source already published it
            else:
                result.append(event)
        kept = result
    return kept, duplicates


def load_existing_events(pool, city: str, window_days: int = 15) -> List[ExistingEvent]:
    """city_events rows for the city inside the window (read once per city per run)."""
    now = datetime.now(timezone.utc)
    try:
        with pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT url, title, date, city, source FROM city_events
                    WHERE lower(split_part(city, ',', 1)) = %(key)s
                      AND date BETWEEN %(start)s AND %(end)s
                    """,
                    {"key": city_key(city), "start": now - timedelta(days=1), "end": now + timedelta(days=window_days)},
                )
                return [ExistingEvent(r["url"], r["title"], r["date"], r["city"], r["source"] or "") for r in cursor.fetchall()]
    except Exception as err:
        logger.warning(f"[DEDUPE] Could not load existing events for '{city}': {err}")
        return []
