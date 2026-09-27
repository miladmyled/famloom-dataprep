from dataclasses import replace
from unittest.mock import MagicMock

from psycopg import errors as pg_errors

from src.classify.cache import ClassificationCache, content_hash
from src.classify.models import ClassificationResult
from tests.conftest import make_input


def test_hash_is_stable_and_ignores_html_case_and_whitespace():
    a = make_input(description="<b>Stories</b>   and songs")
    b = make_input(description="stories and SONGS")
    assert content_hash(a, "t1") == content_hash(b, "t1")


def test_hash_changes_with_title_date_city_prompt_and_taxonomy():
    base = make_input()
    h = content_hash(base, "t1")
    assert content_hash(replace(base, title="Other"), "t1") != h
    assert content_hash(base, "t1", primary_language="fr") != content_hash(base, "t1", primary_language="en")
    assert content_hash(replace(base, start_date=None), "t1") != h
    assert content_hash(base, "t2") != h
    assert content_hash(base, "t1", prompt_version="other") != h


def test_hash_ignores_cancellation_and_url():
    base = make_input()
    assert content_hash(replace(base, is_canceled=True, url="https://x"), "t") == content_hash(base, "t")


def _result():
    return ClassificationResult(
        event_id="e1", url="https://x/1", is_canceled=False, provider="jev", model_version="m",
        prompt_version="p", family_score=0.9, adult_score=0.0, decision="accept",
        interest_value_ids=[42], language_value_ids=[503], scores={"family": 0.9}, content_hash="h",
    )


def test_missing_table_disables_cache_without_raising():
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute.side_effect = (
        pg_errors.UndefinedTable("relation does not exist")
    )
    cache = ClassificationCache(pool)
    assert cache.get_cached(["e1"]) == {}
    assert cache.enabled is False
    assert cache.save([_result()]) == 0  # no further DB calls once disabled


def test_save_upserts_on_event_id_and_never_runs_ddl():
    pool = MagicMock()
    cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    assert ClassificationCache(pool).save([_result()]) == 1
    sql, rows = cursor.executemany.call_args[0]
    assert "INSERT INTO city_event_classifications" in sql
    assert "ON CONFLICT (event_id) DO UPDATE" in sql
    assert "CREATE" not in sql.upper()
    assert rows[0]["url"] == "https://x/1" and rows[0]["interest_value_ids"] == [42]


def test_row_roundtrip():
    pool = MagicMock()
    cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{
        "event_id": "e1", "url": "https://x/1", "is_canceled": False, "content_hash": "h", "provider": "jev",
        "model_version": "m", "prompt_version": "p", "family_score": 0.9, "adult_score": 0.0,
        "decision": "accept", "interest_value_ids": [42], "language_value_ids": [503],
        "scores": '{"family": 0.9}', "source": "Eventbrite", "city": "Vancouver", "title": "t",
    }]
    got = ClassificationCache(pool).get_cached(["e1"])["e1"]
    assert got.tag_ids == [42, 503] and got.scores == {"family": 0.9}


def test_same_event_in_two_cities_with_same_primary_language_shares_the_key():
    a = make_input(city="Vancouver, BC, Canada")
    b = make_input(city="Coquitlam, BC, Canada")
    assert content_hash(a, "t", primary_language="en") == content_hash(b, "t", primary_language="en")
