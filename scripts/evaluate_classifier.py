"""
Classifier evaluation against human labels.

1) Build a labeling sheet (stratified by family-score band and source) from dry-run reports:
     python scripts/evaluate_classifier.py --sample 100
   -> reports/labeled_events.csv  (no model scores in it, so labels are not anchored)
   Columns: event_id,url,city,source,title,date,family_label,interest_labels,language_labels,notes,origin
   family_label: yes | no   (interest/language labels: ';'-separated value labels, optional)
   Proposed labels are filled in by Claude and must be reviewed/corrected by the user.

2) Evaluate once the labels are reviewed:
     python scripts/evaluate_classifier.py --evaluate reports/labeled_events.csv
   Uses the scores already stored in city_event_classifications (no new API cost), prints
   family precision/recall/F1 for thresholds 0.30-0.90, the confusion matrix at the current
   thresholds, per-tag precision/recall, language accuracy and a recommendation.
"""
import argparse
import csv
import glob
import os
import random
import sys
from collections import Counter, defaultdict

import _common

LABEL_FIELDS = ["event_id", "url", "city", "source", "title", "date", "family_label",
                "interest_labels", "language_labels", "notes", "origin"]


def _band(score: str) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "none"
    return "0.0-0.2" if s < 0.2 else "0.2-0.4" if s < 0.4 else "0.4-0.7" if s < 0.7 else "0.7-1.0"


def build_sample(n: int, reports, seed: int) -> None:
    rows, seen = [], set()
    for path in reports:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            if r["url"] in seen or r["decision"] in ("canceled", "unclassified"):
                continue
            seen.add(r["url"])
            r["_origin"] = os.path.basename(path)
            rows.append(r)
    strata = defaultdict(list)
    for r in rows:
        strata[(_band(r["family_score"]), r["source"])].append(r)
    rng = random.Random(seed)
    for group in strata.values():
        rng.shuffle(group)
    picked = []
    while len(picked) < n and any(strata.values()):
        for key in sorted(strata):  # round-robin over strata keeps rare bands represented
            if strata[key] and len(picked) < n:
                picked.append(strata[key].pop())
    out = _common.reports_dir() / "labeled_events.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LABEL_FIELDS)
        w.writeheader()
        for r in sorted(picked, key=lambda r: (r["source"], r["title"])):
            w.writerow({"event_id": r["event_id"], "url": r["url"], "city": r["city"], "source": r["source"],
                        "title": r["title"], "date": r["date"], "family_label": "", "interest_labels": "",
                        "language_labels": "", "notes": "", "origin": r["_origin"]})
    print(f"Wrote {len(picked)} rows (from {len(rows)} classified events) to {out}")
    print("Bands:", Counter(_band(r["family_score"]) for r in picked), "Sources:", Counter(r["source"] for r in picked))


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def evaluate(path: str) -> int:
    from src.classify.cache import ClassificationCache
    from src.classify.decision import Thresholds, decide
    from src.classify.taxonomy import get_active_taxonomy
    from src.config.database import get_db_pool

    labeled = [r for r in csv.DictReader(open(path, encoding="utf-8-sig")) if r["family_label"].strip().lower() in ("yes", "no")]
    if not labeled:
        sys.exit("No rows with family_label yes/no yet.")
    _common.require_dev_db()
    pool = get_db_pool()
    try:
        cached = ClassificationCache(pool).get_cached([r["event_id"] for r in labeled])
        taxonomy = get_active_taxonomy(pool)
    finally:
        pool.close()
    by_label = {v.label.lower(): v.value_id for v in taxonomy.values}
    t = Thresholds.from_env()

    pairs = [(r, cached[r["event_id"]]) for r in labeled if r["event_id"] in cached]
    print(f"\n{len(labeled)} labeled rows, {len(pairs)} with cached scores (provider: {Counter(c.provider for _, c in pairs)})")

    print("\nFamily threshold sweep (event is published when family >= threshold and adult < %.2f):" % t.adult_reject)
    print(f"{'thr':>5} {'prec':>6} {'recall':>7} {'f1':>6} {'published':>10}")
    best = None
    for i in range(30, 91, 5):
        thr = i / 100
        tp = fp = fn = 0
        for r, c in pairs:
            pred = (c.family_score or 0) >= thr and (c.adult_score or 0) < t.adult_reject
            gold = r["family_label"].strip().lower() == "yes"
            tp += pred and gold
            fp += pred and not gold
            fn += (not pred) and gold
        p, rc, f = _prf(tp, fp, fn)
        print(f"{thr:>5.2f} {p:>6.2f} {rc:>7.2f} {f:>6.2f} {tp + fp:>10}")
        # prefer precision: a wrong adult event in a family app costs more than a missed one
        score = 0.6 * p + 0.4 * rc
        if best is None or score > best[0]:
            best = (score, thr, p, rc)

    print(f"\nConfusion at current thresholds (accept {t.family_accept}, review {t.family_review}, adult {t.adult_reject}):")
    matrix = Counter()
    for r, c in pairs:
        matrix[(r["family_label"].strip().lower(), decide(c.family_score, c.adult_score, t))] += 1
    for gold in ("yes", "no"):
        print(f"  label={gold:<3} " + "  ".join(f"{d}={matrix[(gold, d)]}" for d in ("accept", "review", "reject")))

    tag_stats = defaultdict(lambda: [0, 0, 0])
    lang_ok = lang_total = 0
    for r, c in pairs:
        if r["interest_labels"].strip():
            gold = {by_label.get(x.strip().lower()) for x in r["interest_labels"].split(";") if x.strip()}
            pred = set(c.interest_value_ids)
            for vid in gold | pred:
                s = tag_stats[vid]
                s[0] += vid in gold and vid in pred
                s[1] += vid in pred and vid not in gold
                s[2] += vid in gold and vid not in pred
        gold_langs = {by_label.get(x.strip().lower()) for x in r["language_labels"].split(";") if x.strip()}
        lang_total += 1
        lang_ok += gold_langs == set(c.language_value_ids)
    if tag_stats:
        labels = {v.value_id: v.label for v in taxonomy.values}
        print("\nInterest tags (rows with interest labels only):")
        for vid, (tp, fp, fn) in sorted(tag_stats.items(), key=lambda kv: -sum(kv[1])):
            p, rc, _ = _prf(tp, fp, fn)
            print(f"  {labels.get(vid, vid):<24} precision={p:.2f} recall={rc:.2f} (tp={tp} fp={fp} fn={fn})")
    print(f"\nLanguage tags exactly right: {lang_ok}/{lang_total}")
    q = len(taxonomy.interests) + len(taxonomy.languages) + 2
    print(f"Cost estimate: ~{q} questions/event, ~4-6k input tokens -> ~$0.0002/event at $0.042/Mtok")
    if best:
        print(f"\nRecommended FAMILY_ACCEPT_THRESHOLD ~ {best[1]:.2f} (precision {best[2]:.2f}, recall {best[3]:.2f}; precision weighted 60/40)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, help="write a labeling sheet with this many events")
    parser.add_argument("--reports", nargs="*", help="pipeline CSVs to sample from (default: all reports/pipeline_*.csv)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--evaluate", help="labeled CSV to evaluate")
    args = parser.parse_args()
    if args.sample:
        reports = args.reports or sorted(glob.glob(str(_common.REPORTS_DIR / "pipeline_*.csv")))
        build_sample(args.sample, reports, args.seed)
        return 0
    if args.evaluate:
        return evaluate(args.evaluate)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
