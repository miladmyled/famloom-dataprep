import os
import re
import json
import logging
from typing import Any, Dict, List, Optional
import urllib.parse
from dotenv import load_dotenv

from src.etl.base import BaseExtractor

load_dotenv(override=True)
logger = logging.getLogger(__name__)


class MeetupExtractor(BaseExtractor):
    """
    Public scraper for Meetup events utilizing headless Playwright.
    Navigates to Meetup's public event discovery pages, waits for network idle,
    extracts script[type="application/ld+json"] payloads, and normalizes Schema.org
    Event data into the standard CityEvent schema.
    """

    def __init__(
        self,
        city: str = "Coquitlam, BC",
        target_url: Optional[str] = None,
        headless: bool = True,
        timeout_seconds: int = 30,
        **kwargs: Any,
    ):
        super().__init__(city=city, **kwargs)
        if target_url:
            self.target_url = target_url
        elif os.getenv("MEETUP_TARGET_URL"):
            self.target_url = os.getenv("MEETUP_TARGET_URL")
        else:
            clean_city = re.sub(r",\s*(Canada|USA|US)$", "", self.city, flags=re.IGNORECASE).strip()
            encoded_city = urllib.parse.quote(clean_city)
            self.target_url = f"https://www.meetup.com/find/?location={encoded_city}&source=EVENTS"
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000


    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        """
        Launches Playwright headless Chromium, navigates to target Meetup URL,
        waits for network idle, and extracts all JSON-LD event definitions.

        Returns:
            List[Dict[str, Any]]: List of raw Schema.org Event dicts extracted from the page.
        """
        from playwright.sync_api import sync_playwright

        logger.info(f"🌐 [MeetupExtractor] Navigating to '{self.target_url}' (headless={self.headless})...")
        raw_events: List[Dict[str, Any]] = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page = context.new_page()

                # Navigate and wait for network idle to ensure hydration is complete
                page.goto(self.target_url, timeout=self.timeout_ms)
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms)

                # Locate all application/ld+json script tags
                script_elements = page.query_selector_all('script[type="application/ld+json"]')
                logger.info(f"🔎 [MeetupExtractor] Found {len(script_elements)} application/ld+json script tag(s).")

                for elem in script_elements:
                    content = elem.inner_text()
                    if not content or not content.strip():
                        continue

                    try:
                        payload = json.loads(content.strip())
                        self._extract_events_from_payload(payload, raw_events)
                    except json.JSONDecodeError as jde:
                        logger.warning(f"⚠️ [MeetupExtractor] Failed to parse JSON-LD script block: {jde}")

                browser.close()

        except Exception as err:
            logger.error(f"❌ [MeetupExtractor] Error fetching events via Playwright: {err}", exc_info=True)

        logger.info(f"📥 [MeetupExtractor] Extracted {len(raw_events)} raw event payload(s) for '{self.city}'.")
        return raw_events

    def _extract_events_from_payload(
        self, payload: Any, raw_events: List[Dict[str, Any]]
    ) -> None:
        """
        Recursively extracts Schema.org Event objects from dict, list, or graph structures.
        """
        if isinstance(payload, list):
            for item in payload:
                self._extract_events_from_payload(item, raw_events)
        elif isinstance(payload, dict):
            item_type = payload.get("@type")
            if item_type == "Event":
                raw_events.append(payload)
            elif "@graph" in payload:
                self._extract_events_from_payload(payload["@graph"], raw_events)
            elif "itemListElement" in payload:
                elements = payload.get("itemListElement")
                if isinstance(elements, list):
                    for el in elements:
                        if isinstance(el, dict):
                            if el.get("@type") == "Event":
                                raw_events.append(el)
                            elif "item" in el and isinstance(el["item"], dict):
                                self._extract_events_from_payload(el["item"], raw_events)

    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalizes Schema.org JSON-LD Event objects into dictionaries adhering
        to the CityEvent contract defined in src/models/event.py.

        Args:
            raw_events: Raw Schema.org Event dictionaries.

        Returns:
            List[Dict[str, Any]]: Standardized event dictionaries ready for transformer validation.
        """
        normalized: List[Dict[str, Any]] = []

        for raw in raw_events:
            try:
                # 1. Event ID extraction
                url = raw.get("url", "")
                event_id = None
                if url:
                    match = re.search(r"/events/(\d+)", str(url))
                    if match:
                        event_id = f"meetup_{match.group(1)}"
                if not event_id:
                    event_id = f"meetup_{abs(hash(str(url or raw.get('name', '')))) % 100000000}"

                # 2. Title (max 240 chars per CityEvent schema)
                title = str(raw.get("name") or "Untitled Meetup Event").strip()[:240]

                # 3. Start and End Date
                start_date = raw.get("startDate")
                end_date = raw.get("endDate") or None
                if not start_date:
                    logger.warning(f"⚠️ [MeetupExtractor] Skipping event '{title}' missing startDate.")
                    continue

                # 4. Description
                description = raw.get("description")
                if description:
                    description = str(description).strip()

                # 5. Location summary
                location_summary = self._format_location_summary(raw.get("location"))

                # 6. Status and tombstone flag
                status_raw = str(raw.get("eventStatus", "")).lower()
                is_canceled = False
                status = "live"

                if "cancelled" in status_raw or "canceled" in status_raw:
                    status = "canceled"
                    is_canceled = True
                elif "postponed" in status_raw:
                    status = "postponed"
                elif "rescheduled" in status_raw:
                    status = "postponed"

                event_city = self._resolve_event_city(raw)

                normalized_event = {
                    "event_id": event_id,
                    "city": event_city,
                    "title": title,
                    "source": "Meetup",
                    "url": url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "description": description,
                    "location_summary": location_summary,
                    "status": status,
                    "is_canceled": is_canceled,
                    "tag_ids": [],
                }
                normalized.append(normalized_event)

            except Exception as err:
                logger.error(f"❌ [MeetupExtractor] Error normalizing raw Meetup event: {err}", exc_info=True)

        return normalized

    def _resolve_event_city(self, raw_event: Dict[str, Any]) -> str:
        """
        Determines the true city for an event from its Schema.org location address,
        falling back to self.city if not explicitly specified.
        """
        has_country = "canada" in self.city.lower() or "usa" in self.city.lower()
        suffix = ", Canada" if has_country else ""

        loc = raw_event.get("location")
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                locality = addr.get("addressLocality")
                region = addr.get("addressRegion", "BC")
                # Exclude country placeholders like 'Canada' in addressLocality
                if locality and str(locality).strip().lower() not in ["canada", "usa", "us"]:
                    locality_clean = str(locality).strip()
                    return f"{locality_clean}, {region}{suffix}"

                # Check streetAddress for known municipalities
                street = str(addr.get("streetAddress", ""))
                for known in ["Vancouver", "Coquitlam", "Burnaby", "Richmond", "Surrey", "Toronto"]:
                    if re.search(rf"\b{known}\b", street, re.IGNORECASE):
                        return f"{known}, {region}{suffix}"
        return self.city



    def _format_location_summary(self, location_data: Any) -> Optional[str]:
        """
        Extracts human-readable venue name and address from Schema.org Place/VirtualLocation.
        """
        if not location_data:
            return None

        if isinstance(location_data, str):
            return location_data.strip() or None

        if isinstance(location_data, dict):
            name = location_data.get("name")
            address = location_data.get("address")
            addr_str = ""

            if isinstance(address, dict):
                parts = [
                    address.get("streetAddress"),
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                ]
                addr_str = ", ".join([str(p).strip() for p in parts if p and str(p).strip()])
            elif isinstance(address, str):
                addr_str = address.strip()

            if name and addr_str:
                return f"{name.strip()} ({addr_str})"
            return name or addr_str or None

        return None
