"""One-time prep: named waypoints on the Central Park drives, which geocoders can't find.

Riders and the agent need points like "West Drive at W 86 St" or "102nd Street Crossing" to keep a
route on the car-free drives. Coordinates come from data, not guesses:
  - drive geometry from OpenStreetMap (Overpass API)
  - cross-street anchors from NYC Geoclient ("5 Av & E 90 St"), snapped to the nearest point on the drive

Run: uv run scripts/prep_gazetteer.py      (writes data/gazetteer.json; needs NYC_GEOCLIENT_KEY)
"""

import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from shapely import LineString, Point
from shapely.ops import nearest_points

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geoclient  # noqa: E402
from bike_lanes import to_latlng, to_xy  # noqa: E402

load_dotenv()

OVERPASS = "https://overpass-api.de/api/interpreter"
QUERY = """[out:json][timeout:90];
way(40.764,-73.982,40.801,-73.949)["highway"]
   ["name"~"^(East Drive|West Drive|102nd Street Crossing|Terrace Drive)$"];
out geom;"""
OUT = Path(__file__).resolve().parent.parent / "data" / "gazetteer.json"

# Cross streets to anchor each drive at. East Drive runs north (counterclockwise), West Drive south.
EAST = [64, 72, 79, 84, 90, 96, 102]
WEST = [67, 72, 77, 81, 86, 96, 100, 106]


def main():
    resp = requests.post(OVERPASS, data={"data": QUERY}, timeout=120,
                         headers={"User-Agent": "skate-router (class project)"})
    resp.raise_for_status()
    ways = {}
    for e in resp.json()["elements"]:
        line = LineString([to_xy(p["lat"], p["lon"]) for p in e["geometry"]])
        ways.setdefault(e["tags"]["name"], []).append(line)

    entries = []

    def add(name, aliases, lat, lng, note):
        entries.append({"name": name, "aliases": aliases, "lat": round(lat, 6), "lng": round(lng, 6), "note": note})

    for name, aliases in [
        ("102nd Street Crossing", ["102nd Street Cross Drive", "102nd Street Transverse", "Central Park 102nd Street"]),
        ("Terrace Drive", ["72nd Street Cross Drive", "Central Park 72nd Street"]),
    ]:
        longest = max(ways[name], key=lambda l: l.length)
        lat, lng = to_latlng(*longest.interpolate(0.5, normalized=True).coords[0])
        add(f"Central Park {name}", aliases, lat, lng, "Car-free cross drive connecting East and West Drive.")

    for drive, avenue, prefix, streets, direction in [
        ("East Drive", "5 Av", "E", EAST, "northbound"),
        ("West Drive", "Central Park W", "W", WEST, "southbound"),
    ]:
        for n in streets:
            found = geoclient.search(f"{avenue} and {prefix} {n} St Manhattan")
            if not found:
                print(f"  no anchor for {avenue} & {prefix} {n} St, skipped")
                continue
            anchor = Point(to_xy(found[0]["lat"], found[0]["lng"]))
            snapped = min((nearest_points(line, anchor)[0] for line in ways[drive]), key=anchor.distance)
            lat, lng = to_latlng(snapped.x, snapped.y)
            add(f"Central Park {drive} at {prefix} {n} St",
                [f"{drive} at {prefix} {n} St", f"{drive} & {prefix} {n} St", f"{drive} {n}th"],
                lat, lng, f"On the car-free drive; skate it {direction} only (counterclockwise loop).")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"source": "OpenStreetMap (Overpass) + NYC Geoclient anchors", "places": entries}, indent=1))
    print(f"Done: {len(entries)} places -> {OUT}")


if __name__ == "__main__":
    main()
