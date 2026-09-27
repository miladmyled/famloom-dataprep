from src.classify.cache import content_hash
from src.classify.factory import ClassifierChain
from src.classify.stage import classify_events, to_input
from tests.conftest import FakeCache, FakeProvider, make_event


def _run(events, provider=None, cache=None, taxonomy=None, policy="drop"):
    cache = cache or FakeCache()
    chain = ClassifierChain([provider] if provider else [])
    return classify_events(events, chain, cache, taxonomy, policy), cache


def test_accepted_published_with_union_of_tags_and_replace_flag(taxonomy):
    provider = FakeProvider("jev", {"e1": {"interests": [42], "languages": [503]}})
    out, cache = _run([make_event("e1")], provider, taxonomy=taxonomy)
    assert [e.event_id for e in out.publish] == ["e1"]
    assert out.publish[0].tag_ids == [42, 503]
    assert out.publish[0].replace_tags is True
    assert cache.saved[0].url == "https://example.com/e/e1"
    assert out.metrics["Eventbrite"]["accepted"] == 1


def test_rejected_not_published_but_cached_with_url(taxonomy):
    provider = FakeProvider("jev", {"bar": {"decision": "reject", "family": 0.1, "adult": 0.9}})
    out, cache = _run([make_event("bar", title="Wine bar night")], provider, taxonomy=taxonomy)
    assert out.publish == []
    assert cache.rows["bar"].decision == "reject"
    assert cache.rows["bar"].url == "https://example.com/e/bar"


def test_review_band_follows_policy(taxonomy):
    provider = FakeProvider("jev", {"e1": {"decision": "review", "family": 0.5}})
    assert _run([make_event("e1")], provider, taxonomy=taxonomy, policy="drop")[0].publish == []
    assert len(_run([make_event("e1")], provider, taxonomy=taxonomy, policy="publish")[0].publish) == 1


def test_canceled_never_published_and_recorded_for_janitor(taxonomy):
    provider = FakeProvider("jev")
    out, cache = _run([make_event("c1", status="canceled", is_canceled=True)], provider, taxonomy=taxonomy)
    assert out.publish == []
    assert provider.calls == []
    assert cache.rows["c1"].is_canceled is True
    assert cache.rows["c1"].provider == "status"
    assert out.metrics["Eventbrite"]["canceled"] == 1


def test_canceled_event_with_prior_classification_only_updates_status(taxonomy):
    first, cache = _run([make_event("e1")], FakeProvider("jev"), taxonomy=taxonomy)
    out, cache = _run([make_event("e1", is_canceled=True, status="canceled")], FakeProvider("jev"), cache=cache, taxonomy=taxonomy)
    assert out.publish == []
    assert ("e1", "https://example.com/e/e1", True) in cache.status_updates


def test_reinstated_event_is_reclassified_not_stuck_as_status_row(taxonomy):
    _, cache = _run([make_event("e1", is_canceled=True, status="canceled")], FakeProvider("jev"), taxonomy=taxonomy)
    provider = FakeProvider("jev")
    out, _ = _run([make_event("e1")], provider, cache=cache, taxonomy=taxonomy)
    assert provider.calls == [["e1"]]
    assert len(out.publish) == 1


def test_cache_hit_skips_classifier(taxonomy):
    event = make_event("e1")
    _, cache = _run([event], FakeProvider("jev"), taxonomy=taxonomy)
    provider = FakeProvider("jev")
    out, _ = _run([event], provider, cache=cache, taxonomy=taxonomy)
    assert provider.calls == []  # nothing to classify: provider not called
    assert out.metrics["Eventbrite"]["cache_hits"] == 1
    assert len(out.publish) == 1


def test_changed_content_is_reclassified(taxonomy):
    _, cache = _run([make_event("e1")], FakeProvider("jev"), taxonomy=taxonomy)
    provider = FakeProvider("jev", {"e1": {"decision": "reject"}})
    out, _ = _run([make_event("e1", title="Now a 19+ pub crawl")], provider, cache=cache, taxonomy=taxonomy)
    assert provider.calls == [["e1"]]
    assert out.publish == [] and cache.rows["e1"].decision == "reject"


def test_all_providers_failed_new_event_not_published(taxonomy):
    out, cache = _run([make_event("e1")], FakeProvider("jev", fail={"e1"}), taxonomy=taxonomy)
    assert out.publish == []
    assert out.metrics["Eventbrite"]["classifier_errors"] == 1
    assert cache.saved == []


def test_all_providers_failed_known_event_uses_stale_cache(taxonomy):
    _, cache = _run([make_event("e1")], FakeProvider("jev", {"e1": {"interests": [45]}}), taxonomy=taxonomy)
    out, _ = _run([make_event("e1", title="Updated title")], FakeProvider("jev", fail={"e1"}), cache=cache, taxonomy=taxonomy)
    assert [e.tag_ids for e in out.publish] == [[45]]


def test_keyword_only_mode_publishes_like_before_without_caching(taxonomy):
    out, cache = _run([make_event("e1", title="Family hiking day")], provider=None, taxonomy=taxonomy)
    assert len(out.publish) == 1
    assert out.publish[0].tag_ids == [45]
    assert out.publish[0].replace_tags is False
    assert cache.saved == []


def test_english_never_tagged_in_vancouver_french_tagged(taxonomy):
    """The real classifiers never offer the primary language; guard the stage input city too."""
    from src.classify.jev import JevClassifier

    keys = JevClassifier(client=object(), model="m").build_questions(to_input(make_event("e1")), taxonomy).keys()
    assert "lang_501" not in keys and "lang_502" in keys


def test_content_hash_saved_on_results(taxonomy):
    event = make_event("e1")
    _, cache = _run([event], FakeProvider("jev"), taxonomy=taxonomy)
    assert cache.rows["e1"].content_hash == content_hash(to_input(event), taxonomy.hash, primary_language="en")


def test_cache_hit_applies_current_thresholds_and_saves_change(taxonomy):
    from src.classify.decision import Thresholds

    event = make_event("e1")
    provider = FakeProvider("jev", {"e1": {"decision": "review", "family": 0.55, "interests": [42]}})
    _, cache = _run([event], provider, taxonomy=taxonomy)
    cache.rows["e1"].scores = {"family": 0.55, "adult": 0.1, "tag_42": 0.65, "tag_45": 0.95, "lang_503": 0.9}
    out = classify_events([event], ClassifierChain([FakeProvider("jev")]), cache, taxonomy, "drop",
                          thresholds=Thresholds(family_accept=0.50, tag=0.70))
    assert [e.event_id for e in out.publish] == ["e1"]
    assert out.publish[0].tag_ids == [45, 503]          # 42 (0.65) dropped at 0.70, 45 added
    assert cache.rows["e1"].decision == "accept"         # written back for the janitor
