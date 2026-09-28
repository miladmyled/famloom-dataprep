import os
import sys
import time
import logging
import concurrent.futures
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

# Load environment configuration
load_dotenv(override=True)

# Configure structured enterprise logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ETLOrchestrator")

from src.etl.extractor import get_active_cities
from src.etl.eventbrite import EventbriteScraper
from src.etl.meetup_public import MeetupExtractor
from src.etl.transformer import clean_and_validate_event
from src.etl.kafka_producer import EventKafkaProducer
from src.etl.dedupe import dedupe_events, load_existing_events
from src.etl.curated_calendars import CuratedCalendarSource, curated_enabled
from src.etl.web_search_discovery import SiteStore, WebSearchDiscoverySource, web_search_enabled
from src.etl.facebook_snippets import FacebookSnippetSource, facebook_snippets_enabled
from src.etl.instagram_business import InstagramBusinessSource, instagram_enabled
from src.etl.official_sites import OfficialSitesDiscoverySource, official_sites_enabled
from src.etl.base import BaseEventScraper
from src.classify.cache import ClassificationCache
from src.classify.factory import get_classifier
from src.classify.stage import METRIC_KEYS, classify_events
from src.classify.taxonomy import get_active_taxonomy
from src.config.database import get_db_pool
from src.models.event import CityEvent

SOURCE_METRIC_KEYS = ("raw", "valid", "duplicates") + METRIC_KEYS + ("queued",)


@dataclass
class ScrapeResult:
    name: str
    source: str
    city: str
    raw: int = 0
    events: List[CityEvent] = field(default_factory=list)
    ok: bool = True


class SharedWebClients:
    """One polite HTTP client, Brave budget, Jev screener and Gemini extractor per run (lazy)."""

    def __init__(self, pool=None):
        self.pool = pool
        self._http = self._search = self._screener = self._extractor = None
        self.store = SiteStore(pool)

    @property
    def http(self):
        if self._http is None:
            from src.net.http import PoliteHttpClient

            self._http = PoliteHttpClient()
        return self._http

    @property
    def search(self):
        if self._search is None:
            from src.net.brave import BraveSearchClient

            self._search = BraveSearchClient()
        return self._search

    @property
    def screener(self):
        if self._screener is None:
            from src.classify.screen import get_screener

            self._screener = get_screener() or False
        return self._screener or None

    @property
    def extractor(self):
        if self._extractor is None:
            try:
                from src.classify.gemini import GeminiExtractor

                self._extractor = GeminiExtractor()
            except Exception as err:
                logger.warning(f"⚠️ Gemini extractor unavailable: {err}")
                self._extractor = False
        return self._extractor or None


def _brave_configured() -> bool:
    return bool(os.getenv("BRAVE_SEARCH_API_KEY"))


def build_scraper_tasks(city: str, shared: Optional[SharedWebClients] = None) -> List[Tuple[str, str, BaseEventScraper]]:
    """(task name, source kind, scraper) for every enabled source of a city."""
    tasks: List[Tuple[str, str, BaseEventScraper]] = [
        (f"EventbriteScraper[{city}]", "eventbrite", EventbriteScraper(city=city)),
        (f"MeetupExtractor[{city}]", "meetup", MeetupExtractor(city=city)),
    ]
    if shared is None:
        return tasks
    if curated_enabled():
        curated = CuratedCalendarSource(city=city, http=shared.http, screener=shared.screener, extractor=shared.extractor)
        if curated.calendars:
            tasks.append((f"CuratedCalendars[{city}]", "curated", curated))
    if official_sites_enabled():
        tasks.append((f"OfficialSites[{city}]", "official", OfficialSitesDiscoverySource(
            city=city, http=shared.http, screener=shared.screener, extractor=shared.extractor, store=shared.store)))
    if web_search_enabled():
        if _brave_configured():
            tasks.append((f"WebDiscovery[{city}]", "web", WebSearchDiscoverySource(
                city=city, search=shared.search, http=shared.http, screener=shared.screener,
                extractor=shared.extractor, store=shared.store)))
        else:
            logger.warning(f"⚠️ WEB_SEARCH_ENABLED but BRAVE_SEARCH_API_KEY missing; web discovery skipped for '{city}'.")
    if facebook_snippets_enabled():
        if _brave_configured():
            tasks.append((f"FacebookSnippets[{city}]", "facebook_snippet", FacebookSnippetSource(
                city=city, search=shared.search, screener=shared.screener, extractor=shared.extractor)))
        else:
            logger.warning(f"⚠️ FACEBOOK_SNIPPETS_ENABLED but BRAVE_SEARCH_API_KEY missing; skipped for '{city}'.")
    if instagram_enabled():
        if os.getenv("META_IG_USER_ID") and os.getenv("META_ACCESS_TOKEN"):
            instagram = InstagramBusinessSource(city=city, screener=shared.screener, extractor=shared.extractor)
            if instagram.accounts:
                tasks.append((f"InstagramBusiness[{city}]", "instagram", instagram))
        else:
            logger.warning(f"⚠️ INSTAGRAM_ENABLED but META_IG_USER_ID/META_ACCESS_TOKEN missing; skipped for '{city}'.")
    return tasks


def _scrape(name: str, source: str, scraper: BaseEventScraper) -> ScrapeResult:
    """Extract, normalize and validate one source for one city. Never raises."""
    result = ScrapeResult(name=name, source=source, city=scraper.city)
    logger.info(f"⚡ [WORKER START] Running {name}...")
    try:
        raw_events = scraper.fetch_raw_events()
        result.raw = len(raw_events)
        for raw_dict in scraper.normalize_data(raw_events):
            valid_event = clean_and_validate_event(raw_dict)
            if valid_event is not None:
                if not valid_event.origin:
                    valid_event = valid_event.model_copy(update={"origin": source})
                result.events.append(valid_event)
        logger.info(f"✅ [WORKER FINISH] {name}: {result.raw} raw, {len(result.events)} valid.")
    except Exception as exc:
        result.ok = False
        logger.error(f"❌ [{name}] Unexpected error during extraction: {exc}", exc_info=True)
    return result


def _format_source_metrics(source: str, m: Counter) -> str:
    return f"[METRICS] source={source} " + " ".join(f"{k}={m.get(k, 0)}" for k in SOURCE_METRIC_KEYS)


def run_etl_pipeline() -> int:
    """
    Main ETL Orchestration routine executed by the Kubernetes CronJob (twice a day).
    1. Loads active cities, the interests/languages taxonomy and the classifier chain.
    2. Scrapes every source for every city concurrently and validates events (14-day window).
    3. Per city: de-duplicates, classifies (cache -> Jev -> Gemini), keeps family events only.
    4. Publishes accepted events with interest + non-primary language tags to Kafka.
    5. Flushes the Kafka producer and logs per-source metrics and per-city completion.
    """
    start_time = time.time()
    logger.info("==================================================")
    logger.info("🚀 Starting Famloom Event ETL Worker (CronJob Run)")
    logger.info("==================================================")

    # 1. Cities, taxonomy, classifier chain and classification cache
    logger.info("[STEP 1/5] Fetching active cities, taxonomy and classifier configuration...")
    try:
        active_cities: List[str] = get_active_cities()
    except Exception as db_err:
        logger.error(f"❌ Fatal database error fetching active cities: {db_err}")
        return 1

    if not active_cities:
        logger.warning("⚠️ No active cities returned from database. Terminating job successfully.")
        return 0
    logger.info(f"📍 Found {len(active_cities)} active target cities: {active_cities}")

    cache_pool = None
    try:
        cache_pool = get_db_pool()
    except Exception as pool_err:
        logger.warning(f"⚠️ Could not open a DB pool for the classification cache: {pool_err}")
    taxonomy = get_active_taxonomy(cache_pool)
    chain = get_classifier()
    cache = ClassificationCache(cache_pool)

    # 2. Kafka producer
    logger.info("[STEP 2/5] Initializing Confluent Kafka Producer...")
    producer: Optional[EventKafkaProducer] = None
    try:
        producer = EventKafkaProducer()
    except Exception as k_err:
        logger.error(f"❌ Fatal Kafka initialization error: {k_err}")
        if cache_pool is not None:
            cache_pool.close()
        return 1

    metrics: Dict[str, Counter] = defaultdict(Counter)
    city_status: Dict[str, str] = {}
    tasks: List[Tuple[str, str, BaseEventScraper]] = []
    unflushed = 0

    try:
        # 3. Scrape all sources concurrently
        logger.info("[STEP 3/5] Scraping and validating events concurrently...")
        shared = SharedWebClients(cache_pool)
        for city in active_cities:
            tasks.extend(build_scraper_tasks(city, shared))
        by_city: Dict[str, List[ScrapeResult]] = defaultdict(list)
        max_workers = min(int(os.getenv("MAX_CONCURRENT_WORKERS", "3")), max(len(tasks), 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_scrape, name, source, scraper) for name, source, scraper in tasks]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                by_city[result.city].append(result)
                metrics[result.source]["raw"] += result.raw
                metrics[result.source]["valid"] += len(result.events)

        # 4. Per city: de-duplicate, classify, publish
        logger.info("[STEP 4/5] De-duplicating, classifying and publishing per city...")
        for city in active_cities:
            results = by_city.get(city, [])
            try:
                existing = load_existing_events(cache_pool, city) if cache_pool is not None else []
                events, duplicates = dedupe_events([e for r in results for e in r.events], existing)
                for source, count in duplicates.items():
                    metrics[source]["duplicates"] += count
                stage = classify_events(events, chain, cache, taxonomy)
                for source, counter in stage.metrics.items():
                    metrics[source].update(counter)
                queued = 0
                for event in stage.publish:
                    if producer.publish_event(event):
                        metrics[event.origin or event.source]["queued"] += 1
                        queued += 1
                if queued:
                    producer.flush(timeout=5.0, max_attempts=1)
                complete = bool(results) and all(r.ok for r in results)
                city_status[city] = "complete" if complete else "incomplete"
                logger.info(
                    f"🏙️ [CITY {'COMPLETE' if complete else 'INCOMPLETE'}] '{city}': {len(events)} events, "
                    f"{len(stage.publish)} published."
                )
            except Exception as city_err:
                city_status[city] = "incomplete"
                logger.error(f"❌ City '{city}' failed and will be retried next run: {city_err}", exc_info=True)

    except KeyboardInterrupt:
        logger.warning("⚠️ Interrupted by signal. Commencing graceful shutdown...")
    except Exception as pipeline_err:
        logger.error(f"❌ Unexpected pipeline error during execution: {pipeline_err}", exc_info=True)

    finally:
        # 5. Guarantee Kafka Producer buffer flush before process termination
        if producer is not None:
            logger.info("\n[STEP 5/5] Executing guaranteed final Kafka producer buffer flush...")
            try:
                unflushed = producer.flush(timeout=30.0, max_attempts=3)
            except Exception as flush_err:
                logger.error(f"❌ Error during final producer flush: {flush_err}")
                unflushed = -1
        if cache_pool is not None:
            cache_pool.close()

    delivery_stats = producer.get_delivery_metrics() if producer else {}
    elapsed = time.time() - start_time
    total = Counter()
    for counter in metrics.values():
        total.update(counter)

    logger.info("==================================================")
    logger.info("📊 ETL PIPELINE EXECUTION SUMMARY")
    logger.info("==================================================")
    logger.info(f"⏱️ Total Execution Time : {elapsed:.2f} seconds")
    logger.info(f"🧠 Classifier Chain     : {' -> '.join(chain.names)}")
    logger.info(f"🏙️ Cities Complete      : {sum(1 for s in city_status.values() if s == 'complete')}/{len(active_cities)}")
    logger.info(f"📥 Raw Events Scraped   : {total['raw']}")
    logger.info(f"✅ Validated Events     : {total['valid']}")
    logger.info(f"👪 Accepted / Review / Rejected / Canceled : {total['accepted']} / {total['review']} / {total['rejected']} / {total['canceled']}")
    logger.info(f"📤 Queued to Kafka      : {total['queued']}")
    logger.info(f"🎯 Broker Acknowledged  : {delivery_stats.get('delivered', 0)}")
    logger.info(f"⚠️ Unflushed Buffer Msg : {unflushed}")
    for source in sorted(metrics):
        logger.info(_format_source_metrics(source, metrics[source]))
    for city, status in city_status.items():
        logger.info(f"[CITY STATUS] city='{city}' status={status}")
    logger.info("==================================================")

    if unflushed > 0:
        logger.error(f"❌ Worker completed with {unflushed} unsent Kafka messages.")
        return 1

    logger.info("🎉 ETL CronJob execution completed successfully!")
    return 0


if __name__ == "__main__":
    exit_code = run_etl_pipeline()
    sys.exit(exit_code)
