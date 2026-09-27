from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional

Decision = Literal["accept", "review", "reject"]


@dataclass(frozen=True)
class TaxonomyValue:
    value_id: int          # question_values.id
    code: str              # question code: 'interests' | 'languages'
    value_code: str        # question_values.value_code, e.g. 'hiking', 'fr'
    label: str             # question_values.value_label
    hint: Optional[str] = None  # optional extra description from tag_hints.yaml


@dataclass(frozen=True)
class ClassifyInput:
    event_id: str
    url: str               # city_events.url, the event's identity in the database
    is_canceled: bool      # provider status; stored in the cache so the janitor can remove it
    title: str
    description: Optional[str]
    location_summary: Optional[str]
    source: str
    city: str
    start_date: Optional[datetime]


@dataclass
class ClassificationResult:
    event_id: str
    url: str
    is_canceled: bool
    provider: str          # 'jev' | 'gemini' | 'keyword' | 'status'
    model_version: str
    prompt_version: str
    family_score: Optional[float]
    adult_score: Optional[float]
    decision: Decision
    interest_value_ids: List[int]
    language_value_ids: List[int]
    scores: Dict[str, float] = field(default_factory=dict)
    content_hash: str = ""
    source: Optional[str] = None
    city: Optional[str] = None
    title: Optional[str] = None

    @property
    def tag_ids(self) -> List[int]:
        return sorted(set(self.interest_value_ids) | set(self.language_value_ids))


class ProviderUnavailable(Exception):
    """Raised when a classifier cannot be used at all (e.g. missing API key)."""
