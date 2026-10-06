import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import yaml

from src.classify.models import TaxonomyValue
from src.classify.text import fold

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "classification"
TAG_HINTS_PATH = CONFIG_DIR / "tag_hints.yaml"
PRIMARY_LANGUAGE_PATH = CONFIG_DIR / "city_primary_language.yaml"

INTERESTS = "interests"
LANGUAGES = "languages"
MANAGED_CODES = (INTERESTS, LANGUAGES)

# The catch-all language value is never tagged.
UNTAGGABLE_LANGUAGE_CODES = frozenset({"other"})

_TAXONOMY_SQL = """
    SELECT v.id AS value_id, q.code AS code, v.value_code AS value_code, v.value_label AS label
    FROM questions q
    JOIN question_values v ON v.question_id = q.id
    WHERE q.code = ANY(%(codes)s) AND q.is_active = true AND v.is_active = true
    ORDER BY q.code, v.sort_order NULLS LAST, v.id;
"""

_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def load_tag_hints(path: Path = TAG_HINTS_PATH) -> Dict[str, str]:
    """Hints keyed by lowercase label, e.g. {'cycling': 'biking, bike rides, BMX'}."""
    return {str(k).strip().lower(): str(v).strip() for k, v in load_yaml(path).items() if v}


class PrimaryLanguageMap:
    """
    Resolves the primary language of a place (a languages value_code such as 'en' or 'fr').
    Lookup on the comma-separated parts of the city string: city, then region, then country.
    Unknown place -> None (nothing is excluded; never guess).
    """

    def __init__(self, cities: Dict[str, str], regions: Dict[str, str], countries: Dict[str, str]):
        self.cities = {fold(k): str(v).strip().lower() for k, v in cities.items()}
        self.regions = {fold(k): str(v).strip().lower() for k, v in regions.items()}
        self.countries = {fold(k): str(v).strip().lower() for k, v in countries.items()}

    @classmethod
    def from_yaml(cls, path: Path = PRIMARY_LANGUAGE_PATH) -> "PrimaryLanguageMap":
        data = load_yaml(path)
        return cls(data.get("cities") or {}, data.get("regions") or {}, data.get("countries") or {})

    def primary_language_code(self, city: str) -> Optional[str]:
        parts = [fold(p) for p in str(city or "").split(",") if fold(p)]
        if not parts:
            return None
        if parts[0] in self.cities:
            return self.cities[parts[0]]
        for part in parts[1:]:
            if part in self.regions:
                return self.regions[part]
        for part in reversed(parts):
            if part in self.countries:
                return self.countries[part]
        _warn_once(f"primary:{fold(city)}", f"[TAXONOMY] No primary language configured for '{city}'; no language is excluded.")
        return None

    def fingerprint(self) -> str:
        items = sorted(
            [("c", k, v) for k, v in self.cities.items()]
            + [("r", k, v) for k, v in self.regions.items()]
            + [("n", k, v) for k, v in self.countries.items()]
        )
        return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()


@dataclass
class Taxonomy:
    interests: List[TaxonomyValue]
    languages: List[TaxonomyValue]
    primary_languages: PrimaryLanguageMap = field(default_factory=lambda: PrimaryLanguageMap({}, {}, {}))

    @property
    def values(self) -> List[TaxonomyValue]:
        return list(self.interests) + list(self.languages)

    def ids(self, code: str) -> set:
        return {v.value_id for v in (self.interests if code == INTERESTS else self.languages)}

    def languages_for_city(self, city: str) -> List[TaxonomyValue]:
        """Languages that may be tagged for an event in this city: all minus primary and 'other'."""
        primary = self.primary_languages.primary_language_code(city)
        return [
            v
            for v in self.languages
            if v.value_code.lower() not in UNTAGGABLE_LANGUAGE_CODES
            and (primary is None or v.value_code.lower() != primary)
        ]

    @property
    def hash(self) -> str:
        return taxonomy_hash(self.values, extra=self.primary_languages.fingerprint())


def taxonomy_hash(values: Iterable[TaxonomyValue], extra: str = "") -> str:
    rows = sorted((v.code, v.value_id, v.label, v.hint or "") for v in values)
    return hashlib.sha256((repr(rows) + "|" + extra).encode("utf-8")).hexdigest()


def load_taxonomy(conn, codes: Sequence[str] = MANAGED_CODES, hints: Optional[Dict[str, str]] = None) -> List[TaxonomyValue]:
    """Active values of the given questions (same join as the app's languages query)."""
    hints = load_tag_hints() if hints is None else hints
    with conn.cursor() as cursor:
        cursor.execute(_TAXONOMY_SQL, {"codes": list(codes)})
        rows = cursor.fetchall()
    values = []
    for row in rows:
        label = str(row["label"]).strip()
        if not label:
            continue
        values.append(
            TaxonomyValue(
                value_id=int(row["value_id"]),
                code=str(row["code"]),
                value_code=str(row["value_code"] or "").strip(),
                label=label,
                hint=hints.get(label.lower()) if row["code"] == INTERESTS else None,
            )
        )
    return values


def load_taxonomy_from_pool(pool=None, codes: Sequence[str] = MANAGED_CODES) -> List[TaxonomyValue]:
    """load_taxonomy with its own pool when none is given; any DB error returns []."""
    created = False
    try:
        if pool is None:
            from src.config.database import get_db_pool

            pool = get_db_pool()
            created = True
        with pool.connection() as conn:
            return load_taxonomy(conn, codes)
    except Exception as err:
        logger.warning(f"[TAXONOMY] Unable to load taxonomy {list(codes)}: {err}. Defaulting to empty.")
        return []
    finally:
        if created and pool is not None:
            pool.close()


def get_active_taxonomy(pool=None) -> Taxonomy:
    """Interests + languages + primary-language map. Tolerates a missing languages question."""
    values = load_taxonomy_from_pool(pool, MANAGED_CODES)
    interests = [v for v in values if v.code == INTERESTS]
    languages = [v for v in values if v.code == LANGUAGES]
    if not languages:
        _warn_once("no-languages", "[TAXONOMY] 'languages' question not found or inactive; classifying interests only.")
    logger.info(f"[TAXONOMY] Loaded {len(interests)} interests and {len(languages)} languages.")
    return Taxonomy(interests=interests, languages=languages, primary_languages=PrimaryLanguageMap.from_yaml())
