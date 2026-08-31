import logging
from typing import Any, Dict, Optional
from pydantic import ValidationError
from src.models.event import CityEvent

logger = logging.getLogger(__name__)


def clean_and_validate_event(raw_data: Dict[str, Any]) -> Optional[CityEvent]:
    """
    Takes a normalized event dictionary, applies physical-location business rules
    (filtering out virtual/online events), and validates schema contracts and
    date constraints against the CityEvent Pydantic model.

    Returns:
        CityEvent if valid and scheduled for CURRENT_DATE or later.
        None if virtual, malformed, or scheduled in the past.
    """
    title = str(raw_data.get("title", "")).lower()
    description = str(raw_data.get("description", "")).lower()

    # Business Logic: Filter out online / virtual events per architectural requirements
    forbidden_words = ["online", "virtual", "webinar", "zoom", "livestream", "virtual event"]
    if any(word in title for word in forbidden_words) or any(word in description for word in ["zoom link", "livestream only"]):
        logger.info(f"[FILTER] Skipping virtual event: '{raw_data.get('title')}'")
        return None

    try:
        # Hand the dictionary to Pydantic for validation & timezone-aware date enforcement
        valid_event = CityEvent(**raw_data)
        return valid_event

    except ValidationError as e:
        # Catch and log validation errors (including past start_date rejection and schema errors)
        event_title = raw_data.get("title", "Untitled")
        logger.warning(f"[DROP] Data Quality / Business Rule dropped event '{event_title}': {e}")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Unexpected error processing event: {e}")
        return None


if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("--- INITIATING TRANSFORMER TEST ---\n")

    future_time = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    past_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    # 1. Valid future physical event
    good_event = {
        "event_id": "eb_101",
        "city": "Coquitlam, BC, Canada",
        "title": "Family Bike Ride at Town Centre Park",
        "url": "https://eventbrite.com/e/coquitlam-bike-ride-123",
        "start_date": future_time,
    }

    # 2. Virtual event (forbidden)
    virtual_event = {
        "event_id": "eb_102",
        "city": "Vancouver, BC, Canada",
        "title": "Online Parenting Webinar",
        "url": "https://eventbrite.com/e/webinar-456",
        "start_date": future_time,
    }

    # 3. Past event (should be rejected by date rule)
    past_event = {
        "event_id": "eb_103",
        "city": "Coquitlam, BC, Canada",
        "title": "Last Week Kids Art Workshop",
        "url": "https://eventbrite.com/e/art-workshop-789",
        "start_date": past_time,
    }

    print("Test 1: Processing Good Future Event...")
    valid = clean_and_validate_event(good_event)
    if valid:
        print(f"[SUCCESS] {valid.title} is valid! (ID: {valid.event_id}, Date: {valid.start_date})\n")

    print("Test 2: Processing Virtual Event...")
    clean_and_validate_event(virtual_event)
    print("")

    print("Test 3: Processing Past Event...")
    clean_and_validate_event(past_event)