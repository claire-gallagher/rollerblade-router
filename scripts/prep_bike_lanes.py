"""One-time prep: download NYC DOT's current Manhattan bike facilities.

Source: NYC DOT, "New York City Bike Routes" (NYC Open Data, mzxg-pwib). Each row is a bike facility
on one LION street segment, with its facility type in each direction of travel.
https://data.cityofnewyork.us/dataset/New-York-City-Bike-Routes/mzxg-pwib

Run: uv run scripts/prep_bike_lanes.py      (writes data/bike_routes_manhattan.geojson)
"""

import json
from pathlib import Path

import requests

URL = "https://data.cityofnewyork.us/resource/mzxg-pwib.geojson"
FIELDS = "the_geom,street,fromstreet,tostreet,ft_facilit,tf_facilit,facilitycl,onoffst,gwsystem"
OUT = Path(__file__).resolve().parent.parent / "data" / "bike_routes_manhattan.geojson"


def main():
    OUT.parent.mkdir(exist_ok=True)
    params = {
        "$select": FIELDS,
        "$where": "boro='1' AND status='Current'",  # Manhattan; retired facilities left out
        "$limit": 50000,
    }
    resp = requests.get(URL, params=params, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    data["metadata"] = {"source": "NYC DOT, New York City Bike Routes (NYC Open Data mzxg-pwib)", "url": URL}
    OUT.write_text(json.dumps(data))
    print(f"Done: {len(data['features'])} current Manhattan facilities -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
