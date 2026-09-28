"""
GATE 2b: measure what the Facebook-snippet source would add, without publishing anything.

    python scripts/evaluate_snippet_yield.py --city "Vancouver, BC, Canada" [--city ...]

Runs the snippet flow (Brave results only; facebook.com is never requested) and reports:
queries, results, screened in, extracted with date+time+place, inside the 14-day window,
duplicates of events already in the dev database, usable rate, and the estimated monthly
Brave cost at two runs a day. Writes reports/snippet_yield_<ts>.csv for eyeballing.
Reads the dev database (duplicates check) but writes nothing to it.
"""
import argparse
import csv
import sys
from datetime import datetime, timezone

import _common

from src.config.database import get_db_pool
from src.etl.dedupe import dedupe_events, load_existing_events
from src.etl.facebook_snippets import FacebookSnippetSource
from src.etl.transformer import clean_and_validate_event

BRAVE_PRICE_PER_1000 = 5.0
RUNS_PER_DAY = 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--city", action="append", required=True)
    args = parser.parse_args()
    _common.require_dev_db()

    pool = get_db_pool()
    rows, totals = [], {"queries": 0, "results": 0, "screened_in": 0, "extracted": 0, "skipped_no_time": 0,
                        "inside_window": 0, "duplicates": 0, "usable": 0}
    try:
        for city in args.city:
            src = FacebookSnippetSource(city)
            raw = src.fetch_raw_events()
            for key in ("queries", "results", "screened_in", "extracted", "skipped_no_time"):
                totals[key] += src.metrics.get(key, 0)
            valid = [e for e in (clean_and_validate_event(r) for r in src.normalize_data(raw)) if e is not None]
            totals["inside_window"] += len(valid)
            kept, dups = dedupe_events(valid, load_existing_events(pool, city))
            totals["duplicates"] += sum(dups.values())
            totals["usable"] += len(kept)
            kept_ids = {e.event_id for e in kept}
            for e in valid:
                rows.append({"city": city, "event_id": e.event_id, "title": e.title, "start_utc": e.start_date.isoformat(),
                             "location": e.location_summary, "summary": e.description, "url": str(e.url),
                             "usable": e.event_id in kept_ids})
    finally:
        pool.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _common.reports_dir() / f"snippet_yield_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["city", "event_id", "title", "start_utc", "location", "summary", "url", "usable"])
        w.writeheader()
        w.writerows(rows)

    per_run_queries = totals["queries"]
    monthly_cost = per_run_queries * RUNS_PER_DAY * 30 * BRAVE_PRICE_PER_1000 / 1000
    usable_rate = totals["usable"] / totals["results"] if totals["results"] else 0.0
    print("\nFacebook snippet yield (nothing published)")
    for key, value in totals.items():
        print(f"  {key:<16} {value}")
    print(f"  usable rate      {usable_rate:.0%} of search results became new, usable events")
    print(f"  Brave cost       ~${monthly_cost:.2f}/month for these cities at {RUNS_PER_DAY} runs/day (before the monthly credit)")
    print(f"\nReport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
