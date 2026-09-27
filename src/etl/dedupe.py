from typing import Dict, List, Tuple

from src.models.event import CityEvent


def dedupe_events(events: List[CityEvent]) -> Tuple[List[CityEvent], Dict[str, int]]:
    """
    Cross-source de-duplication call site. Phase 1: keeps the first event per url (the database
    identity) and reports duplicates per source. Phase 2 adds fuzzy title/time matching and
    source priority.
    """
    kept: List[CityEvent] = []
    seen = set()
    duplicates: Dict[str, int] = {}
    for event in events:
        key = str(event.url)
        if key in seen:
            duplicates[event.source] = duplicates.get(event.source, 0) + 1
            continue
        seen.add(key)
        kept.append(event)
    return kept, duplicates
