# famloom-dataprep

City Events ETL for FamLoom. It finds upcoming events for every city a family lives in, keeps the
ones a family could attend together, tags them with interests and languages, and loads them into
the app's `city_events` table.

```
sources ──► validate (14-day window) ──► de-duplicate ──► classify (cache → Jev → Gemini)
   ──► accepted events to Kafka ──► consumer upserts city_events + replaces tags
janitor (nightly): expired events + events classified as rejected/canceled are removed
```

Design decisions and trade-offs: [docs/adr/0001-city-events-sourcing-and-classification.md](docs/adr/0001-city-events-sourcing-and-classification.md).

## Workloads (Kubernetes, ArgoCD watches `main`)

| Workload | Schedule | Entry point |
|---|---|---|
| `famloom-scraper` CronJob | 05:00 and 16:00 America/Vancouver | `main.py` |
| `famloom-event-consumer` Deployment | always on | `consumer_main.py` |
| `famloom-event-janitor` CronJob | daily 01:00 UTC | `janitor.py` |

Merging to `main` builds the image and deploys production. Work on feature branches from `dev`.

## Sources

| Source | Flag | Needs | Notes |
|---|---|---|---|
| Eventbrite | always | `EVENTBRITE_API_TOKEN` | API search per city |
| Meetup | always | – | public GraphQL/pages per city |
| Curated calendars | `CURATED_CALENDARS_ENABLED` | Jev + Gemini for HTML pages | `config/sources/curated_calendars.yaml`, human approved |
| Official sites | `OFFICIAL_SITES_ENABLED` | Jev, Gemini | city/venue websites from Wikidata (CC0) → events page → automatic checks + terms check + AI approval; approved pages remembered in `event_source_sites` |
| Web search | `WEB_SEARCH_ENABLED` (false in prod) | `BRAVE_SEARCH_API_KEY`, Jev, Gemini | same checks; memoryless unless `WEB_SEARCH_REMEMBER_SITES=true` (Brave storage rights) |

A source whose key is missing logs a warning and is skipped; the run continues.
De-duplication keeps the higher-priority source: Eventbrite/Meetup > curated and official sites >
web search.

Curated and discovered events get the organizer's own picture (`pictureurl`) from structured
data, the event's card on the listing, or the event page (`og:image`). Events without a picture
are skipped (`REQUIRE_PICTURE=true`).

### Crawling rules

All web requests go through `src/net/http.py`: User-Agent `FamLoomBot/1.0 (+mailto:<CRAWLER_CONTACT_EMAIL>)`,
robots.txt obeyed (403 = no), per-domain rate limit, 3 MB cap, Facebook/Instagram hosts and private
addresses always refused. No logins, no CAPTCHA solving, no stealth, cookie banners are never accepted.

## Classification

Per event, one Jev request asks: family outing with children, designed for children, kid-welcome
public event, couple leisure outing (family score = the strongest), singles/dating (rejects), adult
(stored only), one question per interest, one per language **except the city's primary language**
(`config/classification/city_primary_language.yaml`). Thresholds (GATE 1a): accept ≥ 0.50, review
0.40–0.50 (dropped, kept in the cache), tags ≥ 0.70, languages ≥ 0.60. Interest hints:
`config/classification/tag_hints.yaml`. Wording: `src/classify/prompts.py` (bump `PROMPT_VERSION`).

## Configuration

Copy `.env.example` to `.env` (git-ignored) and fill in values. Never print or commit secrets.
New variables for the production Secret `dataprep-secrets`: `TYPESAFE_API_KEY`, `GEMINI_API_KEY`,
`GEMINI_MODEL`, `BRAVE_SEARCH_API_KEY`.
Non-secret settings (flags, thresholds, crawler contact) are in `k8s-manifests/*.yaml`.

## Running locally (dev database only)

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows path; bin/ on Linux/macOS
.venv/Scripts/python -m pytest -q                                         # offline: tests never hit the network
.venv/Scripts/python scripts/check_env.py --skip kafka                    # keys set/missing + smoke calls
.venv/Scripts/python scripts/run_pipeline_dev.py --city "Vancouver, BC, Canada" --sources eventbrite,meetup,curated --no-publish
.venv/Scripts/python scripts/run_pipeline_dev.py --all-cities --publish-direct   # write to the dev DB via the consumer code path
.venv/Scripts/python janitor.py --dry-run                                  # what the janitor would remove (CSV in reports/)
```

Dev runs are manual; nothing is scheduled on dev and no Kafka is needed (`--publish-direct`
serializes the Kafka message and runs the consumer's upsert). Scripts print the target database and
refuse the production database (`x3db`).

| Script | Purpose |
|---|---|
| `scripts/check_env.py` | variables set/missing, smoke-test DB tables, Jev, Gemini, Brave, Kafka |
| `scripts/run_pipeline_dev.py` | dev run for chosen cities/sources: `--no-publish`, `--publish-direct`, `--publish` (local Kafka) |
| `scripts/evaluate_classifier.py` | build a labeling sheet (`--sample`) and grade thresholds (`--evaluate`) |
| `scripts/reset_dev_events.py` | back up and empty dev `city_events`/tags before a fresh load (never TRUNCATE) |
| `janitor.py --dry-run / --backup` | preview / back up before the classified removal |
| `scripts/load_events_to_db.py` | legacy direct loader, now classifies like `main.py` |
| `scripts/backfill_interest_tags.py` | legacy keyword backfill (superseded; existing events simply expire) |

## Adding sources

**A curated calendar:** add an entry to `config/sources/curated_calendars.yaml` with `city`, `name`,
`url`, `kind` (`ical` | `rss` | `jsonld` | `html`), `render` (`static` | `js`), `source_label`,
`enabled`, and record `robots_checked` / `terms_checked` with notes. Prefer iCal/RSS feeds. Sites
that forbid automated access stay listed with `enabled: false` and the reason (they are then also
blocked for web discovery).

**A new city:** nothing to do. Cities come from `family_profiles.location`; Eventbrite, Meetup and
official-site discovery (Wikidata) cover a new city on the next run. Add a `city_primary_language.yaml` entry if the
city's province/country is not listed.

## City Events contract (shared database)

- `city_events` (app-owned schema): dataprep writes exactly `id, city, title, source, url, date,
  pictureurl, created_at, updated_at`, matches rows by `url`, never runs DDL.
- `event_interest_tags`: dataprep replaces tags of the `interests` and `languages` questions only
  (messages with `replace_tags=true`); tags of other questions are never touched.
- `questions` / `question_values` (`interests`, `languages`) and `family_profiles.location`: read only.
- `city_event_classifications` (app migration `20260927184201`): written by the scraper, read by the
  janitor; the app never reads it.
- `event_source_sites` (app migration `20260927210000`): approved discovered pages, dataprep only.
- Removal of rejected/canceled events is done by the janitor (`JANITOR_REMOVE_CLASSIFIED`); deleting
  an event cascades its tags and sets `activities.source_city_event_id` to NULL.
