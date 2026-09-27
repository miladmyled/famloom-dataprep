import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.classify.cache import ClassificationCache, content_hash
from src.classify.decision import is_publishable, review_policy
from src.classify.factory import ClassifierChain
from src.classify.models import ClassificationResult, ClassifyInput
from src.classify.prompts import PROMPT_VERSION
from src.classify.taxonomy import Taxonomy
from src.models.event import CityEvent

logger = logging.getLogger(__name__)

METRIC_KEYS = (
    "cache_hits",
    "classified",
    "accepted",
    "review",
    "rejected",
    "canceled",
    "classifier_errors",
)


def to_input(event: CityEvent) -> ClassifyInput:
    return ClassifyInput(
        event_id=event.event_id,
        url=str(event.url),
        is_canceled=bool(event.is_canceled),
        title=event.title,
        description=event.description,
        location_summary=event.location_summary,
        source=event.source,
        city=event.city,
        start_date=event.start_date,
    )


@dataclass
class StageOutput:
    publish: List[CityEvent] = field(default_factory=list)
    results: Dict[str, ClassificationResult] = field(default_factory=dict)
    metrics: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))  # per source


def classify_events(
    events: List[CityEvent],
    chain: ClassifierChain,
    cache: ClassificationCache,
    taxonomy: Taxonomy,
    policy: Optional[str] = None,
) -> StageOutput:
    """
    Decide which validated events are published and with which tags.
    - canceled events are never published; they are recorded in the cache (is_canceled) so the
      nightly janitor removes a previously published copy by url;
    - cache hits with the same content_hash are reused; misses go through the classifier chain;
    - when every AI provider fails, a previous (stale) cached result is used if present,
      otherwise the event is not published this run and is retried next run;
    - accepted events (and review with REVIEW_POLICY=publish) are published with
      tag_ids = interests U non-primary languages and replace_tags=True;
    - in keyword-only mode (no AI provider configured) every live event is published as before.
    """
    policy = policy or review_policy()
    out = StageOutput()
    if not events:
        return out

    tax_hash = taxonomy.hash
    inputs = {e.event_id: to_input(e) for e in events}
    hashes = {eid: content_hash(inp, tax_hash) for eid, inp in inputs.items()}
    by_id = {e.event_id: e for e in events}
    cached = cache.get_cached(inputs.keys())

    to_save: List[ClassificationResult] = []
    status_updates = []
    misses: List[ClassifyInput] = []

    for eid, inp in inputs.items():
        m = out.metrics[inp.source]
        prior = cached.get(eid)
        if inp.is_canceled:
            m["canceled"] += 1
            if prior is not None:
                status_updates.append((eid, inp.url, True))
            else:
                to_save.append(_status_only_result(inp, hashes[eid]))
            continue
        if prior is not None and prior.provider != "status" and prior.content_hash == hashes[eid]:
            m["cache_hits"] += 1
            prior.is_canceled = False
            prior.url = inp.url
            status_updates.append((eid, inp.url, False))
            out.results[eid] = prior
        else:
            misses.append(inp)

    outcome = chain.classify(misses, taxonomy)
    for eid, result in outcome.results.items():
        result.content_hash = hashes[eid]
        out.results[eid] = result
        out.metrics[inputs[eid].source]["classified"] += 1
        if result.provider != "keyword":  # keyword results are not cached: AI classifies later
            to_save.append(result)
    for eid in outcome.failed:
        out.metrics[inputs[eid].source]["classifier_errors"] += 1
        prior = cached.get(eid)
        if prior is not None:
            out.results[eid] = prior  # stale but known: better than dropping a known event

    cache.update_status(status_updates)
    cache.save(to_save)

    for eid, result in out.results.items():
        event = by_id[eid]
        m = out.metrics[event.source]
        if chain.keyword_only and result.provider == "keyword":
            m["review"] += 1
            out.publish.append(event.model_copy(update={"tag_ids": result.interest_value_ids, "replace_tags": False}))
            continue
        m[{"accept": "accepted", "review": "review", "reject": "rejected"}[result.decision]] += 1
        if is_publishable(result.decision, policy):
            out.publish.append(event.model_copy(update={"tag_ids": result.tag_ids, "replace_tags": True}))
    return out


def _status_only_result(inp: ClassifyInput, chash: str) -> ClassificationResult:
    """Canceled event without a prior classification: record it so the janitor can remove it."""
    return ClassificationResult(
        event_id=inp.event_id,
        url=inp.url,
        is_canceled=True,
        provider="status",
        model_version="n/a",
        prompt_version=PROMPT_VERSION,
        family_score=None,
        adult_score=None,
        decision="reject",
        interest_value_ids=[],
        language_value_ids=[],
        content_hash=chash,
        source=inp.source,
        city=inp.city,
        title=inp.title,
    )
