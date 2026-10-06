import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from src.classify.decision import Thresholds, combined_family_score, decide
from src.classify.jev import format_when
from src.classify.models import ClassificationResult, ClassifyInput, ProviderUnavailable
from src.classify.prompts import (
    ADULT_QUESTION,
    CHILDREN_QUESTION,
    COUPLE_QUESTION,
    FAMILY_QUESTION,
    FAMILY_QUESTION_KEYS,
    KID_WELCOME_QUESTION,
    PROMPT_VERSION,
    SINGLES_QUESTION,
    build_state_text,
)
from src.classify.taxonomy import Taxonomy

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiQuotaExhausted(Exception):
    """Daily (or otherwise non-retryable) quota reached; remaining work retries next run."""


class GeminiClient:
    """
    Minimal client for models/{model}:generateContent with a JSON schema response.
    Rate-limited to GEMINI_REQUESTS_PER_MINUTE; retries 429/5xx with backoff, honouring
    Retry-After; a per-day quota error raises GeminiQuotaExhausted immediately.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout_seconds: Optional[float] = None,
        requests_per_minute: Optional[float] = None,
        max_retries: Optional[int] = None,
        base_url: str = GEMINI_BASE_URL,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model or os.getenv("GEMINI_MODEL")
        if not self.api_key or not self.model:
            raise ProviderUnavailable("GEMINI_API_KEY or GEMINI_MODEL is not set")
        self.session = session or requests.Session()
        self.timeout = float(timeout_seconds or os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
        rpm = float(requests_per_minute or os.getenv("GEMINI_REQUESTS_PER_MINUTE", "10"))
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self.max_retries = int(max_retries if max_retries is not None else os.getenv("GEMINI_MAX_RETRIES", "3"))
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    @staticmethod
    def _is_daily_quota(response: requests.Response) -> bool:
        text = response.text or ""
        return "PerDay" in text or "per day" in text.lower()

    def generate_json(self, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/models/{self.model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json=body,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as err:
                last_error = err
            else:
                if response.status_code == 200:
                    return self._parse(response.json())
                if response.status_code == 429 and self._is_daily_quota(response):
                    raise GeminiQuotaExhausted("Gemini daily quota reached")
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:300]}")
                last_error = RuntimeError(f"Gemini HTTP {response.status_code}")
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < self.max_retries:
                    time.sleep(min(float(retry_after), 60.0))
                    continue
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt + random.uniform(0, 0.5), 30.0))
        if isinstance(last_error, RuntimeError) and "429" in str(last_error):
            raise GeminiQuotaExhausted(str(last_error))
        raise last_error or RuntimeError("Gemini request failed")

    @staticmethod
    def _parse(payload: Dict[str, Any]) -> Dict[str, Any]:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {str(payload)[:300]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return json.loads(text)


CLASSIFY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "family_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "children_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "kid_welcome_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "couple_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "singles_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "adult_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "interest_value_ids": {"type": "array", "items": {"type": "integer"}},
                    "language_value_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["event_id", "family_score", "children_score", "kid_welcome_score", "couple_score", "singles_score", "adult_score", "interest_value_ids", "language_value_ids"],
            },
        }
    },
    "required": ["events"],
}


class GeminiClassifier:
    """Fallback classifier: up to GEMINI_BATCH_SIZE events per request, same rules as Jev."""

    name = "gemini"

    def __init__(self, client: Optional[GeminiClient] = None, thresholds: Optional[Thresholds] = None, batch_size: Optional[int] = None):
        self.client = client or GeminiClient()
        self.thresholds = thresholds or Thresholds.from_env()
        self.batch_size = max(1, int(batch_size or os.getenv("GEMINI_BATCH_SIZE", "20")))
        self.quota_exhausted = False

    def build_prompt(self, batch: List[ClassifyInput], taxonomy: Taxonomy) -> str:
        interests = "\n".join(
            f"- {v.value_id}: {v.label}" + (f" ({v.hint})" if v.hint else "") for v in taxonomy.interests
        )
        blocks = []
        for inp in batch:
            langs = taxonomy.languages_for_city(inp.city)
            lang_list = ", ".join(f"{v.value_id}: {v.label}" for v in langs) or "none"
            state = build_state_text(inp.title, format_when(inp), inp.location_summary, inp.city, inp.source, inp.description)
            blocks.append(f"### event_id: {inp.event_id}\n{state}\nAllowed language ids: {lang_list}")
        return (
            "You classify city events for a family app. For EACH event return:\n"
            f"- family_score (0..1): probability that this is true: \"{FAMILY_QUESTION}\"\n"
            f"- children_score (0..1): probability that this is true: \"{CHILDREN_QUESTION}\"\n"
            f"- kid_welcome_score (0..1): probability that this is true: \"{KID_WELCOME_QUESTION}\"\n"
            f"- couple_score (0..1): probability that this is true: \"{COUPLE_QUESTION}\"\n"
            f"- singles_score (0..1): probability that this is true: \"{SINGLES_QUESTION}\"\n"
            f"- adult_score (0..1): probability that this is true: \"{ADULT_QUESTION}\"\n"
            "- interest_value_ids: ids from the interest list that the event is clearly about or strongly involves.\n"
            "- language_value_ids: ids from that event's allowed language ids only, when the event is held fully "
            "or partly in that language. Judge only by the language of the event itself, never by culture, cuisine, "
            "country, ethnicity or topic. Leave empty when unsure.\n"
            "Use only ids from the lists. Return every event_id exactly once.\n\n"
            f"Interests:\n{interests}\n\nEvents:\n\n" + "\n\n".join(blocks)
        )

    def to_result(self, inp: ClassifyInput, taxonomy: Taxonomy, item: Dict[str, Any]) -> ClassificationResult:
        def score(key: str) -> Optional[float]:
            try:
                return min(1.0, max(0.0, float(item.get(key))))
            except (TypeError, ValueError):
                return None

        parts = {"family": score("family_score"), "children": score("children_score"), "kid_welcome": score("kid_welcome_score"), "couple": score("couple_score")}
        family, adult = combined_family_score(parts, FAMILY_QUESTION_KEYS), score("adult_score")
        interest_ok = taxonomy.ids("interests")
        language_ok = {v.value_id for v in taxonomy.languages_for_city(inp.city)}
        interests = sorted({int(i) for i in item.get("interest_value_ids") or [] if str(i).lstrip("-").isdigit() and int(i) in interest_ok})
        languages = sorted({int(i) for i in item.get("language_value_ids") or [] if str(i).lstrip("-").isdigit() and int(i) in language_ok})
        return ClassificationResult(
            event_id=inp.event_id,
            url=inp.url,
            is_canceled=inp.is_canceled,
            provider=self.name,
            model_version=self.client.model,
            prompt_version=PROMPT_VERSION,
            family_score=family,
            adult_score=adult,
            decision=decide(family, adult, self.thresholds, score("singles_score")),
            interest_value_ids=interests,
            language_value_ids=languages,
            scores={k: v for k, v in list(parts.items()) + [("adult", adult), ("singles", score("singles_score"))] if v is not None},
            source=inp.source,
            city=inp.city,
            title=inp.title,
        )

    def classify(self, inputs: List[ClassifyInput], taxonomy: Taxonomy) -> Dict[str, ClassificationResult]:
        results: Dict[str, ClassificationResult] = {}
        for start in range(0, len(inputs), self.batch_size):
            if self.quota_exhausted:
                break
            batch = inputs[start : start + self.batch_size]
            by_id = {inp.event_id: inp for inp in batch}
            try:
                payload = self.client.generate_json(self.build_prompt(batch, taxonomy), CLASSIFY_SCHEMA)
            except GeminiQuotaExhausted as err:
                self.quota_exhausted = True
                logger.warning(f"[GEMINI] Quota exhausted ({err}); {len(inputs) - start} event(s) retry next run.")
                break
            except Exception as err:
                logger.warning(f"[GEMINI] Batch of {len(batch)} failed: {type(err).__name__}: {err}")
                continue
            for item in payload.get("events") or []:
                inp = by_id.get(str(item.get("event_id")))
                if inp is not None and inp.event_id not in results:
                    results[inp.event_id] = self.to_result(inp, taxonomy, item)
        logger.info(f"[GEMINI] Classified {len(results)}/{len(inputs)} events.")
        return results


EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_local": {"type": "string", "description": "YYYY-MM-DDTHH:MM in the event's local time"},
                    "end_local": {"type": "string"},
                    "location_summary": {"type": "string"},
                    "description_short": {"type": "string"},
                    "event_url": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["title", "start_local", "location_summary", "confidence"],
            },
        }
    },
    "required": ["events"],
}


class GeminiExtractor:
    """
    Turns free text (a calendar page, a search snippet, a caption) into dated events.
    Rules given to the model: explicit or resolvable dates only, nearest future date inside the
    window when the year is missing, no event without a date and a place, a neutral summary of
    its own (never copied text), online-only events excluded. Local times are converted to UTC here.
    """

    def __init__(self, client: Optional[GeminiClient] = None, window_days: int = 14, max_chars: int = 12000):
        self.client = client or GeminiClient()
        self.window_days = window_days
        self.max_chars = max_chars
        self.quota_exhausted = False

    def build_prompt(self, text: str, reference: datetime, city: str, tz_name: str, source_url: Optional[str]) -> str:
        return (
            "Extract the specific upcoming events described in the text below.\n"
            f"Reference date (today at the source): {reference.strftime('%A %Y-%m-%d')}. City: {city}. Time zone: {tz_name}.\n"
            f"Only events starting between the reference date and {self.window_days} days after it.\n"
            "Rules:\n"
            "- Dates must be explicit or clearly resolvable from the reference date (e.g. 'this Saturday'). "
            "If the year is missing, use the nearest future date inside the window. Never guess a date.\n"
            "- start_local/end_local as YYYY-MM-DDTHH:MM in the local time zone; if no time is given use 00:00 and "
            "lower the confidence.\n"
            "- Require a physical place (venue or address) in or near the city; skip online-only events.\n"
            "- A recurring event: list each date inside the window separately.\n"
            "- description_short: your own neutral one- or two-sentence summary, max 300 characters, never copied "
            "text; do not include names of private individuals.\n"
            "- event_url: the event's own link if present in the text, else empty.\n"
            "- Return an empty list when there is no dated event.\n"
            + (f"Source page: {source_url}\n" if source_url else "")
            + "\nText:\n" + text[: self.max_chars]
        )

    def to_utc(self, value: Optional[str], tz) -> Optional[datetime]:
        if not value:
            return None
        try:
            local = datetime.fromisoformat(value.strip().replace("Z", ""))
        except ValueError:
            return None
        if local.tzinfo is None:
            local = local.replace(tzinfo=tz)
        return local.astimezone(timezone.utc)

    def extract(self, text: str, reference: datetime, city: str, tz, source_url: Optional[str] = None) -> List[Dict[str, Any]]:
        if self.quota_exhausted or not (text or "").strip():
            return []
        try:
            payload = self.client.generate_json(self.build_prompt(text, reference, city, str(tz), source_url), EXTRACT_SCHEMA)
        except GeminiQuotaExhausted as err:
            self.quota_exhausted = True
            logger.warning(f"[GEMINI] Extraction quota exhausted ({err}); remaining pages retry next run.")
            return []
        except Exception as err:
            logger.warning(f"[GEMINI] Extraction failed for {source_url or 'text'}: {type(err).__name__}: {err}")
            return []
        events = []
        for item in payload.get("events") or []:
            start = self.to_utc(item.get("start_local"), tz)
            place = (item.get("location_summary") or "").strip()
            title = (item.get("title") or "").strip()
            if not start or not place or not title:
                continue
            events.append({
                "title": title[:240],
                "start_date": start,
                "end_date": self.to_utc(item.get("end_local"), tz),
                "location_summary": place,
                "description": (item.get("description_short") or "").strip()[:300] or None,
                "event_url": (item.get("event_url") or "").strip() or None,
                "confidence": float(item.get("confidence") or 0),
            })
        return events
