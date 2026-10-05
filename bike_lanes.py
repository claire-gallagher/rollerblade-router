"""Which kind of bike facility each part of a route uses, from NYC DOT's bike route map.

Data: data/bike_routes_manhattan.geojson, built by scripts/prep_bike_lanes.py. Each DOT segment has a
facility type for each direction of travel, so a one-way protected lane only counts when the route
goes the lane's way.
"""

import json
import math
from pathlib import Path

import numpy as np
from shapely import LineString, Point, STRtree

from settings import DATA_DIR

DATA_PATH = str(DATA_DIR / "bike_routes_manhattan.geojson")
MATCH_M = 15  # how far a route point may be from a DOT segment and still be "on" it
MAX_ANGLE_DEG = 35  # segments at a sharper angle than this to the route are cross streets

# DOT facility types grouped by what they mean on skates, best first.
TIERS = ["protected", "painted", "shared", "none", "unpaved"]
TIER_OF = {
    "Protected": "protected",
    "Ped Plaza": "protected",  # car-free
    "Conventional": "painted",
    "Conventional Buffered": "painted",
    "Curbside": "painted",
    "Curbside Buffered": "painted",
    "Shared": "shared",
    "Wide Parking Lane": "shared",
    "Signed Route": "shared",
    "Link": "shared",
    "Sidewalk": "shared",
    "Unpaved": "unpaved",
}
RANK = {"protected": 0, "painted": 1, "shared": 2, "unpaved": 3}

# A flat local projection in meters is plenty accurate across Manhattan.
LAT0, LNG0 = 40.78, -73.97
M_PER_DEG_LAT = 111_000
M_PER_DEG_LNG = 111_000 * math.cos(math.radians(LAT0))


def to_xy(lat: float, lng: float) -> tuple[float, float]:
    return (lng - LNG0) * M_PER_DEG_LNG, (lat - LAT0) * M_PER_DEG_LAT


def to_latlng(x: float, y: float) -> tuple[float, float]:
    return LAT0 + y / M_PER_DEG_LAT, LNG0 + x / M_PER_DEG_LNG


class BikeLanes:
    def __init__(self, path: str):
        data = json.loads(Path(path).read_text())
        self.lines, self.forward, self.backward, self.streets = [], [], [], []
        for f in data["features"]:
            props, geom = f["properties"], f["geometry"]
            parts = geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]
            for part in parts:
                if len(part) < 2:
                    continue
                self.lines.append(LineString([to_xy(lat, lng) for lng, lat in part]))
                self.streets.append(props.get("street") or "")
                # ft = along the segment's drawn direction, tf = against it.
                self.forward.append(TIER_OF.get(props.get("ft_facilit")))
                self.backward.append(TIER_OF.get(props.get("tf_facilit")))
        self.tree = STRtree(self.lines)

    def match(self, points: list[tuple[float, float]]) -> list[str]:
        """The best facility tier at each route point, for the route's direction of travel."""
        n = len(points)
        if n == 0:
            return []
        xy = np.array([to_xy(lat, lng) for lat, lng in points])
        tiers = ["none"] * n
        cos_max = math.cos(math.radians(MAX_ANGLE_DEG))
        pt_idx, line_idx = self.tree.query([Point(p) for p in xy], predicate="dwithin", distance=MATCH_M)
        for i, j in zip(pt_idx, line_idx):
            travel = xy[min(i + 1, n - 1)] - xy[max(i - 1, 0)]
            line = self.lines[j]
            s = line.project(Point(xy[i]))
            a, b = line.interpolate(max(s - 2, 0)), line.interpolate(min(s + 2, line.length))
            seg = np.array([b.x - a.x, b.y - a.y])
            norm = np.linalg.norm(travel) * np.linalg.norm(seg)
            if norm == 0:
                continue
            cos = float(travel @ seg) / norm
            tier = self.forward[j] if cos >= cos_max else self.backward[j] if cos <= -cos_max else None
            if tier and RANK[tier] < RANK.get(tiers[i], 99):
                tiers[i] = tier
        return tiers


    def named(self, names: list[str]) -> list[LineString]:
        """DOT segments whose street contains one of the names, e.g. 'Hudson River Greenway'."""
        wanted = [n.upper() for n in names]
        return [line for line, street in zip(self.lines, self.streets) if any(w in street for w in wanted)]

    def miles_on(self, names: list[str], points: list[tuple[float, float]], dist_m: list[float]) -> float:
        """How many miles of the route run along the named corridors."""
        lines = self.named(names)
        if not lines or len(points) < 2:
            return 0.0
        tree = STRtree(lines)
        hits, _ = tree.query([Point(to_xy(lat, lng)) for lat, lng in points], predicate="dwithin", distance=MATCH_M)
        on = set(hits.tolist())
        meters = sum(dist_m[i + 1] - dist_m[i] for i in range(len(points) - 1) if i in on and i + 1 in on)
        return round(meters / 1609.34, 2)



_lanes: BikeLanes | None = None


def load() -> BikeLanes | None:
    """The DOT bike lanes, loaded on first use; None if scripts/prep_bike_lanes.py hasn't been run."""
    global _lanes
    if _lanes is None and Path(DATA_PATH).exists():
        _lanes = BikeLanes(DATA_PATH)
    return _lanes


def shares(tiers: list[str], dist_m: list[float]) -> dict[str, float]:
    """Fraction of the route's length on each tier, e.g. {'protected': 0.72, 'painted': 0.1, ...}."""
    length = {t: 0.0 for t in TIERS}
    for i in range(len(tiers) - 1):
        length[tiers[i]] += dist_m[i + 1] - dist_m[i]
    total = sum(length.values()) or 1.0
    return {t: round(v / total, 2) for t, v in length.items() if v > 0}


def tier_between(tiers: list[str], dist_m: list[float], start_m: float, end_m: float) -> str:
    """The most common tier between two distances along the route (e.g. for one steep stretch)."""
    inside = [t for t, d in zip(tiers, dist_m) if start_m <= d <= end_m]
    return max(set(inside), key=inside.count) if inside else "none"
