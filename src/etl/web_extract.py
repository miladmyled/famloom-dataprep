"""Helpers to read event data out of web pages: schema.org JSON-LD, main text, page metadata."""
import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_EVENT_TYPE_RE = re.compile(r"Event$")
_WS_RE = re.compile(r"\s+")


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _iter_nodes(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
            if key in data:
                yield from _iter_nodes(data[key])


def _is_event(node: Dict[str, Any]) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and _EVENT_TYPE_RE.search(t) for t in types)


def jsonld_events(html: str) -> List[Dict[str, Any]]:
    """All schema.org Event (and subtype) objects found in ld+json scripts."""
    events: List[Dict[str, Any]] = []
    for script in soup_of(html).find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        events.extend(node for node in _iter_nodes(data) if _is_event(node))
    return events


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value"))
    if isinstance(value, list) and value:
        return _text(value[0])
    return None


def _location(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    if str(value.get("@type", "")).lower() == "virtuallocation":
        return None
    parts = [_text(value.get("name"))]
    address = value.get("address")
    if isinstance(address, dict):
        parts += [_text(address.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion")]
    elif isinstance(address, str):
        parts.append(address.strip())
    parts = [p for p in parts if p]
    return ", ".join(dict.fromkeys(parts)) or None


def normalize_jsonld_event(node: Dict[str, Any], page_url: str) -> Optional[Dict[str, Any]]:
    """Map a schema.org Event to title/start/end/location/url/status; None if unusable."""
    title = _text(node.get("name"))
    start = _text(node.get("startDate"))
    if not title or not start:
        return None
    attendance = str(node.get("eventAttendanceMode", ""))
    location = _location(node.get("location"))
    if "OnlineEventAttendanceMode" in attendance and not location:
        return None  # online-only events are filtered out
    status = str(node.get("eventStatus", ""))
    url = _text(node.get("url"))
    return {
        "title": title[:240],
        "start": start,
        "end": _text(node.get("endDate")),
        "location_summary": location,
        "description": _text(node.get("description")),
        "event_url": urljoin(page_url, url) if url else None,
        "canceled": "EventCancelled" in status,
        "picture": jsonld_image(node, page_url),
    }


def main_text(html: str, max_chars: int = 12000) -> str:
    """Visible text of the page without scripts, styles, navigation, header and footer."""
    soup = soup_of(html)
    # form *controls* only: ASP.NET sites wrap the whole page in one <form>
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "iframe", "input", "select", "button", "textarea", "option"]):
        tag.decompose()
    body = soup.body or soup
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or body
    text = _WS_RE.sub(" ", root.get_text(" ", strip=True))
    if root is not body and len(text) < 500:
        # some sites keep only a banner inside <main> and render the listing elsewhere
        text = _WS_RE.sub(" ", body.get_text(" ", strip=True))
    return text[:max_chars]


def robots_meta(html: str) -> str:
    """Lowercased content of <meta name="robots"> tags (e.g. 'noindex, noai')."""
    soup = soup_of(html)
    values = [m.get("content", "") for m in soup.find_all("meta", attrs={"name": re.compile("^(robots|famloombot)$", re.I)})]
    return ",".join(values).lower()


def site_name(html: str) -> Optional[str]:
    tag = soup_of(html).find("meta", attrs={"property": "og:site_name"})
    return tag.get("content").strip() if tag and tag.get("content") else None


def has_password_form(html: str) -> bool:
    return soup_of(html).find("input", attrs={"type": "password"}) is not None


def find_terms_link(html: str, page_url: str) -> Optional[str]:
    """URL of the site's terms-of-use page, if the page links to one."""
    pattern = re.compile(r"terms|conditions|legal|copyright|acceptable use", re.I)
    for a in soup_of(html).find_all("a", href=True):
        label = f"{a.get_text(' ', strip=True)} {a['href']}"
        if pattern.search(label) and not re.search(r"privacy", a["href"], re.I):
            return urljoin(page_url, a["href"])
    return None


# ---- pictures and per-event links ----------------------------------------------------------------

_NOT_A_PICTURE = re.compile(r"logo|icon|favicon|sprite|placeholder|spacer|pixel|blank|avatar|badge|button|\.svg(\?|$)|/emoji/", re.I)


def clean_image_url(url: Optional[str], base_url: str) -> Optional[str]:
    """Absolute https URL of a real picture, or None for logos, icons, svgs, data: URIs, pixels."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip().split(" ")[0]  # srcset entries look like "url 2x"
    if not url or url.startswith("data:"):
        return None
    absolute = urljoin(base_url, url)
    if not absolute.startswith("https://") or _NOT_A_PICTURE.search(absolute):
        return None
    from src.net.http import host_of, is_blocked_domain

    if is_blocked_domain(host_of(absolute)):
        return None
    return absolute


def jsonld_image(node: Dict[str, Any], base_url: str) -> Optional[str]:
    value = node.get("image")
    candidates = value if isinstance(value, list) else [value]
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl")
        picture = clean_image_url(item, base_url)
        if picture:
            return picture
    return None


def og_image(html: str, base_url: str) -> Optional[str]:
    soup = soup_of(html)
    for attrs in ({"property": "og:image"}, {"property": "og:image:secure_url"}, {"name": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        picture = clean_image_url(tag.get("content") if tag else None, base_url)
        if picture:
            return picture
    return None


def _img_src(img) -> Optional[str]:
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        if img.get(attr):
            return img.get(attr)
    srcset = img.get("srcset") or img.get("data-srcset")
    return srcset.split(",")[0] if srcset else None


def link_cards(html: str, base_url: str) -> List[Dict[str, Optional[str]]]:
    """Every link on the page with its text and the picture of its card (inside the link or up to
    three parent elements), used to give extracted events their own URL and picture."""
    soup = soup_of(html)
    for tag in soup(["nav", "header", "footer", "script", "style"]):
        tag.decompose()
    cards = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = _WS_RE.sub(" ", a.get_text(" ", strip=True))
        img, node = a.find("img"), a
        for _ in range(3):
            if img is not None or node.parent is None:
                break
            node = node.parent
            if len(node.find_all("a", href=True)) > 1:
                break  # a container shared with other links is not this event's card
            img = node.find("img")
        if not text:
            text = (img.get("alt") or "").strip() if img is not None else ""
        if not text:
            continue
        cards.append({"text": text, "href": urljoin(base_url, href),
                      "image": clean_image_url(_img_src(img), base_url) if img is not None else None})
    return cards


def match_card(title: str, cards: List[Dict[str, Optional[str]]], threshold: int = 85) -> Optional[Dict[str, Optional[str]]]:
    """The link card whose text best matches an event title (rapidfuzz token_set_ratio)."""
    from rapidfuzz import fuzz

    best, best_score = None, threshold - 1
    for card in cards:
        score = fuzz.token_set_ratio(title.lower(), card["text"].lower())
        if score > best_score and len(card["text"]) >= 4:
            best, best_score = card, score
    return best
