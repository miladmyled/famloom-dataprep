from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseEventScraper(ABC):
    """
    Abstract base class defining the contract for external event scrapers.
    Any provider (Eventbrite, Meetup, Ticketmaster, etc.) must implement
    fetch_raw_events() and normalize_data().
    """

    def __init__(self, city: str, **kwargs: Any):
        self.city = city
        self.kwargs = kwargs

    @abstractmethod
    def fetch_raw_events(self) -> List[Dict[str, Any]]:
        """
        Fetches raw, unparsed event payloads from the external data source / API.

        Returns:
            List[Dict[str, Any]]: List of raw event dictionary objects from the provider.
        """
        pass

    @abstractmethod
    def normalize_data(self, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalizes provider-specific raw JSON event records into a standardized
        dictionary structure matching the CityEvent schema.

        Args:
            raw_events (List[Dict[str, Any]]): Raw JSON records from fetch_raw_events().

        Returns:
            List[Dict[str, Any]]: Normalized dictionaries ready for Pydantic validation.
        """
        pass

    def scrape(self) -> List[Dict[str, Any]]:
        """
        Template method executing the complete extraction and normalization flow.

        Returns:
            List[Dict[str, Any]]: Standardized event dictionaries for the target city.
        """
        raw_events = self.fetch_raw_events()
        return self.normalize_data(raw_events)
