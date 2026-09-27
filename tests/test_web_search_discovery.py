import pytest

from src.etl.web_search_discovery import SiteStore, WebSearchDiscoverySource
from src.net.http import PoliteHttpClient
from tests.web_fakes import FakeBrave, FakeExtractor, FakeResponse, FakeScreener, FakeSession, fixture, public_resolver

HARBOUR = "https://www.harbourscience.example/events"
RIVERSIDE = "https://riverside.example/whats-on"


class MemoryStore(SiteStore):
    def __init__(self, sites=None):
        super().__init__(None)
        self.enabled = True
        self.sites = list(sites or [])
        self.remembered, self.successes, self.failures = [], [], []

    def active_sites(self, city):
        return [s for s in self.sites if s.get("city", city) == city]

    def remember(self, city, url, label, kind, scores, terms_url):
        self.remembered.append({"city": city, "url": url, "label": label, "kind": kind, "scores": scores, "terms": terms_url})

    def record_success(self, site_id):
        self.successes.append(site_id)

    def record_failure(self, site_id, reason):
        self.failures.append((site_id, reason))


def _routes(**overrides):
    routes = {
        HARBOUR: FakeResponse(HARBOUR, 200, fixture("venue_jsonld.html")),
        RIVERSIDE: FakeResponse(RIVERSIDE, 200, fixture("venue_text.html")),
        "https://www.harbourscience.example/terms-of-use": FakeResponse("https://www.harbourscience.example/terms-of-use", 200, "<main>Standard terms.</main>"),
        "https://riverside.example/legal/terms": FakeResponse("https://riverside.example/legal/terms", 200, "<main>Be kind.</main>"),
    }
    routes.update(overrides)
    return routes


def _source(routes=None, screener=None, extractor=None, store=None, max_queries=1, queries=("family events {city}",), robots=None):
    session = FakeSession(routes if routes is not None else _routes(), **({"default_robots": robots} if robots else {}))
    http = PoliteHttpClient(session=session, min_interval_seconds=0, resolver=public_resolver, sleep=lambda s: None)
    calendars = [{"url": "https://www.scienceworld.ca/events/", "enabled": True},
                 {"url": "https://www.destinationvancouver.com/events/", "enabled": False}]
    src = WebSearchDiscoverySource(
        "Vancouver, BC, Canada", search=FakeBrave("brave_discovery.json", max_queries=max_queries), http=http,
        screener=screener or FakeScreener(answers={"forbids": 0.05}), extractor=extractor or FakeExtractor(), store=store or MemoryStore(),
        queries=list(queries), blocked={"stubhub.ca"}, calendars=calendars,
    )
    return src, session


def test_approved_pages_give_events_and_are_remembered():
    store = MemoryStore()
    src, session = _source(store=store)
    events = src.normalize_data(src.fetch_raw_events())
    titles = sorted(e["title"] for e in events)
    assert titles == ["Family pancake breakfast", "Toddler Science Morning"]
    assert {r["url"] for r in store.remembered} == {HARBOUR, RIVERSIDE}
    assert {r["kind"] for r in store.remembered} == {"jsonld", "html"}
    assert all(e["event_id"].startswith("web_") and e["origin"] == "web" and e["pictureurl"] is None for e in events)
    assert {e["source"] for e in events} == {"Harbour Science Centre", "Riverside Community Centre"}
    # skip rules: facebook, curated (enabled), blocked, http-only
    assert src.metrics["skipped_social"] == 1 and src.metrics["skipped_curated"] == 1 and src.metrics["skipped_blocked"] == 1
    assert src.metrics["rejected_not_https"] == 1
    assert not any("facebook.com" in u or "stubhub" in u or "scienceworld" in u for u in session.requested)


def test_query_budget_is_respected():
    src, _ = _source(max_queries=1, queries=("a {city}", "b {city}", "c {city}"))
    src.fetch_raw_events()
    assert src.search.queries == ["a Vancouver"]


@pytest.mark.parametrize("override, reason", [
    ({RIVERSIDE: FakeResponse(RIVERSIDE, 200, fixture("login_wall.html"))}, "rejected_login"),
    ({RIVERSIDE: FakeResponse(RIVERSIDE, 200, fixture("noindex.html"))}, "rejected_robots_meta"),
    ({RIVERSIDE: FakeResponse(RIVERSIDE, 403, "")}, "rejected_login"),
    ({RIVERSIDE: FakeResponse(RIVERSIDE, 500, "")}, "rejected_fetch_error"),
    ({"https://riverside.example/legal/terms": FakeResponse("https://riverside.example/legal/terms", 200, fixture("terms_forbid.html"))}, "rejected_terms"),
])
def test_each_automatic_check_rejects_and_nothing_is_stored(override, reason):
    store = MemoryStore()
    screener = FakeScreener(by_text={"robot, spider, scraper": {"forbids": 0.95}}, answers={"forbids": 0.05})
    src, _ = _source(routes=_routes(**override), store=store, screener=screener)
    src.fetch_raw_events()
    assert src.metrics[reason] >= 1
    assert RIVERSIDE not in {r["url"] for r in store.remembered}


def test_robots_txt_disallow_rejects():
    src, _ = _source(robots="User-agent: *\nDisallow: /\n")
    assert src.fetch_raw_events() == []
    assert src.metrics["rejected_robots"] == 2


@pytest.mark.parametrize("key, reason", [
    ("lists_events", "rejected_ai_not_events"),
    ("real_organizer", "rejected_ai_not_organizer"),
    ("family_relevant", "rejected_ai_not_family"),
])
def test_ai_approval_needs_all_three_above_threshold(key, reason):
    store = MemoryStore()
    src, _ = _source(store=store, screener=FakeScreener(answers={key: 0.74, "forbids": 0.0}))
    assert src.fetch_raw_events() == []
    assert src.metrics[reason] == 2 and store.remembered == []


def test_remembered_sites_are_read_first_and_failures_recorded():
    store = MemoryStore(sites=[{"id": 7, "url": RIVERSIDE, "source_label": "Riverside CC", "kind": "html"},
                               {"id": 8, "url": "https://gone.example/events", "source_label": "Gone", "kind": "html"}])
    src, _ = _source(store=store, queries=())
    events = src.normalize_data(src.fetch_raw_events())
    assert [e["source"] for e in events] == ["Riverside CC"]
    assert store.successes == [7]
    assert store.failures == [(8, "rejected_fetch_error")]


def test_no_screener_means_no_discovery():
    src, session = _source()
    src._screener = False
    assert src.fetch_raw_events() == [] and session.requested == []
