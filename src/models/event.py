from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class CityEvent(BaseModel):
    """
    Defines the strict schema for family-friendly city events.
    Pydantic automatically validates incoming scraped data against these rules.
    """
    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    event_id: str = Field(..., description="Unique provider idempotency ID, e.g., 'eventbrite_12345'")
    city: str = Field(..., description="The normalized city name")
    title: str = Field(..., max_length=240, description="Event title, max 240 chars")
    source: str = Field(default="Eventbrite", description="Source platform name")
    url: HttpUrl = Field(..., description="Direct URL to the event page")
    start_date: datetime = Field(..., description="Event start timestamp (timezone-aware UTC)")
    end_date: Optional[datetime] = Field(default=None, description="Event end timestamp (timezone-aware UTC)")
    description: Optional[str] = Field(default=None, description="Event summary or description")
    location_summary: Optional[str] = Field(default=None, description="Venue name or physical address summary")
    status: str = Field(default="live", description="Event status: live, canceled, postponed")
    is_canceled: bool = Field(default=False, description="Tombstone flag for canceled events")

    @property
    def date(self) -> datetime:
        """Backwards compatibility accessor for start_date."""
        return self.start_date

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_fields(cls, data: any) -> any:
        """Maps legacy 'date' field to 'start_date' and sets default event_id if missing."""
        if isinstance(data, dict):
            # Map legacy 'date' to 'start_date'
            if "start_date" not in data and "date" in data:
                data["start_date"] = data["date"]
            # Fallback event_id generation if not explicitly provided
            if "event_id" not in data and "url" in data:
                url_str = str(data["url"])
                # Extract trailing id from URL or hash
                data["event_id"] = f"event_{abs(hash(url_str)) % 100000000}"
        return data

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def ensure_timezone_aware(cls, v: any) -> any:
        """
        Enforces timezone awareness. If a string or naive datetime is passed,
        ensures it is parsed and converted to timezone-aware UTC datetime.
        """
        if v is None:
            return None

        if isinstance(v, str):
            # Parse ISO formatted date string
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))

        if isinstance(v, datetime):
            if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
                # If naive, assume UTC
                v = v.replace(tzinfo=timezone.utc)
            else:
                # Convert to UTC
                v = v.astimezone(timezone.utc)

        return v

    @field_validator("start_date", mode="after")
    @classmethod
    def validate_future_date(cls, v: datetime) -> datetime:
        """
        Strict Business Rule: Automatically drop any event where start_date
        is strictly before CURRENT_DATE (in UTC). Only future events (today or later) survive.
        """
        current_utc_date = datetime.now(timezone.utc).date()
        if v.date() < current_utc_date:
            raise ValueError(
                f"Event start_date '{v.isoformat()}' (date: {v.date()}) is strictly before "
                f"CURRENT_DATE '{current_utc_date}'. Only events scheduled for today or later are valid."
            )
        return v