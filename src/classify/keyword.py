import re
from typing import Dict, List, Optional

from src.classify.models import ClassificationResult, ClassifyInput
from src.classify.prompts import PROMPT_VERSION
from src.classify.taxonomy import Taxonomy


def match_interest_tags(title: str, description: Optional[str], interest_mapping: Dict[str, int]) -> List[int]:
    """
    Case-insensitive whole-word match of interest labels in title + description.
    Returns a deduplicated, sorted list of question_value_ids. (Moved unchanged from transformer.py.)
    """
    if not interest_mapping:
        return []
    combined_text = f"{title or ''} {description or ''}"
    matched_ids = set()
    for label, question_value_id in interest_mapping.items():
        clean_label = label.strip()
        if not clean_label:
            continue
        if re.search(rf"\b{re.escape(clean_label)}\b", combined_text, re.IGNORECASE):
            matched_ids.add(question_value_id)
    return sorted(matched_ids)


class KeywordClassifier:
    """
    Last-resort, no-AI tagging: interest tags by label match, no family decision (review),
    no language tags. Used only when no AI provider is configured at all (legacy behaviour).
    """

    name = "keyword"
    model_version = "keyword-v1"

    def classify(self, inputs: List[ClassifyInput], taxonomy: Taxonomy) -> Dict[str, ClassificationResult]:
        mapping = {v.label.lower(): v.value_id for v in taxonomy.interests}
        return {
            inp.event_id: ClassificationResult(
                event_id=inp.event_id,
                url=inp.url,
                is_canceled=inp.is_canceled,
                provider=self.name,
                model_version=self.model_version,
                prompt_version=PROMPT_VERSION,
                family_score=None,
                adult_score=None,
                decision="review",
                interest_value_ids=match_interest_tags(inp.title, inp.description, mapping),
                language_value_ids=[],
                source=inp.source,
                city=inp.city,
                title=inp.title,
            )
            for inp in inputs
        }
