from pydantic import BaseModel, HttpUrl, Field
from datetime import datetime

class CityEvent(BaseModel):
    """
    Defines the strict schema for our events. 
    Pydantic will automatically validate incoming scraped data against these rules.
    """
    city: str = Field(..., description="The normalized city name")
    title: str = Field(..., max_length=240, description="Event title, max 240 chars")
    source: str = Field(default="Eventbrite")
    url: HttpUrl 
    date: datetime 
    
    class Config:
        str_strip_whitespace = True