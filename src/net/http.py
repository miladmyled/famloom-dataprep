"""
Shared, polite HTTP client for every web source (curated calendars, web discovery).

- Identifies itself: User-Agent "FamLoomBot/1.0 (+mailto:<CRAWLER_CONTACT_EMAIL>)".
- Obeys robots.txt (cached per domain for the client's lifetime, i.e. one run), including
  Crawl-delay when it is longer than the default per-domain interval.
- Rate-limits per domain (default one request every 2 seconds), retries 429/5xx with backoff.
- Caps response size (default 3 MB) and uses ETag / Last-Modified when a URL is fetched again.
- Refuses Facebook/Instagram domains and private, loopback or link-local addresses, always.
  The Instagram source talks to graph.facebook.com through its own explicit client instead.
No login, no CAPTCHA solving, no stealth: a site that says no is simply skipped.
"""
import ipaddress
import logging
import os
import random
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

BLOCKED_DOMAINS = ("facebook.com", "fb.com", "fb.me", "instagram.com", "instagr.am")
DEFAULT_MAX_BYTES = 3 * 1024 * 1024


class BlockedDomainError(Exception):
    """The URL points at a domain or address this crawler must never request."""


class RobotsDisallowedError(Exception):
    """robots.txt does not allow our User-Agent to fetch this URL."""


class ResponseTooLargeError(Exception):
    """The response exceeded the size cap."""


def user_agent() -> str:
    contact = os.getenv("CRAWLER_CONTACT_EMAIL", "").strip()
    return f"FamLoomBot/1.0 (+mailto:{contact})" if contact else "FamLoomBot/1.0"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def is_blocked_domain(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def _is_private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def check_url_allowed(url: str, resolver: Callable[[str], list] = None) -> None:
    """Raise BlockedDomainError for blocked social domains, non-http(s) URLs and private addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedDomainError(f"Unsupported scheme: {parts.scheme!r}")
    host = host_of(url)
    if not host:
        raise BlockedDomainError("URL has no host")
    if is_blocked_domain(host):
        raise BlockedDomainError(f"Blocked domain: {host}")
    if host == "localhost" or host.endswith(".localhost") or _is_private_ip(host.strip("[]")):
        raise BlockedDomainError(f"Private or loopback address: {host}")
    resolver = resolver or (lambda h: [info[4][0] for info in socket.getaddrinfo(h, None)])
    try:
        addresses = resolver(host)
    except OSError:
        return  # unresolvable: the request itself will fail normally
    for address in addresses:
        if _is_private_ip(address):
            raise BlockedDomainError(f"{host} resolves to a private address")


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    not_modified: bool = False


class PoliteHttpClient:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        min_interval_seconds: Optional[float] = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_retries: int = 2,
        resolver: Optional[Callable[[str], list]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent(), "Accept-Language": "en-CA,en;q=0.8"})
        self.min_interval = float(min_interval_seconds if min_interval_seconds is not None else os.getenv("CRAWLER_MIN_INTERVAL_SECONDS", "2"))
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.resolver = resolver
        self.sleep = sleep
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._last_request: Dict[str, float] = {}
        self._validators: Dict[str, Dict[str, str]] = {}
        self._bodies: Dict[str, FetchResult] = {}
        self._lock = threading.Lock()

    # ---- robots.txt -----------------------------------------------------------------------

    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        parser = urllib.robotparser.RobotFileParser()
        try:
            self._throttle(host_of(url))
            resp = self.session.get(f"{origin}/robots.txt", timeout=self.timeout, allow_redirects=True)
            if resp.status_code in (401, 403):
                parser.disallow_all = True  # the site refuses us: treat as "no"
            elif resp.status_code >= 400:
                parser.allow_all = True  # no robots.txt: everything allowed
            else:
                parser.parse(resp.text.splitlines())
        except requests.RequestException as err:
            logger.info(f"[HTTP] robots.txt unreachable for {origin} ({err}); skipping the site this run")
            parser.disallow_all = True
        self._robots[origin] = parser
        return parser

    def allowed_by_robots(self, url: str) -> bool:
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(user_agent(), url)

    def _crawl_delay(self, url: str) -> float:
        parser = self._robots.get(f"{urlsplit(url).scheme}://{urlsplit(url).netloc}")
        try:
            delay = parser.crawl_delay(user_agent()) if parser else None
        except Exception:
            delay = None
        return float(delay) if delay else 0.0

    # ---- rate limit -----------------------------------------------------------------------

    def _throttle(self, host: str, interval: Optional[float] = None) -> None:
        interval = self.min_interval if interval is None else interval
        with self._lock:
            wait = self._last_request.get(host, 0.0) + interval - time.monotonic()
            self._last_request[host] = time.monotonic() + max(wait, 0.0)
        if wait > 0:
            self.sleep(wait)

    # ---- fetch ----------------------------------------------------------------------------

    def get(self, url: str, check_robots: bool = True) -> FetchResult:
        check_url_allowed(url, self.resolver)
        if check_robots and not self.allowed_by_robots(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        host = host_of(url)
        headers = dict(self._validators.get(url, {}))
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host, max(self.min_interval, self._crawl_delay(url)))
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout, stream=True, allow_redirects=True)
                # a redirect must not land on a blocked domain either
                check_url_allowed(resp.url, self.resolver)
                if resp.status_code == 304 and url in self._bodies:
                    cached = self._bodies[url]
                    return FetchResult(url, 200, cached.text, cached.content_type, cached.headers, not_modified=True)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                    retry_after = resp.headers.get("Retry-After", "")
                    self.sleep(min(float(retry_after), 60.0) if retry_after.isdigit() else min(2 ** attempt + random.uniform(0, 0.5), 30.0))
                    continue
                body = self._read_capped(resp)
                result = FetchResult(resp.url, resp.status_code, body, resp.headers.get("Content-Type", ""), dict(resp.headers))
                if resp.status_code == 200:
                    validators = {}
                    if resp.headers.get("ETag"):
                        validators["If-None-Match"] = resp.headers["ETag"]
                    if resp.headers.get("Last-Modified"):
                        validators["If-Modified-Since"] = resp.headers["Last-Modified"]
                    if validators:
                        self._validators[url] = validators
                        self._bodies[url] = result
                return result
            except (requests.ConnectionError, requests.Timeout) as err:
                last_error = err
                self.sleep(min(2 ** attempt, 10.0))
        raise last_error or requests.RequestException(f"GET {url} failed")

    def _read_capped(self, resp: requests.Response) -> str:
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            resp.close()
            raise ResponseTooLargeError(f"{resp.url} declares {declared} bytes")
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > self.max_bytes:
                resp.close()
                raise ResponseTooLargeError(f"{resp.url} exceeded {self.max_bytes} bytes")
            chunks.append(chunk)
        raw = b"".join(chunks)
        encoding = resp.encoding or "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def get_rendered(self, url: str, wait_ms: int = 3000) -> FetchResult:
        """Render a JavaScript page with headless Chromium (same guard and robots rules, no stealth)."""
        check_url_allowed(url, self.resolver)
        if not self.allowed_by_robots(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        self._throttle(host_of(url), max(self.min_interval, self._crawl_delay(url)))
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                page = browser.new_page(user_agent=user_agent())
                # block requests to blocked domains (e.g. embedded Facebook widgets) and heavy assets
                page.route("**/*", lambda route: route.abort() if (
                    is_blocked_domain(host_of(route.request.url)) or route.request.resource_type in ("image", "media", "font")
                ) else route.continue_())
                resp = page.goto(url, wait_until="domcontentloaded", timeout=int(max(self.timeout, 45.0) * 1000))
                page.wait_for_timeout(wait_ms)
                html = page.content()
                if len(html.encode("utf-8")) > self.max_bytes:
                    raise ResponseTooLargeError(f"{url} rendered page exceeded {self.max_bytes} bytes")
                return FetchResult(page.url, resp.status if resp else 0, html, "text/html")
            finally:
                browser.close()
