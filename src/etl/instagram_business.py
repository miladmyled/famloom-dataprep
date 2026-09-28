"""
Instagram Business Discovery source (spec phase 3, shipped with INSTAGRAM_ENABLED=false).
For each approved professional account of the city (config/sources/instagram_accounts.yaml):
recent posts (last 21 days) -> Jev screen ("announces a specific upcoming event with a date")
-> Gemini extraction with the post timestamp as reference date. The post's own picture is used
(decided 2026-09-27); no person names are stored.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.etl.base import BaseEventScraper
from src.etl.places import city_key, city_timezone

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "sources" / "instagram_accounts.yaml"
LOOKBACK_DAYS = 21
SOURCE_LABEL = "Instagram"


def load_accounts(path: Path = CONFIG_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [a for a in data if isinstance(a, dict)]


def parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:  # Graph API format: 2026-09-25T17:03:22+0000
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


class InstagramBusinessSource(BaseEventScraper):
    source_name = "Instagram"

    def __init__(self, city: str, graph=None, screener=None, extractor=None, accounts: Optional[List[Dict[str, Any]]] = None, **kwargs):
        super().__init__(city, **kwargs)
        self.tz = city_timezone(city)
        self.accounts = [
            a for a in (accounts if accounts is not None else load_accounts())
            if a.get("enabled") and a.get("username") and city_key(a.get("city", "")) == city_key(city)
        ]
        self._graph, self._screener, self._extractor = graph, screener, extractor
        self.metrics: Dict[str, int] = {}

    def _count(self, key: str, n: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + n

    @property
    def graph(self):
        if self._graph is None:
            from src.net.meta_graph import MetaGraphClient

            self._graph = MetaGraphClient()
        return self._graph

    @property
    def screener(self):
        if self._screener is None:
            from src.classify.screen import get_screener

            self._screener = get_screener() or False
        return self._screener or None

    @property
    def extractor(self):
        if self._extractor is None:
            from src.classify.gemini import GeminiExtractor

            self._extractor = GeminiExtractor()
        return self._extractor

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        from src.classify.screen import POST_ANNOUNCES_EVENT
        from src.net.meta_graph import AccountUnavailable, GraphRateLimited, TokenExpiredError

        if not self.accounts:
            return []
        if self.screener is None:
            logger.warning("[INSTAGRAM] No Jev screener; Instagram skipped.")
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        raw: List[Dict[str, Any]] = []
        for account in self.accounts:
            username = str(account["username"]).lstrip("@")
            try:
                posts = self.graph.recent_media(username)
            except TokenExpiredError as err:
                logger.error(f"[INSTAGRAM] {err}")
                self._count("token_expired")
                break
            except GraphRateLimited as err:
                logger.warning(f"[INSTAGRAM] Rate limited, stopping for this run: {err}")
                self._count("rate_limited")
                break
            except AccountUnavailable as err:
                logger.warning(f"[INSTAGRAM] Skipping account: {err}")
                self._count("accounts_skipped")
                continue
            except Exception as err:
                logger.warning(f"[INSTAGRAM] @{username} failed: {type(err).__name__}: {err}")
                self._count("accounts_failed")
                continue
            self._count("accounts_read")
            for post in posts:
                posted = parse_timestamp(post.timestamp)
                if not posted or posted < cutoff or not post.caption.strip():
                    continue
                self._count("posts_recent")
                if not self.screener.passes(post.caption, POST_ANNOUNCES_EVENT):
                    continue
                self._count("screened_in")
                events = self.extractor.extract(post.caption, posted.astimezone(self.tz), self.city, self.tz, source_url=None)
                for n, event in enumerate(events):
                    raw.append(dict(event, _media_id=post.media_id, _n=n, _permalink=post.permalink,
                                    _label=account.get("display_name"), _picture=post.picture or None))
                self._count("extracted", len(events))
        logger.info(f"[INSTAGRAM] '{self.city}': {self.metrics}")
        return raw

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for item in raw_events:
            suffix = f"_{item['_n']}" if item["_n"] else ""
            permalink = item["_permalink"]
            out.append({
                "event_id": f"instagram_{item['_media_id']}{suffix}",
                "city": self.city,
                "title": item["title"],
                "source": SOURCE_LABEL,
                "url": permalink + (f"#fl-{item['_n']}" if item["_n"] else ""),
                "start_date": item["start_date"],
                "end_date": item.get("end_date"),
                "description": item.get("description"),  # extractor's own neutral summary
                "location_summary": item.get("location_summary"),
                "status": "live",
                "is_canceled": False,
                "pictureurl": item.get("_picture"),
                "origin": "instagram",
            })
        return out


def instagram_enabled() -> bool:
    return os.getenv("INSTAGRAM_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
