"""
Question wording shared by every classifier. Bump PROMPT_VERSION whenever any wording changes:
it is part of the cache key, so all events are re-classified with the new wording.
"""

from src.classify.text import strip_html

PROMPT_VERSION = "2026-09-27.2"

# An event is family-relevant when ANY of these three is true (option B, decided 2026-09-27):
# a family outing, a program for children (including drop-off), or a public event where
# bringing children is reasonable. The adult question still rejects regardless.
FAMILY_QUESTION = (
    "Families with children would attend this event together, and it is suitable and "
    "appealing for children."
)

CHILDREN_QUESTION = (
    "The event is designed for children or teens, for example a kids' class, camp, club, "
    "storytime, show or drop-off program."
)

KID_WELCOME_QUESTION = (
    "This is a public event where children are welcome and it would be reasonable for parents "
    "to bring them, for example an open community walk or run, market, festival, parade, "
    "exhibition, sports game or community day. Not a social meetup organised for adults."
)

FAMILY_QUESTION_KEYS = ("family", "children", "kid_welcome")

ADULT_QUESTION = (
    "The event is intended for adults only (for example 18+/19+, bar or nightclub event, "
    "alcohol-focused, dating or singles event, or explicit content)."
)


def interest_question(label: str, hint: str | None) -> str:
    detail = f" ({hint})" if hint else ""
    return f"The event is clearly about or strongly involves {label}{detail}."


def language_question(label: str) -> str:
    return (
        f"The event is held fully or partly in {label}. Judge only by the language of the event "
        "itself, not by culture, cuisine, country or topic."
    )


DESCRIPTION_MAX_CHARS = 3000


def build_state_text(
    title: str,
    when: str | None,
    where: str | None,
    city: str,
    source: str,
    description: str | None,
) -> str:
    """Compact, stable text representation of an event sent to the classifiers."""
    desc = strip_html(description)
    if len(desc) > DESCRIPTION_MAX_CHARS:
        desc = desc[:DESCRIPTION_MAX_CHARS].rstrip() + " ..."
    lines = [
        f"Title: {title}",
        f"When: {when or 'unknown'}",
        f"Where: {where or 'unknown'}",
        f"City: {city}",
        f"Source: {source}",
        f"Description: {desc or 'none'}",
    ]
    return "\n".join(lines)
