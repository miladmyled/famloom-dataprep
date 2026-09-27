import json
from unittest.mock import MagicMock

import pytest

from src.classify.models import ProviderUnavailable
from src.net.brave import BRAVE_URL, BraveSearchClient, BudgetExhausted
from tests.web_fakes import fixture


def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


def _client(responses, max_queries=5):
    session = MagicMock()
    session.get.side_effect = responses
    return BraveSearchClient(api_key="k", max_queries=max_queries, session=session, min_interval_seconds=0, sleep=lambda s: None), session


def test_request_shape_and_parsing():
    client, session = _client([_resp(200, json.loads(fixture("brave_facebook.json")))])
    results = client.search("family events Vancouver")
    url = session.get.call_args[0][0]
    kwargs = session.get.call_args[1]
    assert url == BRAVE_URL
    assert kwargs["headers"]["X-Subscription-Token"] == "k"
    assert kwargs["params"] == {"q": "family events Vancouver", "count": 20, "country": "CA", "search_lang": "en",
                                "freshness": "pm", "extra_snippets": "true"}
    assert results[0].url == "https://www.facebook.com/events/1234567890123/"
    assert results[0].extra_snippets == ["Hosted by a community group"]
    assert results[0].page_age == "2026-10-20T00:00:00"


def test_budget_is_enforced():
    client, _ = _client([_resp(200, {"web": {"results": []}})] * 3, max_queries=2)
    client.search("a")
    client.search("b")
    with pytest.raises(BudgetExhausted):
        client.search("c")
    assert client.remaining == 0


def test_429_is_retried_and_counts_against_budget():
    client, session = _client([_resp(429, {}), _resp(200, {"web": {"results": []}})])
    assert client.search("a") == []
    assert session.get.call_count == 2 and client.queries_used == 2


def test_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable):
        BraveSearchClient()
