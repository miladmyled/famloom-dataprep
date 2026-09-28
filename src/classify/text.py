import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Remove HTML tags and entities and collapse whitespace; None becomes ''."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", str(text))
    return _WS_RE.sub(" ", html.unescape(no_tags)).strip()


def normalize_for_hash(text: str | None) -> str:
    """strip_html + lowercase: the stable form used in cache keys."""
    return strip_html(text).lower()


def fold(text: str | None) -> str:
    """Case- and accent-insensitive key for place names ('Montréal' -> 'montreal')."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", no_marks).strip().casefold()
