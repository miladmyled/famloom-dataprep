import os
import time
import random
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

from src.etl.base import BaseEventScraper

load_dotenv(override=True)
logger = logging.getLogger(__name__)


class EventbriteScraper(BaseEventScraper):
    """
    Production-ready scraper for Eventbrite API.
    Extracts external family-friendly events for a specified target city,
    handles rate-limits gracefully with exponential backoff, circuit-breaks pagination,
    and normalizes raw JSON payloads into standard event contracts.
    """

    def __init__(
        self,
        city: str,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
        max_pages: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        **kwargs: Any,
    ):
        super().__init__(city=city, **kwargs)
        self.api_token = api_token or os.getenv("EVENTBRITE_API_TOKEN", "")
        self.base_url = (base_url or os.getenv("EVENTBRITE_API_URL", "https://www.eventbriteapi.com/v3")).rstrip("/")

        # Pagination Circuit Breakers
        self.max_pages = max_pages or int(os.getenv("EVENTBRITE_MAX_PAGES", "5"))
        self.timeout_seconds = timeout_seconds or int(os.getenv("EVENTBRITE_TIMEOUT_SECONDS", "10"))
        self.max_scrape_duration_seconds = int(os.getenv("EVENTBRITE_MAX_SCRAPE_DURATION_SECONDS", "60"))

        # Rate Limit / Retry Config
        self.max_retries = int(os.getenv("EVENTBRITE_MAX_RETRIES", "3"))
        self.base_backoff_seconds = float(os.getenv("EVENTBRITE_BASE_BACKOFF_SECONDS", "1.0"))

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Famloom-DataPrep-Worker/1.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _post_with_retry(self, url: str, json_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Executes an HTTP POST request with robust retry mechanisms,
        handling 429 Rate Limits and 5xx transient server errors with exponential backoff.
        """
        headers = self._get_headers()

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=json_payload,
                    timeout=self.timeout_seconds,
                )

                # HTTP 429: Rate Limit Exceeded
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_time = float(retry_after)
                    else:
                        sleep_time = min(30.0, self.base_backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5))

                    logger.warning(
                        f"⚠️ Rate limit (HTTP 429) encountered for city '{self.city}'. "
                        f"Backing off for {sleep_time:.2f}s (Attempt {attempt}/{self.max_retries})"
                    )
                    time.sleep(sleep_time)
                    continue

                # HTTP 5xx: Transient Server Error
                if 500 <= response.status_code < 600:
                    sleep_time = min(30.0, self.base_backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5))
                    logger.warning(
                        f"⚠️ Eventbrite server error (HTTP {response.status_code}). "
                        f"Retrying in {sleep_time:.2f}s (Attempt {attempt}/{self.max_retries})"
                    )
                    time.sleep(sleep_time)
                    continue

                # HTTP 401/403: Authentication / Permission Error
                if response.status_code in (401, 403):
                    logger.error(
                        f"❌ Eventbrite Authentication/Authorization failed (HTTP {response.status_code}). "
                        "Please verify EVENTBRITE_API_TOKEN."
                    )
                    return None

                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout:
                logger.warning(
                    f"⏱️ Request timeout ({self.timeout_seconds}s) for city '{self.city}' "
                    f"(Attempt {attempt}/{self.max_retries})"
                )
                time.sleep(self.base_backoff_seconds * (2 ** (attempt - 1)))
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Network error requesting Eventbrite API: {e} (Attempt {attempt}/{self.max_retries})")
                time.sleep(self.base_backoff_seconds * (2 ** (attempt - 1)))

        logger.error(f"❌ Max retries reached for Eventbrite API request in city '{self.city}'.")
        return None

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        """
        Fetches raw events from Eventbrite for the target city using pagination circuit breakers.
        """
        if not self.api_token:
            logger.warning(
                f"⚠️ EVENTBRITE_API_TOKEN is not configured. Skipping live API fetch for city '{self.city}'."
            )
            return []

        endpoint = f"{self.base_url}/destination/search/"
        raw_events: List[Dict[str, Any]] = []
        page = 1
        continuation_token = None
        start_time = time.time()

        # Simplify city name for search query (e.g. 'Vancouver, BC, Canada' -> 'Vancouver')
        clean_city_query = self.city.split(",")[0].strip()
        search_query = f"family {clean_city_query}"

        logger.info(f"🔍 Starting Eventbrite scrape for city: '{self.city}' (Query: '{search_query}', Max Pages: {self.max_pages})")

        while page <= self.max_pages:
            # Circuit breaker: Hard timeout guard
            elapsed = time.time() - start_time
            if elapsed > self.max_scrape_duration_seconds:
                logger.warning(
                    f"⏱️ Circuit Breaker: Scrape duration ({elapsed:.1f}s) exceeded limit "
                    f"({self.max_scrape_duration_seconds}s) for city '{self.city}'. Halting pagination."
                )
                break

            event_search_params: Dict[str, Any] = {
                "q": search_query,
                "dates": "current_future",
                "page": page,
                "page_size": 20,
            }
            if continuation_token:
                event_search_params["continuation"] = continuation_token

            payload = {"event_search": event_search_params}

            data = self._post_with_retry(endpoint, payload)
            if not data:
                logger.warning(f"No response received on page {page} for city '{self.city}'. Terminating pagination.")
                break

            # Handle Eventbrite destination search schema
            events_data = data.get("events", {})
            events_page = events_data.get("results", [])
            if not events_page:
                # Fallback to direct 'events' list if using another endpoint format
                events_page = data.get("events", []) if isinstance(data.get("events"), list) else []

            if not events_page:
                logger.info(f"ℹ️ No events found on page {page} for city '{self.city}'.")
                break

            raw_events.extend(events_page)
            logger.info(f"📄 Page {page}: Extracted {len(events_page)} events for '{self.city}'.")

            # Check pagination metadata
            pagination = events_data.get("pagination", {})
            total_pages = pagination.get("page_count", page)
            has_more = pagination.get("has_more_items", False) or (page < total_pages)
            continuation_token = pagination.get("continuation")

            if not has_more or page >= total_pages:
                logger.info(f"🏁 Reached last available page ({page}/{total_pages}) for city '{self.city}'.")
                break

            page += 1

        logger.info(f"✅ Total raw events fetched for '{self.city}': {len(raw_events)}")
        return raw_events

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalizes Eventbrite raw payloads into structured dictionaries matching CityEvent schema.
        Extracts idempotency event_id, timezone-aware start/end dates, and status/tombstone flags.
        """
        normalized: List[Dict[str, Any]] = []

        for raw in raw_events:
            # Extract idempotency key: unique event_id
            raw_id = raw.get("id") or raw.get("eid") or raw.get("eventbrite_event_id")
            if not raw_id and "url" in raw:
                raw_id = str(abs(hash(raw["url"])) % 100000000)
            event_id = f"eventbrite_{raw_id}" if raw_id and not str(raw_id).startswith("eventbrite_") else str(raw_id or "")

            # Extract title
            title_field = raw.get("name")
            if isinstance(title_field, dict):
                title = title_field.get("text") or title_field.get("html") or ""
            else:
                title = raw.get("name") or raw.get("title", "")
            title = str(title).strip()

            # Extract URL
            url = raw.get("url") or raw.get("tickets_url", "")

            # Extract timezone and dates
            event_tz_name = raw.get("timezone", "UTC")
            try:
                event_tz = ZoneInfo(event_tz_name)
            except Exception:
                event_tz = timezone.utc

            # Extract start date / time
            start_date = None
            if isinstance(raw.get("start"), dict):
                start_date = raw["start"].get("utc") or raw["start"].get("local")
            elif "start_date" in raw:
                raw_date_str = raw.get("start_date")
                raw_time_str = raw.get("start_time", "00:00")
                if raw_date_str:
                    try:
                        # Combine date and time, apply event timezone, convert to UTC ISO string
                        dt_str = f"{raw_date_str} {raw_time_str}"
                        local_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=event_tz)
                        start_date = local_dt.astimezone(timezone.utc).isoformat()
                    except Exception:
                        start_date = raw_date_str
            else:
                start_date = raw.get("date")

            # Extract end date / time
            end_date = None
            if isinstance(raw.get("end"), dict):
                end_date = raw["end"].get("utc") or raw["end"].get("local")
            elif "end_date" in raw and raw.get("end_date"):
                raw_end_date_str = raw.get("end_date")
                raw_end_time_str = raw.get("end_time", "23:59")
                try:
                    dt_str = f"{raw_end_date_str} {raw_end_time_str}"
                    local_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=event_tz)
                    end_date = local_dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    end_date = raw_end_date_str

            # Extract description / summary
            desc_field = raw.get("description")
            if isinstance(desc_field, dict):
                description = desc_field.get("text") or desc_field.get("html")
            else:
                description = raw.get("summary") or raw.get("full_description") or raw.get("description")

            # Extract location / venue summary
            location_summary = None
            venue_field = raw.get("venue")
            if isinstance(venue_field, dict):
                venue_name = venue_field.get("name")
                address_field = venue_field.get("address", {})
                localized_address = address_field.get("localized_address_display") or address_field.get("address_1")
                location_parts = [p for p in [venue_name, localized_address] if p]
                location_summary = " - ".join(location_parts) if location_parts else None
            elif raw.get("locations") and isinstance(raw["locations"], list):
                loc_names = [loc.get("name") for loc in raw["locations"] if isinstance(loc, dict) and loc.get("name")]
                location_summary = ", ".join(loc_names) if loc_names else None
            elif "location_summary" in raw:
                location_summary = raw.get("location_summary")

            # Virtual event check flag from API
            is_online = raw.get("is_online_event", False)
            if is_online and not description:
                description = "virtual online event"

            # Status & Tombstone handling
            raw_status = str(raw.get("status", "live")).lower()
            is_cancelled_flag = raw.get("is_cancelled") is True or raw.get("is_canceled") is True
            if raw_status in ("canceled", "cancelled") or is_cancelled_flag:
                status = "canceled"
                is_canceled = True
            elif raw_status in ("postponed", "rescheduled"):
                status = "postponed"
                is_canceled = False
            else:
                status = "live"
                is_canceled = False

            normalized_dict = {
                "event_id": event_id,
                "city": self.city,
                "title": title,
                "source": "Eventbrite",
                "url": url,
                "start_date": start_date,
                "end_date": end_date,
                "description": description,
                "location_summary": location_summary,
                "status": status,
                "is_canceled": is_canceled,
            }

            normalized.append(normalized_dict)

        return normalized
