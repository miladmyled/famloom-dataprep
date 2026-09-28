"""
Give events read from web pages their own link and picture:
  1. picture/link already in structured data (JSON-LD image, iCal IMAGE/ATTACH) are kept;
  2. events without a link are matched to a link on the listing page by title, and take the
     picture of that link's card;
  3. events that still have no picture get the og:image / JSON-LD image of their own page
     (polite fetch, capped per page).
Organizers' own promotional pictures only; Facebook/Instagram pictures are never used.
"""
import logging
import os
from typing import Any, Dict, List

from src.etl.web_extract import jsonld_events, jsonld_image, link_cards, match_card, og_image

logger = logging.getLogger(__name__)


def enrich_events(events: List[Dict[str, Any]], page_html: str, page_url: str, http, max_detail_fetches: int = None) -> List[Dict[str, Any]]:
    if not events:
        return events
    limit = int(max_detail_fetches if max_detail_fetches is not None else os.getenv("PICTURE_DETAIL_FETCHES_PER_PAGE", "15"))
    cards = link_cards(page_html, page_url) if page_html else []
    page_base = page_url.split("#")[0].rstrip("/")
    fetched = 0
    for event in events:
        if event.get("event_url") and event["event_url"].split("#")[0].rstrip("/") == page_base:
            event["event_url"] = None  # a link to the listing itself is not the event's own page
        card = match_card(event.get("title", ""), cards) if cards else None
        if card is not None:
            if card["href"].split("#")[0].rstrip("/") != page_base and not event.get("event_url"):
                event["event_url"] = card["href"]
            if not event.get("picture") and card.get("image"):
                event["picture"] = card["image"]
        if event.get("picture") or not event.get("event_url") or http is None or fetched >= limit:
            continue
        fetched += 1
        try:
            detail = http.get(event["event_url"])
        except Exception as err:
            logger.debug(f"[PICTURE] detail page unavailable {event['event_url']}: {err}")
            continue
        if detail.status != 200:
            continue
        picture = og_image(detail.text, detail.url)
        if not picture:
            for node in jsonld_events(detail.text):
                picture = jsonld_image(node, detail.url)
                if picture:
                    break
        if picture:
            event["picture"] = picture
    return events
