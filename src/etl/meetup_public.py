import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request
from dotenv import load_dotenv

from src.etl.base import BaseExtractor

load_dotenv(override=True)
logger = logging.getLogger(__name__)

# Meetup GraphQL Persisted Query hashes and endpoint
MEETUP_GQL_ENDPOINT = "https://www.meetup.com/gql2"
LOCATION_SEARCH_HASH = "950b939f7033b26849b13e829e04cad7fb6b6e4593e97499fceb3ff21764206d"
RECOMMENDED_EVENTS_HASH = "fe189ee9858cbae80c3cb4100ed216f1c60b4f2956d26e27187fb6d0aca23506"

# Known city coordinates as fast lookup & fallback
KNOWN_CITY_COORDINATES: Dict[str, Tuple[float, float]] = {
    "vancouver": (49.2827, -123.1207),
    "north vancouver": (49.3200, -123.0724),
    "west vancouver": (49.3260, -123.1588),
    "burnaby": (49.2488, -122.9805),
    "coquitlam": (49.2838, -122.7932),
    "port coquitlam": (49.2626, -122.7811),
    "port moody": (49.2831, -122.8317),
    "richmond": (49.1666, -123.1336),
    "surrey": (49.1913, -122.8490),
    "new westminster": (49.2057, -122.9110),
    "delta": (49.0847, -123.0587),
    "langley": (49.1044, -122.6587),
    "maple ridge": (49.2193, -122.5984),
    "toronto": (43.6532, -79.3832),
    "montreal": (45.5017, -73.5673),
    "calgary": (51.0447, -114.0719),
    "ottawa": (45.4215, -75.6972),
    "edmonton": (53.5461, -113.4938),
    "new york": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "seattle": (47.6062, -122.3321),
    "san francisco": (37.7749, -122.4194),
}


class MeetupExtractor(BaseExtractor):
    """
    Public extractor for Meetup events.
    Resolves exact city latitude/longitude coordinates and queries Meetup's
    public GraphQL API with cursor pagination. Includes headless Playwright
    fallback if GraphQL is unavailable.
    """

    def __init__(
        self,
        city: str = "Coquitlam, BC",
        target_url: Optional[str] = None,
        headless: bool = True,
        timeout_seconds: int = 30,
        max_scrolls: Optional[int] = None,
        scroll_delay_seconds: Optional[float] = None,
        distance: Optional[str] = None,
        use_graphql: Optional[bool] = None,
        max_events: Optional[int] = None,
        **kwargs: Any,
    ):
        super().__init__(city=city, **kwargs)
        self.distance = distance or os.getenv("MEETUP_SEARCH_DISTANCE", "tenMiles")

        if target_url:
            self.target_url = target_url
        elif os.getenv("MEETUP_TARGET_URL"):
            self.target_url = os.getenv("MEETUP_TARGET_URL")
        else:
            clean_city = re.sub(r",\s*(Canada|USA|US)$", "", self.city, flags=re.IGNORECASE).strip()
            encoded_city = urllib.parse.quote(clean_city)
            dist_param = f"&distance={self.distance}" if self.distance else ""
            self.target_url = f"https://www.meetup.com/find/?location={encoded_city}&source=EVENTS{dist_param}"

        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self.max_scrolls = max_scrolls if max_scrolls is not None else int(os.getenv("MEETUP_MAX_SCROLLS", "8"))
        self.scroll_delay_seconds = (
            scroll_delay_seconds
            if scroll_delay_seconds is not None
            else float(os.getenv("MEETUP_SCROLL_DELAY_SECONDS", "1.5"))
        )

        env_use_gql = os.getenv("MEETUP_USE_GRAPHQL", "true").lower() in ["1", "true", "yes"]
        self.use_graphql = use_graphql if use_graphql is not None else env_use_gql
        self.max_events = max_events if max_events is not None else int(os.getenv("MEETUP_MAX_EVENTS", "50"))
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None

    def resolve_coordinates(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Resolves latitude and longitude for the target city using fast dictionary lookup,
        or Meetup's getLocationSearch GraphQL API.
        """
        if self.lat is not None and self.lon is not None:
            return self.lat, self.lon

        # 1. Fast static lookup
        city_key = re.sub(r",.*", "", self.city).strip().lower()
        if city_key in KNOWN_CITY_COORDINATES:
            self.lat, self.lon = KNOWN_CITY_COORDINATES[city_key]
            logger.info(f"📍 [MeetupExtractor] Resolved '{self.city}' via static lookup: lat={self.lat}, lon={self.lon}")
            return self.lat, self.lon

        # 2. Dynamic Meetup getLocationSearch query
        try:
            clean_city = re.sub(r",\s*(Canada|USA|US)$", "", self.city, flags=re.IGNORECASE).strip()
            payload = {
                "operationName": "getLocationSearch",
                "variables": {"query": clean_city, "dataConfiguration": "{}"},
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": LOCATION_SEARCH_HASH,
                    }
                },
            }
            req = urllib.request.Request(
                MEETUP_GQL_ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = data.get("data", {}).get("result", [])
                if results and isinstance(results, list):
                    first = results[0]
                    self.lat = float(first["lat"])
                    self.lon = float(first["lon"])
                    logger.info(
                        f"📍 [MeetupExtractor] Resolved '{self.city}' via Meetup API: "
                        f"lat={self.lat}, lon={self.lon} ({first.get('name')})"
                    )
                    return self.lat, self.lon
        except Exception as loc_err:
            logger.warning(f"⚠️ [MeetupExtractor] Dynamic coordinate resolution failed for '{self.city}': {loc_err}")

        # Fallback to Vancouver coordinates if unresolvable
        self.lat, self.lon = (49.2827, -123.1207)
        return self.lat, self.lon

    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        """
        Extracts raw events from Meetup.
        Prefers direct GraphQL querying with city coordinates and cursor pagination.
        Falls back to Playwright headless Chromium if direct GraphQL is disabled or fails.

        Returns:
            List[Dict[str, Any]]: List of raw event dicts (GraphQL nodes and Schema.org Events).
        """
        if self.use_graphql:
            try:
                events = self.fetch_raw_events_graphql()
                if events:
                    return events
                logger.warning(
                    f"⚠️ [MeetupExtractor] Direct GraphQL returned 0 events for '{self.city}'. "
                    f"Falling back to Playwright..."
                )
            except Exception as gql_err:
                logger.warning(
                    f"⚠️ [MeetupExtractor] Direct GraphQL query failed for '{self.city}': {gql_err}. "
                    f"Falling back to Playwright..."
                )

        return self.fetch_raw_events_playwright()

    def fetch_raw_events_graphql(self) -> List[Dict[str, Any]]:
        """
        Queries Meetup's GraphQL endpoint directly using city latitude/longitude coordinates
        and cursor-based pagination. High performance with minimal memory overhead.
        """
        lat, lon = self.resolve_coordinates()
        if lat is None or lon is None:
            raise ValueError(f"Cannot query GraphQL without valid coordinates for '{self.city}'")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        logger.info(
            f"🌐 [MeetupExtractor] Querying Meetup GraphQL for '{self.city}' "
            f"(lat={lat}, lon={lon}, target={self.max_events} events)..."
        )

        all_events: List[Dict[str, Any]] = []
        seen_ids = set()
        after_cursor = None

        headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        while len(all_events) < self.max_events:
            batch_size = min(50, self.max_events - len(all_events))
            variables: Dict[str, Any] = {
                "first": batch_size,
                "lat": lat,
                "lon": lon,
                "startDateRange": now_iso,
                "numberOfEventsForSeries": 5,
                "seriesStartDate": today_str,
                "sortField": "RELEVANCE",
                "doConsolidateEvents": True,
                "doPromotePaypalEvents": False,
                "dataConfiguration": json.dumps({
                    "isSimplifiedSearchEnabled": True,
                    "include_events_from_user_chapters": True,
                }),
            }
            if after_cursor:
                variables["after"] = after_cursor

            payload = {
                "operationName": "recommendedEventsWithSeries",
                "variables": variables,
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": RECOMMENDED_EVENTS_HASH,
                    }
                },
            }

            req = urllib.request.Request(
                MEETUP_GQL_ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result = data.get("data", {}).get("result", {})
                edges = result.get("edges", [])
                if not edges:
                    break

                for edge in edges:
                    node = edge.get("node")
                    if isinstance(node, dict):
                        node_id = str(node.get("id") or node.get("eventUrl") or "")
                        if node_id and node_id not in seen_ids:
                            seen_ids.add(node_id)
                            all_events.append(node)

                page_info = result.get("pageInfo", {})
                if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                    break
                after_cursor = page_info["endCursor"]

        logger.info(
            f"📥 [MeetupExtractor] GraphQL extracted {len(all_events)} raw event payload(s) for '{self.city}'."
        )
        return all_events

    def fetch_raw_events_playwright(self) -> List[Dict[str, Any]]:
        """
        Launches Playwright headless Chromium with geographic context, navigates to Meetup URL,
        intercepts GraphQL pagination queries during infinite scroll, and extracts JSON-LD tags.
        """
        from playwright.sync_api import sync_playwright

        lat, lon = self.resolve_coordinates()
        logger.info(
            f"🌐 [MeetupExtractor] Navigating via Playwright to '{self.target_url}' "
            f"(lat={lat}, lon={lon}, headless={self.headless}, max_scrolls={self.max_scrolls})..."
        )
        captured_events: Dict[str, Dict[str, Any]] = {}

        try:
            with sync_playwright() as p:
                launch_args = (
                    [
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-setuid-sandbox",
                    ]
                    if os.name != "nt"
                    else None
                )
                browser = p.chromium.launch(headless=self.headless, args=launch_args)
                context_options: Dict[str, Any] = {
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "viewport": {"width": 1280, "height": 900},
                }
                if lat is not None and lon is not None:
                    context_options["geolocation"] = {"latitude": lat, "longitude": lon}
                    context_options["permissions"] = ["geolocation"]

                context = browser.new_context(**context_options)
                page = context.new_page()

                # Abort unnecessary heavy assets (images, fonts, media) to optimize memory and network throughput
                try:
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in ["image", "media", "font"]
                            else route.continue_()
                        ),
                    )
                except Exception:
                    pass

                # Response listener to capture dynamic GraphQL responses
                def _handle_response(response: Any) -> None:
                    try:
                        if "gql2" in response.url and response.status == 200:
                            body = response.json()
                            if isinstance(body, dict) and "data" in body:
                                data = body["data"]
                                result = data.get("result") if isinstance(data, dict) else None
                                if isinstance(result, dict) and "edges" in result:
                                    edges = result.get("edges")
                                    if isinstance(edges, list):
                                        for edge in edges:
                                            if isinstance(edge, dict):
                                                node = edge.get("node")
                                                if isinstance(node, dict):
                                                    node_id = str(node.get("id") or node.get("eventUrl") or "")
                                                    if node_id and node_id not in captured_events:
                                                        captured_events[node_id] = node
                    except Exception:
                        pass

                page.on("response", _handle_response)

                # 1. Initial page navigation using domcontentloaded for fast HTML/SSR load
                try:
                    page.goto(self.target_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    page.wait_for_timeout(2000)
                except Exception as nav_err:
                    logger.warning(f"⚠️ [MeetupExtractor] Navigation wait encountered: {nav_err}. Continuing...")

                # 2. Extract SSR Schema.org JSON-LD tags
                json_ld_events: List[Dict[str, Any]] = []
                try:
                    script_elements = page.query_selector_all('script[type="application/ld+json"]')
                    for elem in script_elements:
                        content = elem.inner_text()
                        if not content or not content.strip():
                            continue
                        try:
                            payload = json.loads(content.strip())
                            self._extract_events_from_payload(payload, json_ld_events)
                        except json.JSONDecodeError:
                            pass
                except Exception as ld_err:
                    logger.warning(f"⚠️ [MeetupExtractor] Error reading JSON-LD scripts: {ld_err}")

                for ld_ev in json_ld_events:
                    ev_key = str(ld_ev.get("url") or ld_ev.get("name") or len(captured_events))
                    if ev_key not in captured_events:
                        captured_events[ev_key] = ld_ev

                logger.info(
                    f"🔎 [MeetupExtractor] Initial load captured {len(captured_events)} event(s) "
                    f"(JSON-LD + initial GraphQL). Commencing scrolls..."
                )

                # 3. Iterative scrolling to hydrate additional paginated events
                consecutive_zero_diff = 0
                for scroll_idx in range(1, self.max_scrolls + 1):
                    count_before = len(captured_events)
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(int(self.scroll_delay_seconds * 1000))
                    except Exception as scroll_err:
                        logger.warning(f"⚠️ [MeetupExtractor] Scroll step {scroll_idx} failed: {scroll_err}")
                        break

                    new_added = len(captured_events) - count_before
                    if new_added == 0:
                        consecutive_zero_diff += 1
                        if consecutive_zero_diff >= 2:
                            logger.info(
                                f"ℹ️ [MeetupExtractor] No new events detected after {scroll_idx} scrolls. "
                                f"Ending scroll loop."
                            )
                            break
                    else:
                        consecutive_zero_diff = 0

                browser.close()

        except Exception as err:
            logger.error(f"❌ [MeetupExtractor] Error fetching events via Playwright: {err}", exc_info=True)

        raw_list = list(captured_events.values())
        logger.info(f"📥 [MeetupExtractor] Extracted total {len(raw_list)} raw event payload(s) for '{self.city}'.")
        return raw_list

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
        Normalizes raw Meetup events (Schema.org JSON-LD or GraphQL node dicts)
        into standardized dictionaries adhering to the CityEvent contract.

        Args:
            raw_events: List of raw event dictionaries.

        Returns:
            List[Dict[str, Any]]: Standardized event dictionaries ready for transformer validation.
        """
        normalized: List[Dict[str, Any]] = []
        seen_urls = set()

        for raw in raw_events:
            try:
                # 1. URL and Event ID extraction
                url = str(raw.get("eventUrl") or raw.get("url") or "").strip()
                if url and url in seen_urls:
                    continue

                event_id = None
                node_id = raw.get("id")
                if node_id and str(node_id).isdigit():
                    event_id = f"meetup_{node_id}"
                elif url:
                    match = re.search(r"/events/(\d+)", url)
                    if match:
                        event_id = f"meetup_{match.group(1)}"

                if not event_id:
                    fallback_hash = abs(hash(str(url or raw.get("title") or raw.get("name", "")))) % 100000000
                    event_id = f"meetup_{fallback_hash}"

                # 2. Title (max 240 chars per CityEvent schema)
                title = str(raw.get("title") or raw.get("name") or "Untitled Meetup Event").strip()[:240]

                # 3. Start and End Date
                start_date = raw.get("dateTime") or raw.get("startDate")
                end_date = raw.get("endTime") or raw.get("endDate") or None
                if not start_date:
                    logger.warning(f"⚠️ [MeetupExtractor] Skipping event '{title}' missing startDate/dateTime.")
                    continue

                # 4. Description
                description = raw.get("description")
                if description:
                    description = str(description).strip()

                # 5. Location summary
                location_summary = self._resolve_location_summary(raw)

                # 6. Status and tombstone flag
                status_raw = str(raw.get("eventStatus") or raw.get("status") or "").lower()
                is_canceled = False
                status = "live"

                if "cancelled" in status_raw or "canceled" in status_raw:
                    status = "canceled"
                    is_canceled = True
                elif "postponed" in status_raw or "rescheduled" in status_raw:
                    status = "postponed"

                event_city = self._resolve_event_city(raw)
                pictureurl = self._resolve_picture_url(raw)

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
                    "pictureurl": pictureurl,
                    "tag_ids": [],
                }
                normalized.append(normalized_event)
                if url:
                    seen_urls.add(url)

            except Exception as err:
                logger.error(f"❌ [MeetupExtractor] Error normalizing raw Meetup event: {err}", exc_info=True)

        return normalized

    def _resolve_picture_url(self, raw: Dict[str, Any]) -> Optional[str]:
        """
        Extracts high-resolution image URL from Meetup GraphQL photo objects,
        Schema.org image fields, or group fallback photos.
        """
        # 1. Direct photo objects from GraphQL (featuredEventPhoto, displayPhoto, photo)
        for key in ("featuredEventPhoto", "displayPhoto", "photo"):
            photo_obj = raw.get(key)
            if isinstance(photo_obj, dict):
                url = photo_obj.get("highResUrl") or photo_obj.get("baseUrl")
                if url and str(url).startswith("http"):
                    return str(url)

        # 2. Schema.org / standard image field
        img = raw.get("image")
        if isinstance(img, str) and img.startswith("http"):
            return img
        elif isinstance(img, list) and img and isinstance(img[0], str) and img[0].startswith("http"):
            return img[0]
        elif isinstance(img, dict):
            url = img.get("url") or img.get("contentUrl")
            if url and str(url).startswith("http"):
                return str(url)

        # 3. Fallback to group key photo
        group = raw.get("group")
        if isinstance(group, dict):
            group_photo = group.get("keyGroupPhoto")
            if isinstance(group_photo, dict):
                url = group_photo.get("highResUrl") or group_photo.get("baseUrl")
                if url and str(url).startswith("http"):
                    return str(url)

        # 4. Direct pictureurl or picture_url
        for k in ("pictureurl", "picture_url", "image_url", "photo_url"):
            val = raw.get(k)
            if isinstance(val, str) and val.startswith("http"):
                return val

        return None

    def _resolve_location_summary(self, raw_event: Dict[str, Any]) -> Optional[str]:
        """
        Extracts human-readable venue name and address from either GraphQL venue
        structures or Schema.org Place/VirtualLocation objects.
        """
        event_type = str(raw_event.get("eventType", "")).upper()

        # Check GraphQL venue object
        venue = raw_event.get("venue")
        if isinstance(venue, dict):
            name = str(venue.get("name", "")).strip()
            address = str(venue.get("address", "")).strip()
            city = str(venue.get("city", "")).strip()
            state = str(venue.get("state", "")).strip()

            addr_parts = [p for p in [address, city, state] if p]
            addr_str = ", ".join(addr_parts)

            if name and addr_str:
                return f"{name} ({addr_str})"
            if name:
                return name
            if addr_str:
                return addr_str

        if event_type == "ONLINE":
            return "Online event"

        # Fall back to Schema.org location
        loc = raw_event.get("location")
        if loc:
            return self._format_schema_location(loc)

        return None

    def _format_schema_location(self, location_data: Any) -> Optional[str]:
        """
        Extracts location string from Schema.org Place / PostalAddress.
        """
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
                return f"{str(name).strip()} ({addr_str})"
            return str(name).strip() if name else (addr_str or None)

        return None

    def _resolve_event_city(self, raw_event: Dict[str, Any]) -> str:
        """
        Determines the true city for an event from GraphQL venue or Schema.org address,
        falling back to self.city if not explicitly specified.
        """
        has_ca = "canada" in self.city.lower()
        has_us = "usa" in self.city.lower() or "us" in [x.strip().lower() for x in self.city.split(",")]
        suffix = ", Canada" if has_ca else (", USA" if has_us else "")

        # Infer default state from self.city if available (e.g. "NY" from "New York, NY")
        state_match = re.search(r",\s*([A-Za-z]{2})\b", self.city)
        default_state = state_match.group(1).upper() if state_match else "BC"

        # 1. GraphQL Venue
        venue = raw_event.get("venue")
        if isinstance(venue, dict):
            v_city = venue.get("city")
            v_state = venue.get("state") or default_state
            if v_city and str(v_city).strip().lower() not in ["canada", "usa", "us", ""]:
                return f"{str(v_city).strip()}, {v_state}{suffix}"

        # 2. Schema.org Location
        loc = raw_event.get("location")
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                locality = addr.get("addressLocality")
                region = addr.get("addressRegion") or default_state
                if locality and str(locality).strip().lower() not in ["canada", "usa", "us"]:
                    return f"{str(locality).strip()}, {region}{suffix}"

                street = str(addr.get("streetAddress", ""))
                for known in ["Vancouver", "Coquitlam", "Burnaby", "Richmond", "Surrey", "Toronto", "North Vancouver", "New York"]:
                    if re.search(rf"\b{known}\b", street, re.IGNORECASE):
                        return f"{known}, {region}{suffix}"

        return self.city

