"""
Dev end-to-end run of the scraper flow for chosen cities and sources.

    python scripts/run_pipeline_dev.py --city "Vancouver, BC, Canada" --sources eventbrite,meetup --no-publish
    python scripts/run_pipeline_dev.py --all-cities --publish          # dev/local Kafka only

--no-publish (default): scrape, validate, de-duplicate and classify, then print a summary and
  sample decisions and write reports/pipeline_<ts>.csv. Classifications ARE saved to the dev
  cache (city_event_classifications) unless --no-cache-write is given; nothing is sent to Kafka.
--publish: additionally publishes accepted events to KAFKA_BOOTSTRAP_SERVERS, which must be an
  approved dev/local broker (see scripts/_common.py). Refuses the production database.
--publish-direct: no broker needed. Each accepted event is serialized exactly like the Kafka
  message, parsed back like the consumer does, and written to the dev database with the
  consumer's upsert (city_events + managed tag replacement), one transaction per city.
Replaces scripts/test_pipeline.py for dev runs (that file still works as a quick smoke test).
"""
import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import _common

from src.classify.cache import ClassificationCache
from src.classify.factory import get_classifier
from src.classify.stage import classify_events
from src.classify.taxonomy import get_active_taxonomy
from src.config.database import get_db_pool
from src.etl.dedupe import dedupe_events, load_existing_events
from src.etl.transformer import clean_and_validate_event

# --sources value -> source kind used by main.build_scraper_tasks
SOURCES = {"eventbrite": "eventbrite", "meetup": "meetup", "curated": "curated", "web": "web", "facebook": "facebook_snippet", "instagram": "instagram", "official": "official"}
FLAG_FOR = {"curated": "CURATED_CALENDARS_ENABLED", "web": "WEB_SEARCH_ENABLED", "facebook": "FACEBOOK_SNIPPETS_ENABLED", "instagram": "INSTAGRAM_ENABLED", "official": "OFFICIAL_SITES_ENABLED"}


class _NoWriteCache(ClassificationCache):
    def save(self, results):
        return 0

    def update_status(self, items):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", action="append", default=[], help="city exactly as in family_profiles.location (repeatable)")
    parser.add_argument("--all-cities", action="store_true", help="all distinct family_profiles.location values")
    parser.add_argument("--sources", default="eventbrite,meetup", help=f"comma list of: {', '.join(SOURCES)}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-publish", action="store_true", default=True)
    mode.add_argument("--publish", action="store_true")
    mode.add_argument("--publish-direct", action="store_true", help="write accepted events to the dev DB via the consumer code path, no Kafka")
    parser.add_argument("--no-cache-write", action="store_true", help="do not save classifications to the dev cache")
    parser.add_argument("--samples", type=int, default=20, help="sample decisions to print")
    args = parser.parse_args()

    _common.require_dev_db()
    if args.publish:
        _common.require_dev_kafka()

    pool = get_db_pool()
    try:
        if args.all_cities:
            from src.etl.extractor import get_active_cities

            cities = get_active_cities()
        else:
            cities = args.city
        if not cities:
            parser.error("give --city or --all-cities")
        sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
        unknown = [s for s in sources if s not in SOURCES]
        if unknown:
            parser.error(f"unknown sources: {unknown}")

        # the requested sources are switched on for this run only; everything else off
        import os

        for key, flag in FLAG_FOR.items():
            os.environ[flag] = "true" if key in sources else "false"
        from main import SharedWebClients, build_scraper_tasks

        shared = SharedWebClients(pool)
        wanted = {SOURCES[s] for s in sources}
        taxonomy = get_active_taxonomy(pool)
        chain = get_classifier()
        cache = (_NoWriteCache if args.no_cache_write else ClassificationCache)(pool)
        labels = {v.value_id: v.label for v in taxonomy.values}

        producer = None
        if args.publish:
            from src.etl.kafka_producer import EventKafkaProducer

            producer = EventKafkaProducer()

        rows, metrics = [], defaultdict(Counter)
        for city in cities:
            events = []
            for _name, kind, scraper in build_scraper_tasks(city, shared):
                if kind not in wanted:
                    continue
                raw = scraper.fetch_raw_events()
                valid = [e for e in (clean_and_validate_event(r) for r in scraper.normalize_data(raw)) if e is not None]
                valid = [e if e.origin else e.model_copy(update={"origin": kind}) for e in valid]
                metrics[kind]["raw"] += len(raw)
                metrics[kind]["valid"] += len(valid)
                for key, value in (getattr(scraper, "metrics", None) or {}).items():
                    metrics[kind][f"src:{key}"] += value
                events.extend(valid)
            events, dups = dedupe_events(events, load_existing_events(pool, city))
            for src, n in dups.items():
                metrics[src]["duplicates"] += n
            stage = classify_events(events, chain, cache, taxonomy)
            for src, counter in stage.metrics.items():
                metrics[src].update(counter)
            published = {e.event_id for e in stage.publish}
            if producer:
                for event in stage.publish:
                    if producer.publish_event(event):
                        metrics[event.origin or event.source]["queued"] += 1
            if args.publish_direct and stage.publish:
                from src.db.events import upsert_city_event
                from src.models.event import CityEvent

                with pool.connection() as conn:
                    with conn.transaction():
                        for event in stage.publish:
                            message = event.model_dump_json()  # the Kafka payload
                            upsert_city_event(conn, CityEvent.model_validate_json(message))
                            metrics[event.origin or event.source]["queued"] += 1
            for event in events:
                r = stage.results.get(event.event_id)
                rows.append({
                    "city": city,
                    "source": event.source,
                    "event_id": event.event_id,
                    "title": event.title,
                    "date": event.start_date.isoformat(),
                    "url": str(event.url),
                    "canceled": event.is_canceled,
                    "provider": r.provider if r else "",
                    "family_score": f"{r.family_score:.2f}" if r and r.family_score is not None else "",
                    "adult_score": f"{r.adult_score:.2f}" if r and r.adult_score is not None else "",
                    "decision": r.decision if r else ("canceled" if event.is_canceled else "unclassified"),
                    "published": event.event_id in published,
                    "interests": "; ".join(labels.get(i, str(i)) for i in (r.interest_value_ids if r else [])),
                    "languages": "; ".join(labels.get(i, str(i)) for i in (r.language_value_ids if r else [])),
                })
        if producer:
            producer.flush(timeout=30.0, max_attempts=3)
    finally:
        pool.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _common.reports_dir() / f"pipeline_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["city"])
        writer.writeheader()
        writer.writerows(rows)

    keys = ("raw", "valid", "duplicates", "cache_hits", "classified", "accepted", "review", "rejected", "canceled", "classifier_errors", "queued")
    mode_name = "PUBLISH (Kafka)" if args.publish else "PUBLISH-DIRECT (dev DB)" if args.publish_direct else "no-publish"
    print(f"\nClassifier chain: {' -> '.join(chain.names)}    mode: {mode_name}")
    print(f"{'source':<17}" + "".join(f"{k[:10]:>11}" for k in keys))
    for src in sorted(metrics):
        print(f"{src:<17}" + "".join(f"{metrics[src].get(k, 0):>11}" for k in keys))
        extra = {k[4:]: v for k, v in metrics[src].items() if k.startswith("src:")}
        if extra:
            print(f"{'':<17}source detail: {extra}")

    sample = random.Random(7).sample(rows, min(args.samples, len(rows)))
    print(f"\n{len(sample)} sample decisions:")
    for r in sample:
        print(f"  [{r['decision']:<8} f={r['family_score'] or '-':>4} a={r['adult_score'] or '-':>4}] "
              f"{r['title'][:70]:<70} | {r['interests'] or '-'} | {r['languages'] or '-'}")
    print(f"\nReport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
