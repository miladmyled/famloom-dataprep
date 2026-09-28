import hashlib
import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple

from psycopg import errors as pg_errors

from src.classify.models import ClassificationResult, ClassifyInput
from src.classify.prompts import PROMPT_VERSION
from src.classify.text import normalize_for_hash

logger = logging.getLogger(__name__)

TABLE = "city_event_classifications"


def content_hash(
    inp: ClassifyInput,
    taxonomy_hash: str,
    prompt_version: str = PROMPT_VERSION,
    primary_language: Optional[str] = None,
) -> str:
    """
    Changes whenever anything that could change the classification changes: text, place, date,
    the city's primary language (it decides which languages are asked), prompt wording or
    taxonomy. The city name itself is not part of the key, so the same event listed under
    several cities with the same primary language is classified once.
    """
    parts = [
        normalize_for_hash(inp.title),
        normalize_for_hash(inp.description),
        normalize_for_hash(inp.location_summary),
        inp.start_date.isoformat() if inp.start_date else "",
        primary_language or "",
        prompt_version,
        taxonomy_hash,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


_SELECT_SQL = f"""
    SELECT event_id, url, is_canceled, content_hash, provider, model_version, prompt_version,
           family_score, adult_score, decision, interest_value_ids, language_value_ids, scores,
           source, city, title
    FROM {TABLE}
    WHERE event_id = ANY(%(ids)s);
"""

_UPSERT_SQL = f"""
    INSERT INTO {TABLE} (
        event_id, url, is_canceled, content_hash, provider, model_version, prompt_version,
        family_score, adult_score, decision, interest_value_ids, language_value_ids, scores,
        source, city, title, classified_at
    ) VALUES (
        %(event_id)s, %(url)s, %(is_canceled)s, %(content_hash)s, %(provider)s, %(model_version)s,
        %(prompt_version)s, %(family_score)s, %(adult_score)s, %(decision)s,
        %(interest_value_ids)s, %(language_value_ids)s, %(scores)s::jsonb,
        %(source)s, %(city)s, %(title)s, NOW()
    )
    ON CONFLICT (event_id) DO UPDATE SET
        url = EXCLUDED.url,
        is_canceled = EXCLUDED.is_canceled,
        content_hash = EXCLUDED.content_hash,
        provider = EXCLUDED.provider,
        model_version = EXCLUDED.model_version,
        prompt_version = EXCLUDED.prompt_version,
        family_score = EXCLUDED.family_score,
        adult_score = EXCLUDED.adult_score,
        decision = EXCLUDED.decision,
        interest_value_ids = EXCLUDED.interest_value_ids,
        language_value_ids = EXCLUDED.language_value_ids,
        scores = EXCLUDED.scores,
        source = EXCLUDED.source,
        city = EXCLUDED.city,
        title = EXCLUDED.title,
        classified_at = NOW();
"""

_UPDATE_STATUS_SQL = f"""
    UPDATE {TABLE} SET is_canceled = %(is_canceled)s, url = %(url)s
    WHERE event_id = %(event_id)s AND (is_canceled IS DISTINCT FROM %(is_canceled)s OR url IS DISTINCT FROM %(url)s);
"""


def _row_to_result(row) -> ClassificationResult:
    scores = row["scores"]
    if isinstance(scores, str):
        scores = json.loads(scores)
    return ClassificationResult(
        event_id=row["event_id"],
        url=row["url"],
        is_canceled=bool(row["is_canceled"]),
        provider=row["provider"],
        model_version=row["model_version"],
        prompt_version=row["prompt_version"],
        family_score=row["family_score"],
        adult_score=row["adult_score"],
        decision=row["decision"],
        interest_value_ids=list(row["interest_value_ids"] or []),
        language_value_ids=list(row["language_value_ids"] or []),
        scores=dict(scores or {}),
        content_hash=row["content_hash"],
        source=row["source"],
        city=row["city"],
        title=row["title"],
    )


def _params(r: ClassificationResult) -> dict:
    return {
        "event_id": r.event_id,
        "url": r.url,
        "is_canceled": r.is_canceled,
        "content_hash": r.content_hash,
        "provider": r.provider,
        "model_version": r.model_version,
        "prompt_version": r.prompt_version,
        "family_score": r.family_score,
        "adult_score": r.adult_score,
        "decision": r.decision,
        "interest_value_ids": list(r.interest_value_ids),
        "language_value_ids": list(r.language_value_ids),
        "scores": json.dumps(r.scores),
        "source": r.source,
        "city": r.city,
        "title": (r.title or "")[:240] or None,
    }


class ClassificationCache:
    """
    Read/write access to city_event_classifications. The table is created by the app's
    migrations; if it is missing (or not granted) the cache disables itself for the process,
    logs one warning and the pipeline keeps classifying without it. It never runs DDL.
    """

    def __init__(self, pool):
        self.pool = pool
        self.enabled = pool is not None

    def _disable(self, err: Exception) -> None:
        if self.enabled:
            logger.warning(f"[CACHE] {TABLE} unavailable ({type(err).__name__}: {err}); running without the classification cache.")
        self.enabled = False

    def get_cached(self, event_ids: Iterable[str]) -> Dict[str, ClassificationResult]:
        ids = sorted(set(event_ids))
        if not self.enabled or not ids:
            return {}
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(_SELECT_SQL, {"ids": ids})
                    return {row["event_id"]: _row_to_result(row) for row in cursor.fetchall()}
        except (pg_errors.UndefinedTable, pg_errors.InsufficientPrivilege) as err:
            self._disable(err)
        except Exception as err:
            logger.warning(f"[CACHE] Lookup failed, treating as cache miss: {err}")
        return {}

    def save(self, results: Iterable[ClassificationResult]) -> int:
        rows = [_params(r) for r in results]
        if not self.enabled or not rows:
            return 0
        try:
            with self.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cursor:
                        cursor.executemany(_UPSERT_SQL, rows)
            return len(rows)
        except (pg_errors.UndefinedTable, pg_errors.InsufficientPrivilege) as err:
            self._disable(err)
        except Exception as err:
            logger.warning(f"[CACHE] Saving {len(rows)} classification(s) failed: {err}")
        return 0

    def update_status(self, items: Iterable[Tuple[str, str, bool]]) -> None:
        """Keep url / is_canceled current for cache hits: (event_id, url, is_canceled)."""
        rows = [{"event_id": e, "url": u, "is_canceled": c} for e, u, c in items]
        if not self.enabled or not rows:
            return
        try:
            with self.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cursor:
                        cursor.executemany(_UPDATE_STATUS_SQL, rows)
        except (pg_errors.UndefinedTable, pg_errors.InsufficientPrivilege) as err:
            self._disable(err)
        except Exception as err:
            logger.warning(f"[CACHE] Status update failed: {err}")
