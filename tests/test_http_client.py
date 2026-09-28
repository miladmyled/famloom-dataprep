import pytest

from src.net.http import (
    BlockedDomainError,
    PoliteHttpClient,
    ResponseTooLargeError,
    RobotsDisallowedError,
    check_url_allowed,
    user_agent,
)
from tests.web_fakes import FakeResponse, FakeSession, public_resolver


def _client(routes, robots="User-agent: *\nDisallow:\n", **kw):
    session = FakeSession(routes, default_robots=robots)
    client = PoliteHttpClient(session=session, min_interval_seconds=0, resolver=public_resolver, sleep=lambda s: None, **kw)
    return client, session


@pytest.mark.parametrize("url", [
    "https://facebook.com/events/1", "https://www.facebook.com/events/1/", "https://m.facebook.com/x",
    "https://fb.com/x", "https://fb.me/abc", "https://www.instagram.com/p/1", "https://instagr.am/p/1",
    "http://127.0.0.1/admin", "http://10.0.0.5/", "http://[::1]/", "http://localhost:8080/", "ftp://example.com/x",
])
def test_blocked_urls(url):
    with pytest.raises(BlockedDomainError):
        check_url_allowed(url, resolver=public_resolver)


def test_hostname_resolving_to_private_address_is_blocked():
    with pytest.raises(BlockedDomainError):
        check_url_allowed("https://intranet.example.com/", resolver=lambda h: ["192.168.1.10"])


def test_lookalike_domain_is_not_blocked():
    check_url_allowed("https://notfacebook.com/events", resolver=public_resolver)


def test_blocked_domain_is_never_requested():
    client, session = _client({})
    with pytest.raises(BlockedDomainError):
        client.get("https://www.facebook.com/events/123/")
    assert session.requested == []


def test_user_agent_carries_contact(monkeypatch):
    monkeypatch.setenv("CRAWLER_CONTACT_EMAIL", "team@example.com")
    assert user_agent() == "FamLoomBot/1.0 (+mailto:team@example.com)"


def test_robots_disallow_is_respected():
    client, session = _client({}, robots="User-agent: *\nDisallow: /events\n")
    with pytest.raises(RobotsDisallowedError):
        client.get("https://venue.example/events")
    assert session.requested == ["https://venue.example/robots.txt"]


def test_robots_403_means_no():
    client, _ = _client({"https://guarded.example/robots.txt": FakeResponse("https://guarded.example/robots.txt", 403, "")})
    assert client.allowed_by_robots("https://guarded.example/events") is False


def test_robots_404_means_allowed_and_is_cached():
    client, session = _client({
        "https://open.example/robots.txt": FakeResponse("https://open.example/robots.txt", 404, ""),
        "https://open.example/a": FakeResponse("https://open.example/a", 200, "A"),
        "https://open.example/b": FakeResponse("https://open.example/b", 200, "B"),
    })
    assert client.get("https://open.example/a").text == "A"
    assert client.get("https://open.example/b").text == "B"
    assert session.requested.count("https://open.example/robots.txt") == 1


def test_size_cap():
    client, _ = _client({"https://big.example/p": FakeResponse("https://big.example/p", 200, "x" * 5000)}, max_bytes=1000)
    with pytest.raises(ResponseTooLargeError):
        client.get("https://big.example/p")


def test_retries_on_429_then_succeeds():
    url = "https://busy.example/p"
    client, session = _client({url: [FakeResponse(url, 429, "", {"Retry-After": "1"}), FakeResponse(url, 200, "ok")]})
    assert client.get(url).text == "ok"
    assert session.requested.count(url) == 2


def test_redirect_to_blocked_domain_is_refused():
    url = "https://short.example/go"
    client, _ = _client({url: FakeResponse("https://www.facebook.com/events/9/", 200, "fb")})
    with pytest.raises(BlockedDomainError):
        client.get(url)


def test_conditional_request_uses_etag():
    url = "https://feed.example/cal.ics"
    client, session = _client({url: [
        FakeResponse(url, 200, "BODY", {"Content-Type": "text/calendar", "ETag": '"v1"'}),
        FakeResponse(url, 304, ""),
    ]})
    assert client.get(url).text == "BODY"
    second = client.get(url)
    assert second.not_modified and second.text == "BODY"
    assert session.request_headers[-1].get("If-None-Match") == '"v1"'
