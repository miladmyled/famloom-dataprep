"""Place helpers shared by the web sources: time zone of a city and a comparable city key."""
import os
from zoneinfo import ZoneInfo

from src.classify.text import fold

# Region / country part of "City, Region, Country" -> IANA time zone. Unknown -> CITY_DEFAULT_TIMEZONE.
_REGION_TZ = {
    "bc": "America/Vancouver", "british columbia": "America/Vancouver", "wa": "America/Los_Angeles",
    "ca": "America/Los_Angeles", "or": "America/Los_Angeles",
    "ab": "America/Edmonton", "alberta": "America/Edmonton",
    "sk": "America/Regina", "saskatchewan": "America/Regina",
    "mb": "America/Winnipeg", "manitoba": "America/Winnipeg",
    "on": "America/Toronto", "ontario": "America/Toronto", "qc": "America/Toronto", "quebec": "America/Toronto",
    "nb": "America/Halifax", "ns": "America/Halifax", "pe": "America/Halifax", "nl": "America/St_Johns",
    "germany": "Europe/Berlin", "italy": "Europe/Rome", "france": "Europe/Paris", "spain": "Europe/Madrid",
    "united kingdom": "Europe/London", "uk": "Europe/London",
}


def city_timezone(city: str) -> ZoneInfo:
    for part in reversed([fold(p) for p in str(city or "").split(",")]):
        if part in _REGION_TZ:
            return ZoneInfo(_REGION_TZ[part])
    return ZoneInfo(os.getenv("CITY_DEFAULT_TIMEZONE", "America/Vancouver"))


def city_key(city: str) -> str:
    """'North Vancouver, BC, Canada' -> 'north vancouver' (first part, folded)."""
    return fold(str(city or "").split(",")[0])
