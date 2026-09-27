from unittest.mock import MagicMock

import pytest

from src.classify.taxonomy import (
    PRIMARY_LANGUAGE_PATH,
    TAG_HINTS_PATH,
    PrimaryLanguageMap,
    get_active_taxonomy,
    load_tag_hints,
    load_taxonomy,
)


def _conn(rows):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
    return conn


ROWS = [
    {"value_id": 45, "code": "interests", "value_code": "hiking", "label": "Hiking"},
    {"value_id": 501, "code": "languages", "value_code": "en", "label": "English"},
    {"value_id": 502, "code": "languages", "value_code": "fr", "label": "French"},
]


def test_load_taxonomy_interests_and_languages_with_hints():
    values = load_taxonomy(_conn(ROWS), hints={"hiking": "hikes, trails"})
    by_id = {v.value_id: v for v in values}
    assert by_id[45].code == "interests" and by_id[45].hint == "hikes, trails"
    assert by_id[502].value_code == "fr" and by_id[502].hint is None


def test_missing_languages_question_returns_interests_only(caplog):
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = _conn(ROWS[:1])
    tax = get_active_taxonomy(pool)
    assert [v.value_id for v in tax.interests] == [45]
    assert tax.languages == []


@pytest.mark.parametrize(
    "city, expected",
    [
        ("Vancouver, BC, Canada", "en"),
        ("North Vancouver, BC, Canada", "en"),
        ("Coquitlam, BC, Canada", "en"),
        ("Montréal, QC, Canada", "fr"),
        ("Laval, Quebec, Canada", "fr"),
        ("Berlin, Germany", "de"),
        ("Rome, Italy", "it"),
        ("Somewhere, Nowhere", None),
    ],
)
def test_primary_language_lookup_from_shipped_config(city, expected):
    assert PrimaryLanguageMap.from_yaml().primary_language_code(city) == expected


def test_languages_for_city_excludes_primary_and_other(taxonomy):
    codes = lambda city: [v.value_code for v in taxonomy.languages_for_city(city)]
    assert codes("Vancouver, BC, Canada") == ["fr", "fa"]
    assert codes("Montreal, QC, Canada") == ["en", "fa"]
    assert codes("Unknown Town, Atlantis") == ["en", "fr", "fa"]  # never guess


def test_taxonomy_hash_changes_with_labels_and_primary_map(taxonomy):
    from dataclasses import replace

    h = taxonomy.hash
    taxonomy.interests[0] = replace(taxonomy.interests[0], label="Hiking & walks")
    assert taxonomy.hash != h
    h2 = taxonomy.hash
    taxonomy.primary_languages = PrimaryLanguageMap({"Vancouver": "fr"}, {}, {})
    assert taxonomy.hash != h2


def test_shipped_config_files_load():
    assert PRIMARY_LANGUAGE_PATH.exists() and TAG_HINTS_PATH.exists()
    hints = load_tag_hints()
    assert hints["cycling"].startswith("biking")
    assert len(hints) >= 90
