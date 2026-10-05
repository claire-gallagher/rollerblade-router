"""Thin clients for Stadia Maps: Valhalla bicycle routing and Pelias geocoding, on OpenStreetMap data.

Each function returns plain Python data or raises MapsError. MapsError messages are written for the
model to read, so the tools can pass them straight through.
"""

import os
from collections import Counter

import polyline
import requests

ROUTE_URL = "https://api.stadiamaps.com/route/v1"
GEOCODE_URL = "https://api.stadiamaps.com/geocoding/v1/search"

# Geocoding is biased toward Manhattan and limited to the NYC area.
MANHATTAN_CENTER = (40.7831, -73.9712)
NYC_BOX = {"min_lat": 40.49, "max_lat": 40.92, "min_lon": -74.27, "max_lon": -73.68}

# Average skating speed on smooth, flat pavement, so Valhalla's durations fit a skater.
SKATE_SPEED_KMH = 13


class MapsError(Exception):
    """A maps API call failed. The message says what to do about it."""


def _key() -> str:
    key = os.environ.get("STADIA_API_KEY")
    if not key:
        raise MapsError("STADIA_API_KEY is not set on the server, so no routing or place search is available.")
    return key


def search_places(query: str, limit: int = 5) -> list[dict]:
    """Places matching a text query in New York, nearest Manhattan first: name, label, lat, lng, layer.

    Send the query bare: Pelias matches "Little Island" better than "Little Island, New York, NY".
    """
    params = {
        "text": query,
        "focus.point.lat": MANHATTAN_CENTER[0],
        "focus.point.lon": MANHATTAN_CENTER[1],
        **{f"boundary.rect.{k}": v for k, v in NYC_BOX.items()},
        "size": limit,
        "api_key": _key(),
    }
    try:
        resp = requests.get(GEOCODE_URL, params=params, timeout=15)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise MapsError(f"Geocoding request failed: {e}") from e
    if not resp.ok:
        raise MapsError(f"Geocoding returned HTTP {resp.status_code}: {str(data)[:300]}")
    return [
        {
            "name": f["properties"].get("name", ""),
            "label": f["properties"].get("label", ""),
            "lat": f["geometry"]["coordinates"][1],
            "lng": f["geometry"]["coordinates"][0],
            "layer": f["properties"].get("layer", ""),
        }
        for f in data.get("features", [])
        # The search box has to be a rectangle, so it reaches into New Jersey. Drop those.
        if f["properties"].get("region_a") == "NY"
    ]


def _route_name(steps: list[dict]) -> str:
    """'via X and Y': the two streets the route spends the most distance on."""
    by_length = Counter()
    for s in steps:
        if s["street"]:
            by_length[s["street"]] += s["distance_m"]
    top = [name for name, _ in by_length.most_common(2)]
    return "via " + " and ".join(top) if top else ""


def _parse_trip(trip: dict) -> dict:
    steps = []
    for leg in trip["legs"]:
        shape = polyline.decode(leg["shape"], 6)
        for m in leg["maneuvers"]:
            points = shape[m["begin_shape_index"]:m["end_shape_index"] + 1]
            if len(points) < 2:
                continue  # the arrival maneuver has no length
            names = m.get("street_names") or []
            steps.append({
                "instruction": m.get("instruction", ""),
                "street": names[0] if names else None,
                "distance_m": round(m.get("length", 0) * 1000),
                "points": points,
            })
    return {
        "distance_m": round(trip["summary"]["length"] * 1000),
        "duration_s": round(trip["summary"]["time"]),
        "description": _route_name(steps),
        "steps": steps,
    }


def compute_bike_routes(
    places: list[dict],
    use_roads: float = 0.1,
    use_hills: float = 0.1,
    avoid_bad_surfaces: float = 0.5,
    alternates: int = 2,
) -> list[dict]:
    """Bicycle routes through places in order (origin, any waypoints, destination), tuned for skating.

    places: dicts with name, lat, lng (see places.resolve).
    use_roads: 0 stays on bike paths and lanes, 1 is happy on roads with traffic.
    use_hills: 0 avoids hills even at the cost of distance, 1 ignores them.
    avoid_bad_surfaces: 0 ignores pavement quality, 1 refuses rough surfaces.

    Returns one dict per route: distance_m, duration_s, description, and steps. Each step has
    instruction, street, distance_m, and points (a list of (lat, lng)). Returns [] if no route exists.
    """
    locations = [
        {"lat": p["lat"], "lon": p["lng"], "type": "break" if i in (0, len(places) - 1) else "via"}  # via: may U-turn there
        for i, p in enumerate(places)
    ]
    body = {
        "locations": locations,
        "costing": "bicycle",
        "costing_options": {"bicycle": {
            "bicycle_type": "hybrid",
            "cycling_speed": SKATE_SPEED_KMH,
            "use_roads": use_roads,
            "use_hills": use_hills,
            "avoid_bad_surfaces": avoid_bad_surfaces,
        }},
        "units": "kilometers",
        "language": "en-US",
        "alternates": alternates,
    }
    try:
        resp = requests.post(ROUTE_URL, json=body, params={"api_key": _key()}, timeout=20)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise MapsError(f"Routing request failed: {e}") from e
    if not resp.ok:
        code, message = data.get("error_code"), data.get("error", str(data)[:300])
        if code == 442:  # "No path could be found for input"
            return []
        if code == 171:  # "No suitable edges near location"
            names = ", ".join(p["name"] for p in places)
            raise MapsError(f"One of these places isn't near a bikeable path ({names}): {message}")
        raise MapsError(f"Routing returned HTTP {resp.status_code} (error {code}): {message}")

    trips = [data["trip"]] + [a["trip"] for a in data.get("alternates", [])]
    return [_parse_trip(t) for t in trips]
