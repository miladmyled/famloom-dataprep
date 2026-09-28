"""Offline fakes for the web sources: HTTP session, Jev screener, Gemini extractor, Brave client."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

WEB = Path(__file__).parent / "fixtures" / "web"


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def fixture(name: str) -> str:
    text = (WEB / name).read_text(encoding="utf-8")
    return text.replace("{D1}", day(3)).replace("{D2}", day(5)).replace("{D1_COMPACT}", day(3).replace("-", ""))


def ics_fixture() -> str:
    return (WEB / "city_calendar.ics").read_text(encoding="utf-8").replace("{D1}", day(3).replace("-", "")).replace("{D2}", day(5).replace("-", ""))


class FakeResponse:
    def __init__(self, url: str, status: int = 200, body: str = "", headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.status_code = status
        self._body = body.encode("utf-8")
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self._body.decode("utf-8")

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """routes: url -> FakeResponse or list of FakeResponses (consumed in order). Records every URL."""

    def __init__(self, routes: Dict[str, object], default_robots: str = "User-agent: *\nDisallow:\n"):
        self.routes = dict(routes)
        self.default_robots = default_robots
        self.requested: List[str] = []
        self.request_headers: List[dict] = []
        self.headers = {}

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True, params=None):
        self.requested.append(url)
        self.request_headers.append(dict(headers or {}))
        route = self.routes.get(url)
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        if route is not None:
            return route
        if url.endswith("/robots.txt"):
            return FakeResponse(url, 200, self.default_robots, {"Content-Type": "text/plain"})
        return FakeResponse(url, 404, "not found")


def public_resolver(host: str) -> list:
    return ["93.184.216.34"]  # any public address; tests never resolve real DNS


class FakeScreener:
    """Answers every question with preset probabilities (default 0.9); records the calls."""

    def __init__(self, answers: Optional[Dict[str, float]] = None, default: float = 0.9, by_text: Optional[Dict[str, Dict[str, float]]] = None):
        self.answers = answers or {}
        self.default = default
        self.by_text = by_text or {}
        self.calls: List[tuple] = []

    def ask(self, text: str, questions: Dict[str, str]) -> Dict[str, float]:
        self.calls.append((text, dict(questions)))
        for marker, answers in self.by_text.items():
            if marker in text:
                return {k: answers.get(k, self.default) for k in questions}
        return {k: self.answers.get(k, self.default) for k in questions}

    def passes(self, text: str, question: str, threshold: float = 0.5) -> bool:
        return self.ask(text, {"q": question}).get("q", 0.0) >= threshold


class FakeExtractor:
    def __init__(self, events: Optional[List[dict]] = None):
        self.events = events if events is not None else [{
            "title": "Family pancake breakfast",
            "start_date": datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2, hours=1),
            "end_date": None,
            "location_summary": "Riverside Community Centre",
            "description": "A community breakfast for families.",
            "event_url": None,
            "confidence": 0.9,
        }]
        self.calls: List[dict] = []

    def extract(self, text, reference, city, tz, source_url=None):
        self.calls.append({"text": text, "city": city, "source_url": source_url})
        return [dict(e) for e in self.events]


class FakeBrave:
    def __init__(self, payload_name: str, max_queries: int = 10):
        from src.net.brave import BraveSearchClient

        self.results = BraveSearchClient._parse(json.loads(fixture(payload_name)))
        self.max_queries = max_queries
        self.queries: List[str] = []

    def search(self, query, **kwargs):
        from src.net.brave import BudgetExhausted

        if len(self.queries) >= self.max_queries:
            raise BudgetExhausted("budget")
        self.queries.append(query)
        return list(self.results)
