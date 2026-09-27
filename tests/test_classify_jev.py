import json
from pathlib import Path

import httpx2
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

from src.classify.decision import Thresholds, decide
from src.classify.jev import JevClassifier
from src.classify.models import ProviderUnavailable
from src.classify.prompts import DESCRIPTION_MAX_CHARS, PROMPT_VERSION
from tests.conftest import make_input

FIXTURES = Path(__file__).parent / "fixtures" / "typesafe"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(handler, max_retries=0):
    retry = RetryPolicy(max_retries=max_retries, backoff_initial=0.0, backoff_max=0.0, backoff_jitter=0.0)
    return TypeSafeClient(api_key="test-key", transport=httpx2.MockTransport(handler), retry=retry)


def _classifier(handler, max_retries=0, **kw):
    return JevClassifier(client=_client(handler, max_retries), model="jev-1.13.0", thresholds=Thresholds(), **kw)


def test_request_contains_state_and_a_question_per_taxonomy_value(taxonomy):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=_fixture("storytime_persian.json"))

    _classifier(handler).classify([make_input("e1")], taxonomy)

    body = sent[0]
    assert body["model"] == "jev-1.13.0"
    assert body["state"].startswith("Title: Toddler storytime\nWhen: 2026-10-03 17:00 UTC\nWhere: Central Library\n")
    assert "City: Vancouver, BC, Canada" in body["state"]
    keys = set(body["questions"])
    assert {"family", "children", "kid_welcome", "couple", "singles", "adult", "tag_45", "tag_42", "tag_71"} <= keys
    # Vancouver: English is the primary language and 'other' is never asked
    assert "lang_501" not in keys and "lang_519" not in keys
    assert {"lang_502", "lang_503"} <= keys
    assert all(q["type"] == "noul" for q in body["questions"].values())
    assert "hikes, nature walks" in body["questions"]["tag_45"]["instructions"]
    assert "not by culture" in body["questions"]["lang_503"]["instructions"]


def test_montreal_event_is_not_asked_about_french(taxonomy):
    jev = JevClassifier(client=object(), model="m")
    keys = jev.build_questions(make_input(city="Montreal, QC, Canada"), taxonomy).keys()
    assert "lang_502" not in keys and "lang_501" in keys


def test_description_is_truncated_and_html_stripped(taxonomy):
    jev = JevClassifier(client=object(), model="m")
    state = jev.build_state(make_input(description="<p>" + "x" * (DESCRIPTION_MAX_CHARS + 500) + "</p>"))
    desc = state.split("Description: ", 1)[1]
    assert "<p>" not in desc
    assert len(desc) <= DESCRIPTION_MAX_CHARS + 4


def test_response_mapping_accept_with_interest_and_language(taxonomy):
    result = _classifier(lambda r: httpx2.Response(200, json=_fixture("storytime_persian.json"))).classify(
        [make_input("e1")], taxonomy
    )["e1"]
    assert result.provider == "jev" and result.model_version == "jev-1.13.0"
    assert result.prompt_version == PROMPT_VERSION
    assert result.decision == "accept"
    assert result.interest_value_ids == [42]
    assert result.language_value_ids == [503]
    assert result.tag_ids == [42, 503]
    assert result.scores["family"] == 0.93
    assert result.url == "https://example.com/e/e1"


def test_adult_content_does_not_reject_by_default(taxonomy):
    result = _classifier(lambda r: httpx2.Response(200, json=_fixture("wine_bar.json"))).classify(
        [make_input("bar")], taxonomy
    )["bar"]
    assert result.adult_score == 0.86
    assert result.decision == "review"  # family 0.55, adult score stored but not rejecting
    assert result.interest_value_ids == [71]


def test_adult_rejection_can_be_enabled(taxonomy):
    jev = JevClassifier(client=_client(lambda r: httpx2.Response(200, json=_fixture("wine_bar.json"))),
                        model="jev-1.13.0", thresholds=Thresholds(adult_reject=0.6))
    assert jev.classify([make_input("bar")], taxonomy)["bar"].decision == "reject"


@pytest.mark.parametrize(
    "family, adult, expected",
    [(0.95, 0.1, "accept"), (0.70, 0.0, "accept"), (0.69, 0.0, "review"), (0.40, 0.0, "review"),
     (0.39, 0.0, "reject"), (0.99, 0.95, "accept"), (None, 0.0, "review")],
)
def test_decision_thresholds(family, adult, expected):
    assert decide(family, adult, Thresholds()) == expected


@pytest.mark.parametrize("status", [429, 529, 503])
def test_retries_on_rate_limit_and_overload(taxonomy, status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(status, json={"error": "busy"}, headers={"retry-after": "0"})
        return httpx2.Response(200, json=_fixture("storytime_persian.json"))

    results = _classifier(handler, max_retries=2).classify([make_input("e1")], taxonomy)
    assert calls["n"] == 2
    assert "e1" in results


def test_persistent_failure_leaves_event_unclassified(taxonomy):
    results = _classifier(lambda r: httpx2.Response(529, json={"error": "overloaded"}), max_retries=1).classify(
        [make_input("e1"), make_input("e2")], taxonomy
    )
    assert results == {}


def test_timeout_is_a_failure_not_a_crash(taxonomy):
    def handler(request):
        raise httpx2.ReadTimeout("timed out", request=request)

    assert _classifier(handler).classify([make_input("e1")], taxonomy) == {}


def test_missing_answers_counts_as_failure(taxonomy):
    body = {"model": "jev-1.13.0", "answers": {"tag_42": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 1, "output_tokens": 1}}
    assert _classifier(lambda r: httpx2.Response(200, json=body)).classify([make_input("e1")], taxonomy) == {}


def test_missing_key_means_provider_unavailable(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable):
        JevClassifier()


def test_time_budget_skips_remaining_events(taxonomy):
    jev = _classifier(lambda r: httpx2.Response(200, json=_fixture("storytime_persian.json")), time_budget_seconds=1e-9)
    assert jev.classify([make_input("e1"), make_input("e2")], taxonomy) == {}


def test_family_relevance_is_the_strongest_of_family_children_and_kid_welcome(taxonomy):
    body = {
        "model": "jev-1.13.0",
        "answers": {
            "family": {"type": "noul", "noul": 0.30},
            "children": {"type": "noul", "noul": 0.20},
            "kid_welcome": {"type": "noul", "noul": 0.81},
            "adult": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    result = _classifier(lambda r: httpx2.Response(200, json=body)).classify([make_input("walk")], taxonomy)["walk"]
    assert result.family_score == 0.81
    assert result.decision == "accept"
    assert result.scores["kid_welcome"] == 0.81 and result.scores["family"] == 0.30


def test_drop_off_kids_program_accepted_via_children_question(taxonomy):
    body = {
        "model": "jev-1.13.0",
        "answers": {
            "family": {"type": "noul", "noul": 0.25},
            "children": {"type": "noul", "noul": 0.92},
            "kid_welcome": {"type": "noul", "noul": 0.10},
            "adult": {"type": "noul", "noul": 0.01},
        },
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    assert _classifier(lambda r: httpx2.Response(200, json=body)).classify([make_input("camp")], taxonomy)["camp"].decision == "accept"


def test_singles_event_rejected_even_if_couple_friendly(taxonomy):
    body = {
        "model": "jev-1.13.0",
        "answers": {"couple": {"type": "noul", "noul": 0.8}, "singles": {"type": "noul", "noul": 0.95},
                    "adult": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    assert _classifier(lambda r: httpx2.Response(200, json=body)).classify([make_input("speed")], taxonomy)["speed"].decision == "reject"


def test_couple_outing_accepted_even_if_adult(taxonomy):
    body = {
        "model": "jev-1.13.0",
        "answers": {"family": {"type": "noul", "noul": 0.1}, "couple": {"type": "noul", "noul": 0.9},
                    "singles": {"type": "noul", "noul": 0.02}, "adult": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    assert _classifier(lambda r: httpx2.Response(200, json=body)).classify([make_input("paint")], taxonomy)["paint"].decision == "accept"


def test_adult_override_beats_kid_welcome_when_enabled(taxonomy):
    body = {
        "model": "jev-1.13.0",
        "answers": {"kid_welcome": {"type": "noul", "noul": 0.9}, "adult": {"type": "noul", "noul": 0.95}},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    jev = JevClassifier(client=_client(lambda r: httpx2.Response(200, json=body)), model="m", thresholds=Thresholds(adult_reject=0.6))
    assert jev.classify([make_input("pub")], taxonomy)["pub"].decision == "reject"
