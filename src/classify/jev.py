import concurrent.futures
import logging
import os
import time
from typing import Dict, List, Optional

from src.classify.decision import Thresholds, decide, select_ids
from src.classify.models import ClassificationResult, ClassifyInput, ProviderUnavailable
from src.classify.prompts import (
    ADULT_QUESTION,
    FAMILY_QUESTION,
    PROMPT_VERSION,
    build_state_text,
    interest_question,
    language_question,
)
from src.classify.taxonomy import Taxonomy

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "jev-1.13.0"


def format_when(inp: ClassifyInput) -> Optional[str]:
    return inp.start_date.strftime("%Y-%m-%d %H:%M UTC") if inp.start_date else None


class JevClassifier:
    """
    Classifies one event per request with TypeSafe Jev (Noul questions):
    family, adult, one question per interest and one per taggable language of the city.
    Events that fail (after the SDK's retries) are simply absent from the returned dict.
    """

    name = "jev"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        client=None,
        thresholds: Optional[Thresholds] = None,
        max_concurrency: Optional[int] = None,
        time_budget_seconds: Optional[float] = None,
        request_timeout_seconds: Optional[float] = None,
    ):
        self.model = model or os.getenv("TYPESAFE_MODEL") or DEFAULT_MODEL
        self.thresholds = thresholds or Thresholds.from_env()
        self.max_concurrency = max(1, int(max_concurrency or os.getenv("CLASSIFIER_MAX_CONCURRENCY", "4")))
        self.time_budget_seconds = float(time_budget_seconds or os.getenv("CLASSIFIER_TIME_BUDGET_SECONDS", "900"))
        self.request_timeout_seconds = float(request_timeout_seconds or os.getenv("TYPESAFE_TIMEOUT_SECONDS", "30"))
        if client is None:
            api_key = api_key or os.getenv("TYPESAFE_API_KEY")
            if not api_key:
                raise ProviderUnavailable("TYPESAFE_API_KEY is not set")
            client = self._build_client(api_key)
        self.client = client

    def _build_client(self, api_key: str):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        retry = RetryPolicy(
            max_retries=int(os.getenv("TYPESAFE_MAX_RETRIES", "3")),
            backoff_initial=1.0,
            backoff_max=20.0,
            respect_retry_after=True,
            timeout=self.request_timeout_seconds,
        )
        return TypeSafeClient(api_key=api_key, model=self.model, retry=retry, timeout=self.request_timeout_seconds)

    # ---- request building -------------------------------------------------------------------

    def build_state(self, inp: ClassifyInput) -> str:
        return build_state_text(
            title=inp.title,
            when=format_when(inp),
            where=inp.location_summary,
            city=inp.city,
            source=inp.source,
            description=inp.description,
        )

    def build_questions(self, inp: ClassifyInput, taxonomy: Taxonomy) -> Dict[str, object]:
        from typesafe_sdk import Noul

        questions: Dict[str, object] = {
            "family": Noul(instructions=FAMILY_QUESTION),
            "adult": Noul(instructions=ADULT_QUESTION),
        }
        for v in taxonomy.interests:
            questions[f"tag_{v.value_id}"] = Noul(instructions=interest_question(v.label, v.hint))
        for v in taxonomy.languages_for_city(inp.city):
            questions[f"lang_{v.value_id}"] = Noul(instructions=language_question(v.label))
        return questions

    # ---- response mapping -------------------------------------------------------------------

    def to_result(self, inp: ClassifyInput, taxonomy: Taxonomy, answers: Dict[str, float], model_version: str) -> ClassificationResult:
        family = answers.get("family")
        adult = answers.get("adult")
        interest_probs = {v.value_id: answers.get(f"tag_{v.value_id}", 0.0) for v in taxonomy.interests}
        offered_languages = taxonomy.languages_for_city(inp.city)
        language_probs = {v.value_id: answers.get(f"lang_{v.value_id}", 0.0) for v in offered_languages}
        return ClassificationResult(
            event_id=inp.event_id,
            url=inp.url,
            is_canceled=inp.is_canceled,
            provider=self.name,
            model_version=model_version or self.model,
            prompt_version=PROMPT_VERSION,
            family_score=family,
            adult_score=adult,
            decision=decide(family, adult, self.thresholds),
            interest_value_ids=select_ids(taxonomy.interests, interest_probs, self.thresholds.tag),
            language_value_ids=select_ids(offered_languages, language_probs, self.thresholds.language),
            scores={k: round(float(p), 4) for k, p in answers.items()},
            source=inp.source,
            city=inp.city,
            title=inp.title,
        )

    @staticmethod
    def extract_answers(response) -> Dict[str, float]:
        answers = getattr(response, "answers", None) or {}
        out: Dict[str, float] = {}
        for key, answer in answers.items():
            value = getattr(answer, "noul", None)
            if value is None and isinstance(answer, dict):
                value = answer.get("noul")
            if value is not None:
                out[key] = float(value)
        return out

    def classify_one(self, inp: ClassifyInput, taxonomy: Taxonomy) -> ClassificationResult:
        response = self.client.system_one(
            state=self.build_state(inp),
            questions=self.build_questions(inp, taxonomy),
            model=self.model,
        )
        answers = self.extract_answers(response)
        if "family" not in answers or "adult" not in answers:
            raise ValueError(f"Jev response for {inp.event_id} is missing family/adult answers")
        return self.to_result(inp, taxonomy, answers, getattr(response, "model", None) or self.model)

    # ---- batch --------------------------------------------------------------------------------

    def classify(self, inputs: List[ClassifyInput], taxonomy: Taxonomy) -> Dict[str, ClassificationResult]:
        results: Dict[str, ClassificationResult] = {}
        if not inputs:
            return results
        deadline = time.monotonic() + self.time_budget_seconds
        errors = 0
        skipped = 0

        def run(inp: ClassifyInput) -> Optional[ClassificationResult]:
            if time.monotonic() > deadline:
                return None  # budget exhausted: the event is retried next run
            return self.classify_one(inp, taxonomy)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futures = {pool.submit(run, inp): inp for inp in inputs}
            for future in concurrent.futures.as_completed(futures):
                inp = futures[future]
                try:
                    result = future.result()
                except Exception as err:
                    errors += 1
                    logger.warning(f"[JEV] Classification failed for {inp.event_id}: {type(err).__name__}: {err}")
                    continue
                if result is None:
                    skipped += 1
                else:
                    results[inp.event_id] = result
        if skipped:
            logger.warning(f"[JEV] Per-run time budget reached; {skipped} event(s) retry next run.")
        logger.info(f"[JEV] Classified {len(results)}/{len(inputs)} events ({errors} errors, {skipped} skipped).")
        return results
