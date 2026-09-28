from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List

import socket

import pytest

from src.classify.models import ClassificationResult, ClassifyInput, TaxonomyValue
from src.classify.taxonomy import PrimaryLanguageMap, Taxonomy
from src.models.event import CityEvent

@pytest.fixture(autouse=True)
def _no_network_and_new_sources_off(monkeypatch):
    """Tests never touch the network (spec rule 9); new web sources are off unless a test enables them."""
    for flag in ("CURATED_CALENDARS_ENABLED", "WEB_SEARCH_ENABLED", "FACEBOOK_SNIPPETS_ENABLED", "INSTAGRAM_ENABLED"):
        monkeypatch.setenv(flag, "false")
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        raise RuntimeError(f"Network access in tests is not allowed: {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"DNS in tests is not allowed: {a[0]}")))


INTERESTS = [
    TaxonomyValue(45, "interests", "hiking", "Hiking", "hikes, nature walks"),
    TaxonomyValue(42, "interests", "reading", "Reading", "storytime"),
    TaxonomyValue(71, "interests", "nightlife", "Nightlife", None),
]
LANGUAGES = [
    TaxonomyValue(501, "languages", "en", "English"),
    TaxonomyValue(502, "languages", "fr", "French"),
    TaxonomyValue(503, "languages", "fa", "Persian (Farsi)"),
    TaxonomyValue(519, "languages", "other", "Other"),
]


def make_taxonomy() -> Taxonomy:
    return Taxonomy(
        interests=list(INTERESTS),
        languages=list(LANGUAGES),
        primary_languages=PrimaryLanguageMap(
            cities={"Vancouver": "en", "Montreal": "fr", "Berlin": "de", "Rome": "it"},
            regions={"BC": "en", "QC": "fr"},
            countries={"Canada": "en"},
        ),
    )


@pytest.fixture
def taxonomy() -> Taxonomy:
    return make_taxonomy()


def make_event(event_id="eventbrite_1", title="Toddler storytime", city="Vancouver, BC, Canada", source="Eventbrite", days=3, **kw) -> CityEvent:
    return CityEvent(
        event_id=event_id,
        city=city,
        title=title,
        source=source,
        url=kw.pop("url", f"https://example.com/e/{event_id}"),
        start_date=datetime.now(timezone.utc) + timedelta(days=days),
        **kw,
    )


def make_input(event_id="e1", city="Vancouver, BC, Canada", title="Toddler storytime", **kw) -> ClassifyInput:
    defaults = dict(
        event_id=event_id,
        url=f"https://example.com/e/{event_id}",
        is_canceled=False,
        title=title,
        description="Stories and songs for little ones.",
        location_summary="Central Library",
        source="Eventbrite",
        city=city,
        start_date=datetime(2026, 10, 3, 17, 0, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return ClassifyInput(**defaults)


class FakeCache:
    """In-memory stand-in for ClassificationCache."""

    def __init__(self, rows: Dict[str, ClassificationResult] = None):
        self.rows: Dict[str, ClassificationResult] = dict(rows or {})
        self.saved: List[ClassificationResult] = []
        self.status_updates: List[tuple] = []
        self.enabled = True

    def get_cached(self, event_ids: Iterable[str]):
        return {e: self.rows[e] for e in event_ids if e in self.rows}

    def save(self, results):
        results = list(results)
        self.saved.extend(results)
        for r in results:
            self.rows[r.event_id] = r
        return len(results)

    def update_status(self, items):
        self.status_updates.extend(items)


class FakeProvider:
    """Returns preset results (by event_id); ids in `fail` are missing from the output."""

    def __init__(self, name: str, decisions: Dict[str, dict] = None, fail: Iterable[str] = ()):
        self.name = name
        self.decisions = decisions or {}
        self.fail = set(fail)
        self.calls: List[List[str]] = []

    def classify(self, inputs, taxonomy):
        self.calls.append([i.event_id for i in inputs])
        out = {}
        for inp in inputs:
            if inp.event_id in self.fail:
                continue
            d = self.decisions.get(inp.event_id, {})
            out[inp.event_id] = ClassificationResult(
                event_id=inp.event_id,
                url=inp.url,
                is_canceled=inp.is_canceled,
                provider=self.name,
                model_version="test-model",
                prompt_version="test",
                family_score=d.get("family", 0.9),
                adult_score=d.get("adult", 0.0),
                decision=d.get("decision", "accept"),
                interest_value_ids=d.get("interests", []),
                language_value_ids=d.get("languages", []),
                source=inp.source,
                city=inp.city,
                title=inp.title,
            )
        return out
