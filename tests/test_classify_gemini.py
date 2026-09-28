import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.classify.decision import Thresholds
from src.classify.gemini import CLASSIFY_SCHEMA, GeminiClassifier, GeminiClient, GeminiQuotaExhausted
from src.classify.models import ProviderUnavailable
from tests.conftest import make_input

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"


def _response(status, name=None, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8")) if name else (body or {})
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.headers = headers or {}
    return resp


def _client(responses):
    session = MagicMock()
    session.post.side_effect = responses
    client = GeminiClient(api_key="k", model="gemini-3.5-flash-lite", session=session,
                          requests_per_minute=0, max_retries=2)
    return client, session


def test_request_uses_generate_content_with_json_schema(taxonomy, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, session = _client([_response(200, "classify_batch.json")])
    GeminiClassifier(client=client, thresholds=Thresholds()).classify([make_input("e1"), make_input("e2")], taxonomy)

    url = session.post.call_args[0][0]
    kwargs = session.post.call_args[1]
    assert url.endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert kwargs["headers"]["x-goog-api-key"] == "k"
    config = kwargs["json"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == CLASSIFY_SCHEMA
    prompt = kwargs["json"]["contents"][0]["parts"][0]["text"]
    assert "### event_id: e1" in prompt and "### event_id: e2" in prompt
    assert "never by culture" in prompt
    # Vancouver: English and Other are not offered
    assert "Allowed language ids: 502: French, 503: Persian (Farsi)" in prompt


def test_ids_are_validated_against_taxonomy_and_city(taxonomy, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, _ = _client([_response(200, "classify_batch.json")])
    results = GeminiClassifier(client=client, thresholds=Thresholds()).classify(
        [make_input("e1"), make_input("e2")], taxonomy
    )
    assert set(results) == {"e1", "e2"}  # 'unknown' event id ignored
    e1 = results["e1"]
    assert e1.interest_value_ids == [42]          # 9999 dropped
    assert e1.language_value_ids == [503]         # 501 (primary) and 519 (other) dropped
    assert e1.decision == "accept" and e1.provider == "gemini"
    assert results["e2"].decision == "reject"


def test_batches_are_split_by_batch_size(taxonomy, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, session = _client([_response(200, body={"candidates": [{"content": {"parts": [{"text": '{"events": []}'}]}}]})] * 3)
    GeminiClassifier(client=client, batch_size=2).classify([make_input(f"e{i}") for i in range(5)], taxonomy)
    assert session.post.call_count == 3


def test_daily_quota_stops_remaining_batches(taxonomy, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, session = _client([_response(429, "quota_per_day.json")])
    classifier = GeminiClassifier(client=client, batch_size=1)
    results = classifier.classify([make_input("e1"), make_input("e2")], taxonomy)
    assert results == {}
    assert classifier.quota_exhausted is True
    assert session.post.call_count == 1


def test_transient_429_is_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = _response(200, body={"candidates": [{"content": {"parts": [{"text": '{"events": []}'}]}}]})
    client, session = _client([_response(429, body={"error": {"message": "rate"}}, headers={"Retry-After": "1"}), ok])
    assert client.generate_json("p", CLASSIFY_SCHEMA) == {"events": []}
    assert session.post.call_count == 2


def test_persistent_429_raises_quota_exhausted(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, _ = _client([_response(429, body={"error": {"message": "rate"}})] * 3)
    with pytest.raises(GeminiQuotaExhausted):
        client.generate_json("p", CLASSIFY_SCHEMA)


def test_missing_key_or_model_is_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(ProviderUnavailable):
        GeminiClient()


def test_extractor_converts_local_time_to_utc_and_drops_incomplete(monkeypatch):
    from zoneinfo import ZoneInfo
    from src.classify.gemini import GeminiExtractor

    monkeypatch.setattr("time.sleep", lambda s: None)
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps({"events": [
        {"title": "Pancake breakfast", "start_local": "2026-10-03T09:00", "end_local": "2026-10-03T11:00",
         "location_summary": "Riverside CC", "description_short": "x" * 400, "event_url": "", "confidence": 0.9},
        {"title": "No place", "start_local": "2026-10-03T09:00", "location_summary": "", "confidence": 0.9},
        {"title": "Bad date", "start_local": "someday", "location_summary": "Park", "confidence": 0.9},
    ]})}]}}]}
    client, session = _client([_response(200, body=body)])
    events = GeminiExtractor(client=client).extract("text", __import__("datetime").datetime(2026, 9, 28), "Vancouver", ZoneInfo("America/Vancouver"))
    assert [e["title"] for e in events] == ["Pancake breakfast"]
    assert events[0]["start_date"].isoformat() == "2026-10-03T16:00:00+00:00"
    assert len(events[0]["description"]) == 300 and events[0]["event_url"] is None
    prompt = session.post.call_args[1]["json"]["contents"][0]["parts"][0]["text"]
    assert "never copied" in prompt and "skip online-only" in prompt and "Reference date" in prompt


def test_extractor_stops_after_quota(monkeypatch):
    from zoneinfo import ZoneInfo
    from src.classify.gemini import GeminiExtractor

    monkeypatch.setattr("time.sleep", lambda s: None)
    client, session = _client([_response(429, "quota_per_day.json")])
    ex = GeminiExtractor(client=client)
    tz = ZoneInfo("America/Vancouver")
    assert ex.extract("a", __import__("datetime").datetime(2026, 9, 28), "V", tz) == []
    assert ex.extract("b", __import__("datetime").datetime(2026, 9, 28), "V", tz) == []
    assert session.post.call_count == 1
