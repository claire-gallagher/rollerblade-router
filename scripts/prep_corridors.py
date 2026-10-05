"""One-time prep: a catalog of protected skating corridors in Manhattan, graded in each legal direction.

Built from NYC DOT's bike facilities (data/bike_routes_manhattan.geojson) and the LiDAR raster:
  - keep facilities that are Protected in a direction of travel, and orient them that way
  - join touching segments of the same street into continuous corridors
  - grade each corridor with LiDAR and give a verdict per rider level
  - name the ends from DOT's own from/to street fields

The agent uses this to suggest routes from data instead of from memory.
Run: uv run scripts/prep_corridors.py      (writes data/corridors.json; needs the two prep files above)
"""

import json
import math
import sys
from pathlib import Path

from shapely import LineString, MultiLineString
from shapely.ops import linemerge

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import grade  # noqa: E402
from bike_lanes import DATA_PATH, to_latlng, to_xy  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "corridors.json"
MIN_MILES = 0.5
SNAP_M = 2  # round coordinates so touching DOT segments share exact endpoints


def snap(x: float, y: float) -> tuple[float, float]:
    return round(x / SNAP_M) * SNAP_M, round(y / SNAP_M) * SNAP_M


def compass(a: tuple[float, float], b: tuple[float, float]) -> str:
    """Direction of travel from a to b. Manhattan's grid is rotated ~29 degrees, so 'north' means uptown."""
    angle = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) - 29  # 0 = up the avenues
    return ["uptown", "crosstown east", "downtown", "crosstown west"][round((angle % 360) / 90) % 4]


WORDS = {"AV": "Avenue", "AVE": "Avenue", "ST": "St", "DR": "Drive", "BR": "Bridge", "W": "W", "E": "E"}


def title(name: str | None) -> str:
    """'7 AVE' -> '7 Avenue', 'WEST DR' -> 'West Drive'. A few DOT name fields hold junk (embedded XML)."""
    if not name or len(name) > 50 or "<" in name:
        return ""
    return " ".join(WORDS.get(w, w.title()) for w in (name or "").split())


def main():
    features = json.loads(Path(DATA_PATH).read_text())["features"]
    one_way: dict[str, list[LineString]] = {}  # oriented in the direction you may skate
    two_way: dict[str, list[LineString]] = {}
    ends: dict[tuple, str] = {}  # snapped endpoint -> the cross street DOT names there
    for f in features:
        p, g = f["properties"], f["geometry"]
        forward, backward = p.get("ft_facilit") == "Protected", p.get("tf_facilit") == "Protected"
        for part in g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]:
            xy = [snap(*to_xy(lat, lng)) for lng, lat in part]
            if len(xy) < 2 or not (forward or backward):
                continue
            ends.setdefault(xy[0], p.get("fromstreet"))
            ends.setdefault(xy[-1], p.get("tostreet"))
            if forward and backward:
                two_way.setdefault(p["street"], []).append(LineString(xy))
            else:
                one_way.setdefault(p["street"], []).append(LineString(xy if forward else xy[::-1]))

    corridors = []
    for lines_by_street, directed in [(one_way, True), (two_way, False)]:
        for street, lines in lines_by_street.items():
            merged = linemerge(MultiLineString(lines), directed=directed)
            for line in merged.geoms if merged.geom_type == "MultiLineString" else [merged]:
                miles = line.length / 1609.34
                if miles < MIN_MILES:
                    continue
                coords = list(line.coords)
                # A two-way path is graded both ways; a one-way lane only the way you may skate it.
                for c in [coords] if directed else [coords, coords[::-1]]:
                    points = [to_latlng(x, y) for x, y in c]
                    a = grade.analyze_route([{"street": title(street), "points": points}])
                    corridors.append({
                        "id": f"c{len(corridors) + 1}",
                        "street": title(street),
                        "from": title(ends.get(c[0])),
                        "to": title(ends.get(c[-1])),
                        "direction": compass(points[0], points[-1]),
                        "two_way": not directed,
                        "miles": round(miles, 2),
                        "steepest_up_pct": grade.steepest(a, "up")[0],
                        "steepest_down_pct": grade.steepest(a, "down")[0],
                        "suitability": grade.suitability(a),
                        "start": [round(points[0][0], 6), round(points[0][1], 6)],
                        "end": [round(points[-1][0], 6), round(points[-1][1], 6)],
                        # The corridor's shape, simplified to ~10 m, for building loops on it.
                        "line": [[round(lat, 6), round(lng, 6)] for lat, lng in
                                 (to_latlng(x, y) for x, y in LineString(c).simplify(10).coords)],
                    })
                    k = corridors[-1]
                    print(f"  {k['id']:5s} {miles:5.2f} mi {k['street'][:30]:30s} {k['direction']:15s} "
                          f"{'2-way' if k['two_way'] else '1-way'} beginner {k['suitability']['beginner']['verdict']:7s} {k['from']} -> {k['to']}")

    OUT.write_text(json.dumps({"source": "NYC DOT bike facilities (Protected only) + 2017 NYC LiDAR", "corridors": corridors}, indent=1))
    print(f"Done: {len(corridors)} corridors -> {OUT}")


if __name__ == "__main__":
    main()
