import re
import logging
from typing import Any, Dict, List, Optional
from pydantic import ValidationError
from src.models.event import CityEvent

logger = logging.getLogger(__name__)


def match_interest_tags(
    title: str,
    description: Optional[str],
    interest_mapping: Dict[str, int],
) -> List[int]:
    """
    Analyzes event title and description against interest labels using
    case-insensitive whole-word regex matching.
    Returns a deduplicated, sorted list of matching question_value_ids.
    """
    if not interest_mapping:
        return []

    combined_text = f"{title or ''} {description or ''}"
    matched_ids = set()

    for label, question_value_id in interest_mapping.items():
        clean_label = label.strip()
        if not clean_label:
            continue
        # Case-insensitive whole-word regex matching
        pattern = rf"\b{re.escape(clean_label)}\b"
        if re.search(pattern, combined_text, re.IGNORECASE):
            matched_ids.add(question_value_id)

    return sorted(list(matched_ids))


def clean_and_validate_event(
    raw_data: Dict[str, Any],
    interest_tags: Optional[Dict[str, int]] = None,
) -> Optional[CityEvent]:
    """
    Takes a normalized event dictionary, applies physical-location business rules
    (filtering out virtual/online events), enriches with automated interest tagging,
    and validates schema contracts and date constraints against the CityEvent Pydantic
    model (including the 14-day window).

    Returns:
        CityEvent if valid and scheduled within the 14-day ingestion window.
        None if virtual, malformed, or scheduled outside the 14-day window.
    """
    data = dict(raw_data)
    title = str(data.get("title", "")).lower()
    description = str(data.get("description", "")).lower()

    # Business Logic: Filter out online / virtual events per architectural requirements
    forbidden_words = ["online", "virtual", "webinar", "zoom", "livestream", "virtual event"]
    if any(word in title for word in forbidden_words) or any(word in description for word in ["zoom link", "livestream only"]):
        logger.info(f"[FILTER] Skipping virtual event: '{data.get('title')}'")
        return None

    # Automated interest tagging enrichment
    if interest_tags:
        matched_tags = match_interest_tags(
            title=str(data.get("title", "")),
            description=data.get("description"),
            interest_mapping=interest_tags,
        )
        existing_tags = data.get("tag_ids") or []
        data["tag_ids"] = sorted(list(set(existing_tags + matched_tags)))

    try:
        # Hand the dictionary to Pydantic for validation & 14-day window enforcement
        valid_event = CityEvent(**data)
        return valid_event

    except ValidationError as e:
        event_title = raw_data.get("title", "Untitled")
        err_str = str(e)

        # Check if the validation failure is due to falling outside the 14-day window
        if "14-day ingestion window" in err_str or "CURRENT_DATE" in err_str:
            logger.warning(
                f"[DROP] Event falls outside the 14-day ingestion window: '{event_title}'"
            )
        else:
            logger.warning(f"[DROP] Data Quality / Schema validation dropped event '{event_title}': {e}")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Unexpected error processing event: {e}")
        return None


if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("--- INITIATING TRANSFORMER TEST ---\n")

    in_window_time = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    past_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    future_beyond_window = (datetime.now(timezone.utc) + timedelta(days=16)).isoformat()

    # 1. Valid future physical event (within 14 days)
    good_event = {
        "event_id": "eb_101",
        "city": "Coquitlam, BC, Canada",
        "title": "Family Bike Ride at Town Centre Park",
        "url": "https://eventbrite.com/e/coquitlam-bike-ride-123",
        "start_date": in_window_time,
    }

    # 2. Virtual event (forbidden)
    virtual_event = {
        "event_id": "eb_102",
        "city": "Vancouver, BC, Canada",
        "title": "Online Parenting Webinar",
        "url": "https://eventbrite.com/e/webinar-456",
        "start_date": in_window_time,
    }

    # 3. Past event (outside 14-day window: before today)
    past_event = {
        "event_id": "eb_103",
        "city": "Coquitlam, BC, Canada",
        "title": "Last Week Kids Art Workshop",
        "url": "https://eventbrite.com/e/art-workshop-789",
        "start_date": past_time,
    }

    # 4. Far-future event (outside 14-day window: > 14 days)
    far_future_event = {
        "event_id": "eb_104",
        "city": "Vancouver, BC, Canada",
        "title": "Next Month Winter Festival",
        "url": "https://eventbrite.com/e/winter-fest-999",
        "start_date": future_beyond_window,
    }

    print("Test 1: Processing In-Window Event (7 days)...")
    valid = clean_and_validate_event(good_event)
    if valid:
        print(f"[SUCCESS] {valid.title} is valid! (ID: {valid.event_id}, Date: {valid.start_date})\n")

    print("Test 2: Processing Virtual Event...")
    clean_and_validate_event(virtual_event)
    print("")

    print("Test 3: Processing Past Event (< CURRENT_DATE)...")
    clean_and_validate_event(past_event)
    print("")

    print("Test 4: Processing Far-Future Event (> 14 days)...")
    clean_and_validate_event(far_future_event)