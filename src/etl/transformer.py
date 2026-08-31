from pydantic import ValidationError
from src.models.event import CityEvent

def clean_and_validate_event(raw_data: dict) -> CityEvent | None:
    """
    Takes raw dictionary data (e.g., scraped from Eventbrite), applies business 
    logic (removing virtual events), and validates it against our Pydantic model.
    """
    title = raw_data.get("title", "").lower()
    
    # Business Logic: Filter out online events per your architectural requirements
    forbidden_words = ["online", "virtual", "webinar", "zoom", "livestream"]
    if any(word in title for word in forbidden_words):
        print(f"⚠️  Skipping virtual event: {raw_data.get('title')}")
        return None

    try:
        # Hand the raw dictionary to Pydantic. 
        # If it passes, it becomes a perfect CityEvent object.
        valid_event = CityEvent(**raw_data)
        return valid_event
        
    except ValidationError as e:
        # In a real enterprise system, we would log this to Datadog/Grafana here
        print(f"❌ Data Quality Error! Dropping malformed event '{raw_data.get('title')}':\n{e}")
        return None

# Test the transformer with mock data simulating an Eventbrite scrape
if __name__ == "__main__":
    print("--- INITIATING TRANSFORMER TEST ---\n")
    
    # 1. A perfectly valid event in Coquitlam
    raw_good_event = {
        "city": "Coquitlam, BC, Canada",
        "title": "Family Bike Ride at Town Centre Park",
        "url": "https://eventbrite.com/e/coquitlam-bike-ride-123",
        "date": "2026-09-15T14:00:00Z"
    }
    
    # 2. An event that violates our physical-location rule
    raw_virtual_event = {
        "city": "Vancouver, BC, Canada",
        "title": "Online Parenting Webinar",
        "url": "https://eventbrite.com/e/webinar-456",
        "date": "2026-09-16T10:00:00Z"
    }
    
    # 3. An event with garbage data (missing the date and bad URL)
    raw_bad_event = {
        "city": "Coquitlam, BC, Canada",
        "title": "Weekend Farmers Market",
        "url": "not-a-real-url", 
        # Missing the required 'date' field entirely
    }
    
    print("Test 1: Processing Good Event...")
    valid = clean_and_validate_event(raw_good_event)
    if valid:
        print(f"✅ Success: {valid.title} is ready for the database.\n")
        
    print("Test 2: Processing Virtual Event...")
    clean_and_validate_event(raw_virtual_event)
    print("")
    
    print("Test 3: Processing Malformed Event...")
    clean_and_validate_event(raw_bad_event)