import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.classify.keyword import KeywordClassifier
from src.classify.models import ClassificationResult, ClassifyInput, ProviderUnavailable
from src.classify.taxonomy import Taxonomy

logger = logging.getLogger(__name__)


@dataclass
class ChainOutcome:
    results: Dict[str, ClassificationResult] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)          # every AI provider failed
    by_provider: Dict[str, int] = field(default_factory=dict)


class ClassifierChain:
    """
    Runs AI providers in order; events a provider could not classify go to the next one.
    Events no AI provider classified are reported in `failed` (they are not published as new
    events this run; see the pipeline). With no AI provider configured at all, the chain is in
    keyword_only mode: legacy keyword tagging, published as before.
    """

    def __init__(self, providers: List[object], keyword: Optional[KeywordClassifier] = None):
        self.providers = providers
        self.keyword = keyword or KeywordClassifier()

    @property
    def keyword_only(self) -> bool:
        return not self.providers

    @property
    def names(self) -> List[str]:
        return [p.name for p in self.providers] or [self.keyword.name]

    def classify(self, inputs: List[ClassifyInput], taxonomy: Taxonomy) -> ChainOutcome:
        outcome = ChainOutcome()
        if not inputs:
            return outcome
        if self.keyword_only:
            outcome.results = self.keyword.classify(inputs, taxonomy)
            outcome.by_provider[self.keyword.name] = len(outcome.results)
            return outcome
        pending = list(inputs)
        for provider in self.providers:
            if not pending:
                break
            try:
                got = provider.classify(pending, taxonomy)
            except Exception as err:
                logger.warning(f"[CLASSIFY] Provider '{provider.name}' failed for the whole batch: {err}")
                got = {}
            outcome.results.update(got)
            outcome.by_provider[provider.name] = outcome.by_provider.get(provider.name, 0) + len(got)
            pending = [inp for inp in pending if inp.event_id not in got]
        outcome.failed = [inp.event_id for inp in pending]
        return outcome


def _build(name: str):
    name = name.strip().lower()
    if name in ("", "none"):
        return None
    if name == "jev":
        from src.classify.jev import JevClassifier

        return JevClassifier()
    if name == "gemini":
        from src.classify.gemini import GeminiClassifier

        return GeminiClassifier()
    if name == "keyword":
        return None
    raise ValueError(f"Unknown classifier provider '{name}'")


def get_classifier() -> ClassifierChain:
    """CLASSIFIER_PROVIDER (default jev) then CLASSIFIER_FALLBACK (default gemini, or none)."""
    providers = []
    for env_name, default in (("CLASSIFIER_PROVIDER", "jev"), ("CLASSIFIER_FALLBACK", "gemini")):
        name = os.getenv(env_name, default)
        try:
            provider = _build(name)
        except ProviderUnavailable as err:
            logger.warning(f"[CLASSIFY] {env_name}={name} unavailable: {err}")
            continue
        if provider is not None and provider.name not in [p.name for p in providers]:
            providers.append(provider)
    chain = ClassifierChain(providers)
    if chain.keyword_only:
        logger.error(
            "[CLASSIFY] No AI classifier configured: running in legacy keyword mode "
            "(no family filter, no language tags). Set TYPESAFE_API_KEY and/or GEMINI_API_KEY."
        )
    else:
        logger.info(f"[CLASSIFY] Classifier chain: {' -> '.join(chain.names)}")
    return chain
