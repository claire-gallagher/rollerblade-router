"""Turning what a rider types into coordinates.

In order: our gazetteer (named points on the Central Park drives, which geocoders can't find),
NYC Geoclient (addresses and intersections), then Pelias (landmarks, venues, streets).
"""

import functools
import json
import re
from pathlib import Path

import geoclient
import stadia
from settings import DATA_DIR
from stadia import MapsError

LATLNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# Where routes can be graded: the LiDAR raster's extent (see scripts/prep_lidar.py BBOX).
SERVICE_AREA = {"min_lng": -74.025, "min_lat": 40.695, "max_lng": -73.905, "max_lat": 40.885}

# Trailing parts that only say "New York". Pelias matches worse with them, so they're dropped.
CITY_PARTS = {"new york", "new york city", "nyc", "ny", "manhattan", "usa", "us", "new york county"}


GAZETTEER_PATH = str(DATA_DIR / "gazetteer.json")
STOPWORDS = {"the", "at", "and", "of", "in", "near", "on", "by"}
DRIVE_WORDS = {"drive", "crossing", "cross", "transverse"}
ABBREVIATIONS = {"st": "street", "dr": "drive", "e": "east", "w": "west", "av": "avenue", "ave": "avenue", "cp": "central park"}


def _tokens(text: str) -> set[str]:
    """Normalized words: 'E 90th St' -> {'east', '90', 'street'}."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for w in words:
        w = re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w)
        out.update(ABBREVIATIONS.get(w, w).split())
    return out - STOPWORDS


_gazetteer: list[dict] | None = None


def gazetteer() -> list[dict]:
    """Our named waypoints (scripts/prep_gazetteer.py), each with the token sets it answers to."""
    global _gazetteer
    if _gazetteer is None:
        _gazetteer = []
        if Path(GAZETTEER_PATH).exists():
            for p in json.loads(Path(GAZETTEER_PATH).read_text())["places"]:
                p["_tokens"] = [_tokens(n) for n in [p["name"], *p["aliases"]]]
                _gazetteer.append(p)
    return _gazetteer


def gazetteer_matches(query: str) -> list[dict]:
    """Gazetteer places whose name or an alias contains every word of the query.

    The query must name a drive: 'Central Park West & W 81 St' is the avenue, not West Drive.
    """
    q = _tokens(query)
    if not q & DRIVE_WORDS:
        return []
    return [
        {"name": p["name"], "lat": p["lat"], "lng": p["lng"], "type": "park drive", "source": "skate gazetteer", "note": p["note"]}
        for p in gazetteer()
        if any(q <= names for names in p["_tokens"])
    ]


def clean_query(query: str) -> str:
    """Drop trailing city parts, keeping a ZIP code: 'Pier 25, New York, NY 10013' -> 'Pier 25 10013'."""
    parts = [p.strip() for p in query.split(",")]
    zip_code = ""
    while len(parts) > 1:
        m = re.fullmatch(r"(.*?)\s*(\d{5})?", parts[-1])
        rest, z = m[1].lower(), m[2]
        if rest and rest not in CITY_PARTS:
            break
        zip_code = zip_code or (z or "")
        parts.pop()
    return " ".join([", ".join(parts), zip_code]).strip()


def _pelias(text: str, limit: int) -> list[dict]:
    # Ask for extra: some may fall outside the service area and be dropped.
    return [
        {"name": p["label"] or p["name"], "lat": p["lat"], "lng": p["lng"], "type": p["layer"],
         "source": "OpenStreetMap (Pelias)"}
        for p in stadia.search_places(text, limit=limit + 5)
    ]


def _geoclient(text: str) -> list[dict]:
    try:
        return geoclient.search(text)
    except geoclient.GeoclientError:
        return []  # Pelias still answers most queries


def find_places(query: str, limit: int = 5) -> list[dict]:
    """Candidates for a place from every source, best first: {name, lat, lng, type, source}.

    Gazetteer matches come first, then Geoclient's address and intersection matches (exact when
    they exist), then Pelias's landmarks, venues, and streets.
    """
    text = clean_query(query)
    candidates = gazetteer_matches(text) + _geoclient(text) + _pelias(text, limit)
    return [c for c in candidates if in_service_area(c["lat"], c["lng"])][:limit]


@functools.lru_cache(maxsize=1024)
def _first_hit(text: str) -> tuple | None:
    """The best match for a cleaned query, asking each source in turn and stopping at the first hit.

    Cached: the agent often re-plans with the same start and end.
    """
    for source in (gazetteer_matches, _geoclient, lambda t: _pelias(t, 1)):
        hits = [c for c in source(text) if in_service_area(c["lat"], c["lng"])]
        if hits:
            return hits[0]["name"], hits[0]["lat"], hits[0]["lng"]
    return None


def in_service_area(lat: float, lng: float) -> bool:
    a = SERVICE_AREA
    return a["min_lat"] <= lat <= a["max_lat"] and a["min_lng"] <= lng <= a["max_lng"]


def resolve(place: str) -> dict:
    """A place string ('lat,lng', address, intersection, or landmark) as {name, lat, lng}: the top candidate."""
    m = LATLNG.match(place)
    if m:
        lat, lng = float(m[1]), float(m[2])
        if not in_service_area(lat, lng):
            raise MapsError(f"{place} is outside the area this router covers (Manhattan, where there is grade data).")
        return {"name": place.strip(), "lat": lat, "lng": lng}
    found = _first_hit(clean_query(place))
    if not found:
        raise MapsError(
            f"Could not find '{place}' in Manhattan. Call find_place to look it up, or use the place's "
            "official name, a street address, or an intersection like 'Riverside Dr & W 96 St'."
        )
    name, lat, lng = found
    return {"name": name, "lat": lat, "lng": lng}
