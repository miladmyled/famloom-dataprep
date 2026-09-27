from src.classify.factory import ClassifierChain, get_classifier
from tests.conftest import FakeProvider, make_input


def test_fallback_order_only_sends_misses_to_next_provider(taxonomy):
    jev = FakeProvider("jev", fail={"e2"})
    gemini = FakeProvider("gemini")
    outcome = ClassifierChain([jev, gemini]).classify([make_input("e1"), make_input("e2")], taxonomy)

    assert jev.calls == [["e1", "e2"]]
    assert gemini.calls == [["e2"]]
    assert outcome.results["e1"].provider == "jev"
    assert outcome.results["e2"].provider == "gemini"
    assert outcome.failed == []
    assert outcome.by_provider == {"jev": 1, "gemini": 1}


def test_all_providers_failed_is_reported(taxonomy):
    outcome = ClassifierChain([FakeProvider("jev", fail={"e1"}), FakeProvider("gemini", fail={"e1"})]).classify(
        [make_input("e1")], taxonomy
    )
    assert outcome.results == {}
    assert outcome.failed == ["e1"]


def test_provider_exception_falls_through(taxonomy):
    class Boom:
        name = "jev"

        def classify(self, inputs, taxonomy):
            raise RuntimeError("down")

    outcome = ClassifierChain([Boom(), FakeProvider("gemini")]).classify([make_input("e1")], taxonomy)
    assert outcome.results["e1"].provider == "gemini"


def test_keyword_only_mode_when_no_ai_provider(taxonomy):
    chain = ClassifierChain([])
    assert chain.keyword_only
    outcome = chain.classify([make_input("e1", title="Family hiking day", description="")], taxonomy)
    result = outcome.results["e1"]
    assert result.provider == "keyword"
    assert result.decision == "review" and result.family_score is None
    assert result.interest_value_ids == [45]
    assert result.language_value_ids == []


def test_factory_without_keys_is_keyword_only(monkeypatch):
    for name in ("TYPESAFE_API_KEY", "GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert get_classifier().keyword_only


def test_factory_fallback_none(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("CLASSIFIER_FALLBACK", "none")
    assert get_classifier().names == ["jev"]
