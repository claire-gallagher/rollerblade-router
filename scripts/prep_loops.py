"""One-time prep: ready-made skating loops and out-and-backs, verified with LiDAR and bike lanes.

Open-ended requests ("suggest 5-mile beginner routes") used to cost the agent ~20 tool calls of
trial and error. This does that search once:
  - pair each one-way protected corridor with a parallel one running the other way nearby
    (8th Ave up / 9th Ave down) into a loop over the stretch they share
  - turn each two-way protected corridor (greenways, esplanades) into an out-and-back
  - route each with Valhalla, grade it, and if it fails for beginners or intermediates, keep the
    longest shortened version that passes

Run: uv run scripts/prep_loops.py      (writes data/loops.json; needs corridors.json and STADIA_API_KEY)
"""

import json
import math
import sys
from pathlib import Path

from dotenv import load_dotenv
from shapely import LineString

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bike_lanes  # noqa: E402
import grade  # noqa: E402
import stadia  # noqa: E402
from bike_lanes import to_latlng, to_xy  # noqa: E402

load_dotenv()

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "loops.json"
MIN_SPAN_M = 600  # shortest shared stretch worth making a loop of
PAIR_GAP_M = (60, 900)  # how far apart two paired corridors may run
MAX_ANGLE_DEG = 25  # how far from exactly opposite their directions may be
MIN_PROTECTED = 0.6  # the routed loop must actually stay on protected lanes
TRIMS = (0.15, 0.3, 0.45, 0.6)  # fractions of the span to cut when shortening a loop that fails

LANES = bike_lanes.load()


def line(c: dict) -> LineString:
    return LineString([to_xy(lat, lng) for lat, lng in c["line"]])


def unit(ln: LineString) -> tuple[float, float]:
    (x0, y0), (x1, y1) = ln.coords[0], ln.coords[-1]
    d = math.hypot(x1 - x0, y1 - y0)
    return (x1 - x0) / d, (y1 - y0) / d


def stop(ln: LineString, s: float) -> dict:
    p = ln.interpolate(s)
    lat, lng = to_latlng(p.x, p.y)
    return {"name": f"{lat:.6f},{lng:.6f}", "lat": lat, "lng": lng}


def describe(streets: list[str]) -> str:
    """'up 8th Avenue, across West 30th Street, down 9th Avenue, back on West 14th Street' style."""
    if len(streets) == 4:
        return f"{streets[0]}, across {streets[1]}, back on {streets[2]}, then {streets[3]}"
    return " → ".join(streets[:6])


def evaluate(stops: list[dict]) -> dict | None:
    """Route the stops, grade the result, and summarize it; None if it wanders off protected lanes."""
    try:
        routes = stadia.compute_bike_routes(stops, alternates=0, use_hills=0.0)
    except stadia.MapsError:
        return None
    if not routes:
        return None
    r = routes[0]
    a = grade.analyze_route(r["steps"])
    tiers = LANES.match(a.points)
    shares = bike_lanes.shares(tiers, a.dist_m)
    if shares.get("protected", 0) < MIN_PROTECTED:
        return None
    suitability = grade.suitability(a)
    streets = list(dict.fromkeys(s["street"] for s in r["steps"] if s["street"]))
    return {
        "miles": round(r["distance_m"] / 1609.34, 2),
        "minutes": round(r["duration_s"] / 60),
        "bike_facilities": shares,
        "steepest_climb_pct": grade.steepest(a, "up")[0],
        "steepest_descent_pct": grade.steepest(a, "down")[0],
        "suitable_for": {lvl: v["verdict"] for lvl, v in suitability.items()},
        "short_climbs_to_walk": {lvl: v["short_climbs_to_walk"] for lvl, v in suitability.items()},
        "streets": streets,
        "stops": [s["name"] for s in stops],
        "center": [round(sum(p[0] for p in a.points) / len(a.points), 5),
                   round(sum(p[1] for p in a.points) / len(a.points), 5)],
    }


def spans(lo: float, hi: float):
    """The full span, then shorter ones: trimmed from the top, the bottom, or both ends."""
    yield lo, hi
    length = hi - lo
    for f in TRIMS:
        cut = f * length
        for span in ((lo, hi - cut), (lo + cut, hi), (lo + cut / 2, hi - cut / 2)):
            if span[1] - span[0] >= MIN_SPAN_M:
                yield span


def best_versions(build) -> list[dict]:
    """The full version, plus the longest shortened version that passes for intermediates and for
    beginners, if the full one doesn't."""
    tried = {}

    def get(span):
        if span not in tried:
            tried[span] = evaluate(build.stops(*span))
        return tried[span]

    versions = [v for v in [get(build.span)] if v]
    for level in ("intermediate", "beginner"):
        if any(v["suitable_for"][level] == "yes" for v in versions):
            continue
        for span in spans(*build.span):
            r = get(span)
            if r and r["suitable_for"][level] == "yes":
                if r not in versions:
                    versions.append(r)
                break
    return versions


class Loop:
    """A loop up corridor a and back down corridor b over the stretch they share."""

    def __init__(self, a: dict, b: dict, la: LineString, lb: LineString, lo: float, hi: float):
        self.a, self.b, self.la, self.lb, self.span = a, b, la, lb, (lo, hi)

    def stops(self, lo: float, hi: float) -> list[dict]:
        a0, a1 = stop(self.la, lo), stop(self.la, hi)
        b_top = stop(self.lb, self.lb.project(self.la.interpolate(hi)))
        b_bottom = stop(self.lb, self.lb.project(self.la.interpolate(lo)))
        return [a0, a1, b_top, b_bottom, a0]


class OutAndBack:
    def __init__(self, c: dict, ln: LineString):
        self.c, self.ln, self.span = c, ln, (0.0, ln.length)

    def stops(self, lo: float, hi: float) -> list[dict]:
        start = stop(self.ln, lo)
        return [start, stop(self.ln, hi), start]


def main():
    corridors = json.loads((DATA / "corridors.json").read_text())["corridors"]
    one_way = [c for c in corridors if not c["two_way"]]
    cos_max = math.cos(math.radians(MAX_ANGLE_DEG))

    builds = []
    for a in one_way:
        if a["direction"] not in ("uptown", "crosstown east"):
            continue  # each pair once: from its uptown (or eastbound) side
        la = line(a)
        for b in one_way:
            lb = line(b)
            ua, ub = unit(la), unit(lb)
            if ua[0] * ub[0] + ua[1] * ub[1] > -cos_max:
                continue  # not running the opposite way
            gap = la.distance(lb.interpolate(0.5, normalized=True))
            if not PAIR_GAP_M[0] <= gap <= PAIR_GAP_M[1]:
                continue
            p1, p2 = la.project(lb.interpolate(0)), la.project(lb.interpolate(lb.length))
            lo, hi = max(0.0, min(p1, p2)), min(la.length, max(p1, p2))
            if hi - lo >= MIN_SPAN_M:
                builds.append(("loop", f"{a['street']} / {b['street']}", Loop(a, b, la, lb, lo, hi)))

    seen = set()
    for c in corridors:
        if c["two_way"]:
            key = (c["street"], *sorted([tuple(c["start"]), tuple(c["end"])]))
            if key not in seen:
                seen.add(key)
                builds.append(("out-and-back", c["street"], OutAndBack(c, line(c))))

    loops = []
    for n, (kind, label, build) in enumerate(builds, 1):
        versions = best_versions(build)
        for v in versions:
            loops.append({"id": f"l{len(loops) + 1}", "kind": kind, "name": f"{label}: {describe(v['streets'])}", **v})
        verdicts = ", ".join(f"{v['miles']} mi beginner {v['suitable_for']['beginner']}" for v in versions) or "none usable"
        print(f"  [{n}/{len(builds)}] {kind:12s} {label[:45]:45s} {verdicts}", flush=True)

    OUT.write_text(json.dumps({"source": "Valhalla routes over NYC DOT protected corridors, graded with 2017 NYC LiDAR",
                               "loops": loops}, indent=1))
    print(f"Done: {len(loops)} loops -> {OUT}")


if __name__ == "__main__":
    main()
