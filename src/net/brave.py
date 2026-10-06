"""Brave Search API client (GET /res/v1/web/search) with a per-run query budget."""
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from src.classify.models import ProviderUnavailable

logger = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class SearchResult:
    title: str
    url: str
    description: str = ""
    extra_snippets: List[str] = field(default_factory=list)
    page_age: Optional[str] = None


class BudgetExhausted(Exception):
    """BRAVE_MAX_QUERIES_PER_RUN reached."""


class BraveSearchClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        max_queries: Optional[int] = None,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 15.0,
        min_interval_seconds: float = 1.1,
        sleep=time.sleep,
    ):
        self.api_key = api_key or os.getenv("BRAVE_SEARCH_API_KEY")
        if not self.api_key:
            raise ProviderUnavailable("BRAVE_SEARCH_API_KEY is not set")
        self.max_queries = int(max_queries if max_queries is not None else os.getenv("BRAVE_MAX_QUERIES_PER_RUN", "60"))
        self.session = session or requests.Session()
        self.timeout = timeout_seconds
        self.min_interval = min_interval_seconds
        self.sleep = sleep
        self.queries_used = 0
        self._last = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.max_queries - self.queries_used)

    def search(
        self,
        query: str,
        count: int = 20,
        country: str = "CA",
        search_lang: str = "en",
        freshness: Optional[str] = "pm",
        extra_snippets: bool = True,
    ) -> List[SearchResult]:
        if self.queries_used >= self.max_queries:
            raise BudgetExhausted(f"Brave query budget ({self.max_queries}) reached")
        params: Dict[str, object] = {"q": query, "count": min(count, 20), "country": country, "search_lang": search_lang}
        if freshness:
            params["freshness"] = freshness
        if extra_snippets:
            params["extra_snippets"] = "true"
        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        for attempt in range(3):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.queries_used += 1
            resp = self.session.get(BRAVE_URL, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                return self._parse(resp.json())
            if resp.status_code == 429 or resp.status_code >= 500:
                if self.queries_used >= self.max_queries:
                    break
                self.sleep(min(2 ** attempt + random.uniform(0, 0.5), 10.0))
                continue
            raise RuntimeError(f"Brave HTTP {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError("Brave search failed after retries")

    @staticmethod
    def _parse(payload: Dict) -> List[SearchResult]:
        out = []
        for r in ((payload.get("web") or {}).get("results") or []):
            if not r.get("url"):
                continue
            out.append(SearchResult(
                title=r.get("title") or "",
                url=r["url"],
                description=r.get("description") or "",
                extra_snippets=list(r.get("extra_snippets") or []),
                page_age=r.get("page_age") or r.get("age"),
            ))
        return out
