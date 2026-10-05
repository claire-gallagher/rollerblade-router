"""NYC Geoclient v2 (NYC Department of City Planning): the city's official geocoder.

Used for street addresses and intersections, which it resolves against the city's own street
network (LION). Landmarks go to Pelias instead: Geoclient's place-name list is hit and miss.
"""

import os
import re

import requests

URL = "https://api.nyc.gov/geoclient/v2/search.json"


class GeoclientError(Exception):
    """Geoclient failed. The caller falls back to Pelias, so this never reaches the model directly."""


def _title(name: str) -> str:
    """'WEST   96 STREET' -> 'West 96 Street'."""
    return re.sub(r"\s+", " ", name).strip().title().replace("'S", "'s")


def search(query: str) -> list[dict]:
    """Addresses and intersections matching free text, e.g. '2 Broadway' or 'Riverside Dr and W 96 St'.

    Returns {name, lat, lng, type, source} for each match; [] if Geoclient finds no address or intersection.
    """
    key = os.environ.get("NYC_GEOCLIENT_KEY")
    if not key:
        raise GeoclientError("NYC_GEOCLIENT_KEY is not set.")
    try:
        resp = requests.get(URL, params={"input": query}, headers={"Ocp-Apim-Subscription-Key": key}, timeout=10)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise GeoclientError(f"Geoclient request failed: {e}") from e
    if not resp.ok:
        raise GeoclientError(f"Geoclient returned HTTP {resp.status_code}: {str(data)[:200]}")

    out = []
    for r in data.get("results", []):
        kind = r.get("request", "").split(" ", 1)[0]  # "address [houseNumber=...]" -> "address"
        s = r.get("response", {})
        if kind not in ("address", "intersection") or s.get("latitude") is None:
            continue
        if kind == "address":
            name = f"{s.get('houseNumber', '')} {_title(s.get('boePreferredStreetName') or s.get('firstStreetNameNormalized', ''))}"
        else:
            name = f"{_title(s.get('firstStreetNameNormalized', ''))} & {_title(s.get('secondStreetNameNormalized', ''))}"
        borough = _title(s.get("firstBoroughName", ""))
        out.append({
            "name": f"{name.strip()}, {borough} {s.get('zipCode', '')}".strip(),
            "lat": float(s["latitude"]),
            "lng": float(s["longitude"]),
            "type": kind,
            "source": "NYC Geoclient",
        })
    return out
