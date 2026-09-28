import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.classify.models import ProviderUnavailable
from src.etl.instagram_business import InstagramBusinessSource, load_accounts, parse_timestamp
from src.net.meta_graph import AccountUnavailable, GraphRateLimited, MetaGraphClient, TokenExpiredError
from tests.web_fakes import FakeExtractor, FakeScreener

META = Path(__file__).parent / "fixtures" / "meta"


def _payload(name):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    sat = (datetime.now().date() + timedelta(days=3)).isoformat()
    return json.loads((META / name).read_text(encoding="utf-8").replace("{RECENT}", recent).replace("{SAT}", sat))


def _resp(payload, status=200, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.headers = headers or {}
    return r


def _graph(responses):
    session = MagicMock()
    session.get.side_effect = responses
    return MetaGraphClient(ig_user_id="17841411111111111", access_token="tok", version="v26.0", session=session,
                           min_interval_seconds=0, sleep=lambda s: None), session


ACCOUNTS = [
    {"city": "North Vancouver, BC, Canada", "username": "family_hikes", "display_name": "Family Hikes", "enabled": True},
    {"city": "North Vancouver, BC, Canada", "username": "personal_acct", "display_name": "Personal", "enabled": True},
    {"city": "North Vancouver, BC, Canada", "username": "off_acct", "enabled": False},
    {"city": "Vancouver, BC, Canada", "username": "other_city", "enabled": True},
]


def _event():
    return {"title": "Family hike at Lynn Canyon", "start_date": datetime.now(timezone.utc) + timedelta(days=3),
            "end_date": None, "location_summary": "Lynn Canyon Park", "description": "A guided family hike.",
            "event_url": None, "confidence": 0.9}


def test_request_uses_business_discovery_field_syntax():
    graph, session = _graph([_resp(_payload("business_discovery.json"))])
    posts = graph.recent_media("family_hikes")
    url = session.get.call_args[0][0]
    params = session.get.call_args[1]["params"]
    assert url == "https://graph.facebook.com/v26.0/17841411111111111"
    assert params["fields"] == "business_discovery.username(family_hikes){media.limit(25){id,caption,timestamp,permalink,media_type}}"
    assert params["access_token"] == "tok"
    assert len(posts) == 4 and posts[0].permalink == "https://www.instagram.com/p/AAA111/"


@pytest.mark.parametrize("fixture_name, error", [
    ("error_token.json", TokenExpiredError),
    ("error_not_business.json", AccountUnavailable),
    ("error_rate_limit.json", GraphRateLimited),
])
def test_graph_errors_are_classified(fixture_name, error):
    graph, _ = _graph([_resp(_payload(fixture_name), status=400)])
    with pytest.raises(error):
        graph.recent_media("x")


def test_usage_header_near_limit_stops():
    graph, _ = _graph([_resp(_payload("business_discovery.json"), headers={"X-App-Usage": '{"call_count": 95, "total_time": 10}'})])
    with pytest.raises(GraphRateLimited):
        graph.recent_media("x")


def test_missing_keys_unavailable(monkeypatch):
    monkeypatch.delenv("META_IG_USER_ID", raising=False)
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    with pytest.raises(ProviderUnavailable):
        MetaGraphClient()


def test_source_reads_recent_announcements_only_and_skips_non_business_accounts():
    graph, _ = _graph([_resp(_payload("business_discovery.json")), _resp(_payload("error_not_business.json"), status=400)])
    screener = FakeScreener(by_text={"Thanks everyone": {"q": 0.1}}, default=0.9)
    extractor = FakeExtractor([_event()])
    src = InstagramBusinessSource("North Vancouver, BC, Canada", graph=graph, screener=screener, extractor=extractor, accounts=ACCOUNTS)
    assert [a["username"] for a in src.accounts] == ["family_hikes", "personal_acct"]
    events = src.normalize_data(src.fetch_raw_events())
    # old post (2020) and caption-less post ignored; "thanks" post screened out
    assert len(screener.calls) == 2 and len(extractor.calls) == 1
    assert "Lynn Canyon" in extractor.calls[0]["text"]
    assert src.metrics["accounts_read"] == 1 and src.metrics["accounts_skipped"] == 1
    e = events[0]
    assert e["event_id"] == "instagram_17900000000000001" and e["url"] == "https://www.instagram.com/p/AAA111/"
    assert e["source"] == "Instagram" and e["origin"] == "instagram" and e["pictureurl"] is None


def test_several_events_in_one_caption_get_suffixes():
    graph, _ = _graph([_resp(_payload("business_discovery.json"))])
    src = InstagramBusinessSource("North Vancouver, BC, Canada", graph=graph, screener=FakeScreener(),
                                  extractor=FakeExtractor([_event(), dict(_event(), title="Second hike")]), accounts=ACCOUNTS[:1])
    ids = [e["event_id"] for e in src.normalize_data(src.fetch_raw_events())]
    assert "instagram_17900000000000001" in ids and "instagram_17900000000000001_1" in ids


def test_expired_token_stops_the_run():
    graph, session = _graph([_resp(_payload("error_token.json"), status=400)] * 2)
    src = InstagramBusinessSource("North Vancouver, BC, Canada", graph=graph, screener=FakeScreener(), extractor=FakeExtractor(), accounts=ACCOUNTS)
    assert src.fetch_raw_events() == []
    assert session.get.call_count == 1 and src.metrics["token_expired"] == 1


def test_timestamp_parsing():
    assert parse_timestamp("2026-09-25T17:03:22+0000") == datetime(2026, 9, 25, 17, 3, 22, tzinfo=timezone.utc)
    assert parse_timestamp("") is None


def test_shipped_accounts_file_is_example_only():
    assert all(not a["enabled"] for a in load_accounts())
