from pathlib import Path

import yaml

MANIFESTS = Path(__file__).resolve().parents[1] / "k8s-manifests"


def _load(name):
    return yaml.safe_load((MANIFESTS / name).read_text(encoding="utf-8"))


def _env(doc):
    container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_scraper_runs_twice_a_day_in_vancouver_time():
    doc = _load("scraper-cronjob.yaml")
    assert doc["spec"]["schedule"] == "0 5,16 * * *"
    assert doc["spec"]["timeZone"] == "America/Vancouver"
    assert doc["spec"]["concurrencyPolicy"] == "Forbid"
    assert doc["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 5400
    assert _env(doc)["TYPESAFE_MODEL"] == "jev-1.13.0"


def test_janitor_schedule_unchanged_and_classified_removal_off_until_gate3():
    doc = _load("janitor-cronjob.yaml")
    assert doc["spec"]["schedule"] == "0 1 * * *"
    assert _env(doc)["JANITOR_REMOVE_CLASSIFIED"] == "false"


def test_dockerfile_ships_config_directory():
    dockerfile = (MANIFESTS.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY config/ ./config/" in dockerfile


def test_phase2_flags_in_scraper_manifest():
    env = _env(_load("scraper-cronjob.yaml"))
    assert env["CURATED_CALENDARS_ENABLED"] == "true"
    assert env["WEB_SEARCH_ENABLED"] == "true"
    assert env["FACEBOOK_SNIPPETS_ENABLED"] == "false"
    assert env["CRAWLER_CONTACT_EMAIL"] == "miladmyled@gmail.com"
