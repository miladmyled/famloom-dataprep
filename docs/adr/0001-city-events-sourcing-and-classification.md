# ADR 0001: City events sourcing and classification

- Status: accepted (2026-09-27)
- Deciders: Milad (product/owner), implemented with Claude Code
- Repos: famloom-dataprep (this repo), rezabazargan/newcomer (two additive migrations)

## Context

FamLoom shows "City Events" to families. Before this change dataprep ingested every Eventbrite
and Meetup result for a family's city, tagged interests by keyword matching and published
everything. Most Meetup events were adult socials, keyword tags were noisy, the scraper ran every
5 minutes (and used up the Eventbrite token's hourly quota), and fallback event ids used Python's
salted `hash()`. FamLoom also wants more local sources (libraries, cities, venues) and
automatic coverage when a family in a new city signs up.

Constraints: released mobile apps must keep working (no app code change); the shared
PostgreSQL schema is owned by the app repo's migrations; `city_events` keeps exactly its
columns `id, city, title, source, url, date, pictureurl, created_at, updated_at`; tests never hit
the network; no scraping of Facebook/Instagram or login-walled content.

## Decisions

1. **AI classification of every event** with Jev (TypeSafe AI, pinned `jev-1.13.0`, Noul
   questions), Gemini Flash-Lite as fallback, keyword matching only when no AI provider is
   configured at all. Results are cached in `city_event_classifications` (keyed by provider
   event id, content hash on text + date + the city's primary language + prompt version +
   taxonomy), so an unchanged event is classified once. Cached decisions are re-derived from the
   stored probabilities when thresholds change.
2. **What "family" means.** FamLoom families include couples without children and same-sex
   couples. An event is family-relevant if any of: families with children attend together; it
   is designed for children (including drop-off programs); it is a public event where bringing
   children is reasonable; or a couple could attend it as a leisure/social outing (not work,
   networking, sales, professional training, service appointments, therapy/support sessions).
   Singles/dating events are rejected. Adult-only content is not rejected; its score is stored.
3. **Thresholds (GATE 1a, 100 reviewed events):** family accept 0.50 (precision 0.99, recall
   0.94), review band 0.40–0.50 dropped and kept in the cache, tag 0.70, language 0.60,
   singles reject 0.60.
4. **Language tags** mean "the event is held in this language", never culture or topic. The
   city's primary language is never tagged (no English in Vancouver, no French in Montreal); the
   mapping is `config/classification/city_primary_language.yaml`. Language tags use the existing
   `languages` question values in `event_interest_tags`.
5. **Fixed `city_events` columns.** Dataprep writes only the nine existing columns, matches rows
   by `url`, and runs no DDL. Description, location and provider id live only in memory, in the
   Kafka message and in the classification cache.
6. **Removal by the nightly janitor, not tombstones.** Rejected and canceled events are never
   published; they are recorded in the cache with their url, and the janitor deletes matching
   `city_events` rows (`JANITOR_REMOVE_CLASSIFIED`). Tags cascade; Activities keep working
   (`source_city_event_id` is `ON DELETE SET NULL`).
7. **Sources (accredited only).** Eventbrite, Meetup, a human-approved curated list of calendars
   (13 approved at GATE 2a), web pages found through Brave Search that pass automatic checks and
   AI approval, Facebook events from search snippets only, and Instagram Business Discovery for
   approved professional accounts. Everything else is rejected.
8. **New cities need no human approval.** Web discovery searches for official calendars per
   city, checks https/robots/login/noindex automatically, checks the site's terms of use with Jev,
   asks Jev to approve (dated events, genuine organizer, family relevant) and remembers approved
   pages in `event_source_sites`. Rejected sites are never stored. The curated file doubles as a
   block list.
9. **Schedule:** scraper twice a day (05:00 and 16:00 America/Vancouver, cluster v1.35 supports
   `timeZone`); janitor unchanged (daily 01:00 UTC).
10. **Schema ownership:** two additive app migrations (`city_event_classifications`,
    `event_source_sites`), each granting DML to `dataprep_worker`; dataprep tolerates both being
    missing.

## Options considered

| Option | Why not chosen |
|---|---|
| Curated calendars only | Needs a person per new city; misses venues nobody listed |
| Search discovery only | Unpredictable quality; official calendars are the best family content |
| Scraping Facebook/Instagram pages | Against their terms, login walls, personal data of private people |
| **Hybrid (chosen)** | Curated head start + automatic discovery with automated safeguards |
| Keyword tagging | Low precision, no family decision, no language signal |
| One LLM for everything | Jev gives calibrated probabilities cheaply; Gemini only where text must be produced |

## Consequences

- City Events shows fewer, better events. Existing events are not re-classified; they expire
  within the 14-day window.
- Released apps show a language chip (e.g. "French") for events held in a non-primary language.
  App ranking does not yet use `family_profiles.languages` (follow-up for the app owner).
- Costs: Jev ≈ $0.0002 per classified event (cents per month), Gemini Flash-Lite cents per
  month, Brave ≈ $5 per 1,000 queries (≈ 60 queries per run budgeted), Meta free.
- The Kafka transport is unchanged and was not exercised on dev (no dev Kafka; manual dev runs
  use `run_pipeline_dev.py --publish-direct`). Verify from production logs after the first run.

## Privacy and terms

- Canadian privacy law treats publicly posted personal information as still protected. No
  images and no person names are stored from social sources; descriptions from free text are the
  extractor's own neutral summaries, never copied text.
- robots.txt is always obeyed; blocked hosts (403) are never bypassed; no CAPTCHA solving, no
  stealth browsing, cookie banners are never accepted by the crawler.
- Curated sites: robots and written terms checked by a person (see the yaml notes). BiblioCommons
  (VPL, Burnaby PL) forbids automated harvesting except RSS: disabled, ask the libraries.
  Destination Vancouver forbids copying: disabled.
- Discovered sites: robots checked automatically and terms checked by Jev. Accepted risk: an
  automated terms check can miss restrictions; if an owner objects, add the domain to
  `config/sources/blocked_domains.yaml` and purge its events.
- **Brave:** storing derived results requires a Brave plan that grants storage rights. Web
  discovery and snippets must not be enabled in production before that is confirmed.
- **Meta:** Instagram needs App Review (instagram_basic, pages_read_engagement, and related
  permissions); confirm in review that showing extracted event info with a link back is allowed.

## GATE 2b result

Brave indexes almost no `facebook.com/events` pages (0 results with the one-month freshness
filter, 2 without for Vancouver). Facebook snippets stay disabled (`FACEBOOK_SNIPPETS_ENABLED=false`).

## How to change course

- Switch sources: `CURATED_CALENDARS_ENABLED`, `WEB_SEARCH_ENABLED`, `FACEBOOK_SNIPPETS_ENABLED`,
  `INSTAGRAM_ENABLED`.
- Switch classifier: `CLASSIFIER_PROVIDER` / `CLASSIFIER_FALLBACK` (`jev`, `gemini`, `none`).
- Tune: `FAMILY_ACCEPT_THRESHOLD`, `FAMILY_REVIEW_THRESHOLD`, `TAG_THRESHOLD`,
  `LANGUAGE_THRESHOLD`, `SINGLES_REJECT_THRESHOLD`, `ADULT_REJECT_THRESHOLD`, `REVIEW_POLICY`.
- Wording changes: edit `src/classify/prompts.py` and bump `PROMPT_VERSION` (re-classifies).
- Stop removals: `JANITOR_REMOVE_CLASSIFIED=false`.

## Follow-ups

- Backoffice review queue for review-band events (the cache already holds them).
- Follow listing pages to event detail pages (City of North Vancouver, The Polygon, Space Centre
  give titles without dates).
- Ask VPL / Burnaby PL / Coquitlam PL / Science World / City of Vancouver for feeds or permission.
- App: use `family_profiles.languages` in ranking; optionally hide language tags from chips.
- Eventbrite search returns events outside the city; the same event appears under several
  cities (city of the row is whichever wrote last).
- Existing: dead-letter topic, `MAX(id)+1` ids, Ticketmaster source, remove dead
  `KAFKA_RESET_OFFSET_ON_START` from the consumer manifest.
