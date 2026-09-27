"""
Yes/no screening of free text with Jev (Noul questions), used before extraction ("does this page
list dated events?") and for web-discovery approval. One request per text, several questions.
"""
import logging
import os
from typing import Dict, Optional

from src.classify.models import ProviderUnavailable

logger = logging.getLogger(__name__)

# Screening questions (part of what PROMPT_VERSION covers; wording changes are cheap to re-run)
PAGE_LISTS_EVENTS = "This page lists one or more specific upcoming events with dates."
SNIPPET_IS_EVENT = "This text describes a specific upcoming event with a date."
POST_ANNOUNCES_EVENT = "This post announces a specific upcoming event or meetup with a date."


class JevScreener:
    def __init__(self, client=None, model: Optional[str] = None, max_chars: int = 12000):
        self.model = model or os.getenv("TYPESAFE_MODEL") or "jev-1.13.0"
        self.max_chars = max_chars
        if client is None:
            api_key = os.getenv("TYPESAFE_API_KEY")
            if not api_key:
                raise ProviderUnavailable("TYPESAFE_API_KEY is not set")
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            client = TypeSafeClient(api_key=api_key, model=self.model,
                                    retry=RetryPolicy(max_retries=3, backoff_initial=1.0, backoff_max=20.0), timeout=30.0)
        self.client = client

    def ask(self, text: str, questions: Dict[str, str]) -> Dict[str, float]:
        """Probability (0..1) per question key; {} when the request fails (caller treats as 'no')."""
        from typesafe_sdk import Noul

        if not (text or "").strip():
            return {}
        try:
            response = self.client.system_one(
                state=text[: self.max_chars],
                questions={k: Noul(instructions=q) for k, q in questions.items()},
                model=self.model,
            )
        except Exception as err:
            logger.warning(f"[SCREEN] Jev screening failed: {type(err).__name__}: {err}")
            return {}
        out = {}
        for key, answer in (getattr(response, "answers", None) or {}).items():
            value = getattr(answer, "noul", None)
            if value is not None:
                out[key] = float(value)
        return out

    def passes(self, text: str, question: str, threshold: float = 0.5) -> bool:
        return self.ask(text, {"q": question}).get("q", 0.0) >= threshold


def get_screener() -> Optional[JevScreener]:
    try:
        return JevScreener()
    except ProviderUnavailable as err:
        logger.warning(f"[SCREEN] Jev screener unavailable: {err}")
        return None
