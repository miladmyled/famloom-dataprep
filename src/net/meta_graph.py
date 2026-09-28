"""
Explicit Meta Graph API client for Instagram Business Discovery (spec phase 3).
Separate from the shared web client on purpose: that client refuses every facebook.com host,
while this one only ever calls https://graph.facebook.com/<version>/<ig-user-id> with a token.
It never fetches instagram.com pages.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from src.classify.models import ProviderUnavailable

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_VERSION = "v26.0"
MEDIA_FIELDS = "id,caption,timestamp,permalink,media_type,media_url,thumbnail_url"
TOKEN_ERROR_CODES = {190}
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
USAGE_STOP_PERCENT = 90


class TokenExpiredError(Exception):
    """The access token is invalid or expired: refresh META_ACCESS_TOKEN."""


class GraphRateLimited(Exception):
    """Meta rate limit reached; stop for this run."""


class AccountUnavailable(Exception):
    """Account not found, not Business/Creator, age-gated or otherwise not discoverable."""


@dataclass
class IgPost:
    media_id: str
    caption: str
    timestamp: str
    permalink: str
    media_type: str
    media_url: str = ""
    thumbnail_url: str = ""

    @property
    def picture(self) -> str:
        """Post picture (video thumbnail for videos). Instagram CDN links are signed and expire
        after some days; every run re-saves the current link."""
        return (self.thumbnail_url if self.media_type == "VIDEO" else self.media_url) or self.media_url or self.thumbnail_url


class MetaGraphClient:
    def __init__(self, ig_user_id: Optional[str] = None, access_token: Optional[str] = None, version: Optional[str] = None,
                 session: Optional[requests.Session] = None, timeout_seconds: float = 20.0, min_interval_seconds: float = 1.0,
                 sleep=time.sleep):
        self.ig_user_id = ig_user_id or os.getenv("META_IG_USER_ID")
        self.access_token = access_token or os.getenv("META_ACCESS_TOKEN")
        self.version = version or os.getenv("META_GRAPH_API_VERSION") or DEFAULT_VERSION
        if not self.ig_user_id or not self.access_token:
            raise ProviderUnavailable("META_IG_USER_ID or META_ACCESS_TOKEN is not set")
        self.session = session or requests.Session()
        self.timeout = timeout_seconds
        self.min_interval = min_interval_seconds
        self.sleep = sleep
        self._last = 0.0

    def _check_usage(self, headers) -> None:
        for name in ("X-App-Usage", "X-Business-Use-Case-Usage", "X-Ad-Account-Usage"):
            raw = headers.get(name)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            values = data.values() if name == "X-Business-Use-Case-Usage" else [data]
            for entry in values:
                for item in (entry if isinstance(entry, list) else [entry]):
                    if not isinstance(item, dict):
                        continue
                    peak = max((v for k, v in item.items() if isinstance(v, (int, float)) and k in (
                        "call_count", "total_time", "total_cputime", "acc_id_util_pct")), default=0)
                    if peak >= USAGE_STOP_PERCENT:
                        raise GraphRateLimited(f"{name} at {peak}%")

    def recent_media(self, username: str, limit: int = 25) -> List[IgPost]:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        fields = f"business_discovery.username({username}){{media.limit({int(limit)}){{{MEDIA_FIELDS}}}}}"
        resp = self.session.get(
            f"{GRAPH_HOST}/{self.version}/{self.ig_user_id}",
            params={"fields": fields, "access_token": self.access_token},
            timeout=self.timeout,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        if error:
            code = error.get("code")
            message = str(error.get("message", ""))[:200]
            if code in TOKEN_ERROR_CODES:
                raise TokenExpiredError(f"Meta token expired or invalid; refresh META_ACCESS_TOKEN ({message})")
            if code in RATE_LIMIT_CODES:
                raise GraphRateLimited(message)
            raise AccountUnavailable(f"@{username}: {message} (code {code})")
        if resp.status_code != 200:
            raise AccountUnavailable(f"@{username}: HTTP {resp.status_code}")
        self._check_usage(resp.headers)
        media = ((payload.get("business_discovery") or {}).get("media") or {}).get("data") or []
        return [
            IgPost(m.get("id", ""), m.get("caption") or "", m.get("timestamp") or "", m.get("permalink") or "",
                   m.get("media_type") or "", m.get("media_url") or "", m.get("thumbnail_url") or "")
            for m in media if m.get("id")
        ]
