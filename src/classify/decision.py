import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from src.classify.models import Decision, TaxonomyValue


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Thresholds:
    family_accept: float = 0.70
    family_review: float = 0.40
    # Adult-only content is welcome (couples are families); > 1 disables the adult rejection.
    adult_reject: float = 1.01
    singles_reject: float = 0.60
    tag: float = 0.60
    language: float = 0.60

    @classmethod
    def from_env(cls) -> "Thresholds":
        return cls(
            family_accept=_env_float("FAMILY_ACCEPT_THRESHOLD", 0.70),
            family_review=_env_float("FAMILY_REVIEW_THRESHOLD", 0.40),
            adult_reject=_env_float("ADULT_REJECT_THRESHOLD", 1.01),
            singles_reject=_env_float("SINGLES_REJECT_THRESHOLD", 0.60),
            tag=_env_float("TAG_THRESHOLD", 0.60),
            language=_env_float("LANGUAGE_THRESHOLD", 0.60),
        )


def decide(family: Optional[float], adult: Optional[float], t: Thresholds, singles: Optional[float] = None) -> Decision:
    """
    singles >= singles_reject -> reject (a couple would not attend a dating event together);
    adult >= adult_reject -> reject (disabled by default);
    family >= accept -> accept; family < review -> reject; else review.
    """
    if singles is not None and singles >= t.singles_reject:
        return "reject"
    if adult is not None and adult >= t.adult_reject:
        return "reject"
    if family is None:
        return "review"
    if family >= t.family_accept:
        return "accept"
    if family < t.family_review:
        return "reject"
    return "review"


def combined_family_score(answers: Dict[str, float], keys: Iterable[str]) -> Optional[float]:
    """Family relevance = the strongest of the family / children / kid-welcome / couple answers."""
    values = [answers[k] for k in keys if answers.get(k) is not None]
    return max(values) if values else None


def select_ids(
    values: Iterable[TaxonomyValue], probabilities: Dict[int, float], threshold: float
) -> List[int]:
    """Ids of the offered values whose probability meets the threshold (unknown ids are ignored)."""
    return sorted(v.value_id for v in values if probabilities.get(v.value_id, 0.0) >= threshold)


def review_policy() -> str:
    policy = os.getenv("REVIEW_POLICY", "drop").strip().lower()
    return policy if policy in ("drop", "publish") else "drop"


def is_publishable(decision: Decision, policy: Optional[str] = None) -> bool:
    policy = policy or review_policy()
    return decision == "accept" or (decision == "review" and policy == "publish")
