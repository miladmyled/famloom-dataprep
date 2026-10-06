"""
Wikidata (CC0 public domain) lookups for official websites of a city and of its public venues:
libraries, museums/galleries, science centres, zoos, aquariums, theatres, concert halls, arts
and community centres. Used by official-site discovery; storing these URLs has no terms issue.
"""
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

from src.classify.text import fold

logger = logging.getLogger(__name__)

SPARQL_URL = "https://query.wikidata.org/sparql"
COUNTRY_QID = {"canada": "Q16", "united states": "Q30", "usa": "Q30", "germany": "Q183", "italy": "Q38",
               "france": "Q142", "united kingdom": "Q145", "uk": "Q145", "spain": "Q29"}
# library, museum, zoo, public aquarium, theatre building, concert hall, community centre,
# science centre, arts centre, cultural centre (subclasses included via P279*)
VENUE_KINDS = ("Q7075", "Q33506", "Q43501", "Q2281788", "Q24354", "Q1060829", "Q5153694", "Q588140", "Q2190251", "Q1774898")


@dataclass(frozen=True)
class Venue:
    qid: str
    label: str
    website: str
    kind: str


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class WikidataClient:
    def __init__(self, session: Optional[requests.Session] = None, user_agent: Optional[str] = None,
                 timeout_seconds: float = 90.0, min_interval_seconds: float = 1.0, sleep=time.sleep):
        from src.net.http import user_agent as default_user_agent

        self.session = session or requests.Session()
        self.headers = {"User-Agent": user_agent or default_user_agent(), "Accept": "application/sparql-results+json"}
        self.timeout = timeout_seconds
        self.min_interval = min_interval_seconds
        self.sleep = sleep
        self._last = 0.0

    def query(self, sparql: str) -> list:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(SPARQL_URL, params={"query": sparql}, headers=self.headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Wikidata HTTP {resp.status_code}")
        return resp.json()["results"]["bindings"]

    def city_entities(self, city: str) -> List[Venue]:
        """Settlements named like the city's first part (in its country when known), with website."""
        parts = [p.strip() for p in str(city).split(",") if p.strip()]
        if not parts:
            return []
        country = next((COUNTRY_QID[fold(p)] for p in reversed(parts) if fold(p) in COUNTRY_QID), None)
        country_filter = f"wdt:P17 wd:{country};" if country else ""
        rows = self.query(f'''
            SELECT DISTINCT ?c ?cLabel ?site WHERE {{
              ?c rdfs:label "{_escape(parts[0])}"@en; {country_filter} wdt:P31/wdt:P279* wd:Q486972.
              OPTIONAL {{ ?c wdt:P856 ?site }}
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
            }} LIMIT 5''')
        return [Venue(r["c"]["value"].rsplit("/", 1)[-1], r["cLabel"]["value"], r.get("site", {}).get("value", ""), "city") for r in rows]

    def venues_in(self, qid: str, limit: int = 200) -> List[Venue]:
        kinds = " ".join(f"wd:{k}" for k in VENUE_KINDS)
        rows = self.query(f'''
            SELECT DISTINCT ?o ?oLabel ?kindLabel ?site WHERE {{
              {{ ?o wdt:P131/wdt:P131* wd:{qid} }} UNION {{ ?o wdt:P159 wd:{qid} }} UNION {{ ?o wdt:P276 wd:{qid} }}
              ?o wdt:P856 ?site; wdt:P31/wdt:P279* ?kind.
              VALUES ?kind {{ {kinds} }}
              FILTER NOT EXISTS {{ ?o wdt:P576 ?closed }}
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
            }} LIMIT {int(limit)}''')
        seen, venues = set(), []
        for r in rows:
            oid = r["o"]["value"].rsplit("/", 1)[-1]
            if oid in seen:
                continue
            seen.add(oid)
            venues.append(Venue(oid, r["oLabel"]["value"], r["site"]["value"], r.get("kindLabel", {}).get("value", "")))
        return venues

    def official_sites(self, city: str) -> List[Venue]:
        """City websites plus venues located in the city, one entry per website."""
        out, sites = [], set()
        for entity in self.city_entities(city):
            candidates = ([entity] if entity.website else []) + self.venues_in(entity.qid)
            for venue in candidates:
                key = venue.website.rstrip("/").lower()
                if venue.website and key not in sites:
                    sites.add(key)
                    out.append(venue)
        return out
