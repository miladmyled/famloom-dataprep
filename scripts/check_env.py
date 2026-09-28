"""
Environment check: lists every expected variable as set/missing (never values) and runs one
tiny smoke call per configured provider. Exit code 1 if a required check fails.

    python scripts/check_env.py            # everything that is configured
    python scripts/check_env.py --skip kafka
"""
import argparse
import os
import sys
import time

import _common  # noqa: F401  (loads .env, sets sys.path)

EXPECTED = {
    "Database": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"],
    "Kafka": ["KAFKA_BOOTSTRAP_SERVERS", "KAFKA_TOPIC"],
    "Eventbrite": ["EVENTBRITE_API_TOKEN"],
    "Jev (TypeSafe)": ["TYPESAFE_API_KEY", "TYPESAFE_MODEL"],
    "Gemini": ["GEMINI_API_KEY", "GEMINI_MODEL"],
    "Brave Search (P2)": ["BRAVE_SEARCH_API_KEY", "CRAWLER_CONTACT_EMAIL"],
}


def check_db():
    import psycopg

    with psycopg.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT", "5432"), dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"), sslmode="require", connect_timeout=15,
    ) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.execute("SELECT to_regclass('public.city_event_classifications') IS NOT NULL, to_regclass('public.event_source_sites') IS NOT NULL")
        table, sites_table = cur.fetchone()
        cur.execute("SELECT has_table_privilege(current_user, 'city_event_classifications', 'INSERT')" if table else "SELECT false")
        can_write = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM questions q JOIN question_values v ON v.question_id = q.id "
            "WHERE q.code = 'languages' AND q.is_active AND v.is_active"
        )
        languages = cur.fetchone()[0]
    ok = table and sites_table and can_write and languages > 0
    return ok, (f"{os.getenv('DB_NAME')}: classifications table={'yes' if table else 'MISSING'}, "
                f"source-sites table={'yes' if sites_table else 'MISSING'}, insert={'yes' if can_write else 'no'}, languages={languages}")


def check_jev():
    from typesafe_sdk import Noul, TypeSafeClient

    with TypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"]) as client:
        r = client.system_one(state="Title: Toddler storytime", model=os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
                              questions={"family": Noul(instructions="Families with children would attend this event.")})
    return True, f"model {r.model}"


def check_gemini():
    from src.classify.gemini import GeminiClient

    client = GeminiClient(max_retries=0)
    out = client.generate_json('Return {"ok": true}.', {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    return out.get("ok") is True, f"model {client.model}"


def check_brave():
    import requests

    r = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": "vancouver library storytime", "count": 1, "country": "CA"},
        headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"], "Accept": "application/json"},
        timeout=15,
    )
    return r.status_code == 200, f"HTTP {r.status_code}"


def check_kafka():
    from confluent_kafka.admin import AdminClient

    from src.config.kafka import get_kafka_producer_config, get_kafka_topic

    cfg = {k: v for k, v in get_kafka_producer_config().items() if k in ("bootstrap.servers", "security.protocol", "sasl.mechanisms", "sasl.username", "sasl.password")}
    md = AdminClient(cfg).list_topics(timeout=5)
    topic = get_kafka_topic()
    return True, f"{os.getenv('KAFKA_BOOTSTRAP_SERVERS')}: {len(md.brokers)} broker(s), topic '{topic}' {'exists' if topic in md.topics else 'not created yet'}"


CHECKS = [
    ("db", "Database", ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"], check_db, True),
    ("jev", "Jev", ["TYPESAFE_API_KEY"], check_jev, False),
    ("gemini", "Gemini", ["GEMINI_API_KEY", "GEMINI_MODEL"], check_gemini, False),
    ("brave", "Brave", ["BRAVE_SEARCH_API_KEY"], check_brave, False),
    ("kafka", "Kafka", ["KAFKA_BOOTSTRAP_SERVERS"], check_kafka, False),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip", action="append", default=[], help="check to skip: db, jev, gemini, brave, kafka")
    args = parser.parse_args()

    print("\nVariables (set / missing):")
    for group, names in EXPECTED.items():
        print(f"  {group:<20} " + ", ".join(f"{n}={'set' if os.getenv(n) else 'missing'}" for n in names))

    print("\nSmoke checks:")
    failed_required = False
    for key, label, needs, fn, required in CHECKS:
        if key in args.skip:
            print(f"  {label:<12} SKIP (--skip)")
            continue
        if not all(os.getenv(n) for n in needs):
            print(f"  {label:<12} SKIP (not configured)")
            failed_required |= required
            continue
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as err:
            ok, detail = False, f"{type(err).__name__}: {str(err)[:160]}"
        print(f"  {label:<12} {'PASS' if ok else 'FAIL'}  {detail}  ({time.time() - start:.1f}s)")
        failed_required |= required and not ok
    has_ai = any(os.getenv(n) for n in ("TYPESAFE_API_KEY", "GEMINI_API_KEY"))
    if not has_ai:
        print("\n  WARNING: no AI classifier key set; the pipeline would run in legacy keyword mode.")
    return 1 if failed_required else 0


if __name__ == "__main__":
    sys.exit(main())
