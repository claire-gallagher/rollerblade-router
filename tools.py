"""The agent's tools. Each returns a JSON string written for the model to read.

Route geometry and per-point grades stay server-side in ROUTES, keyed by route_id; the model gets
compact summaries. The frontend fetches geometry from GET /routes/{route_id}.
"""

import json
import math
import threading
import uuid
from collections import OrderedDict

import bike_lanes
import grade
import places
import stadia
from grade import RIDER_LEVELS
from settings import DATA_DIR
from stadia import MapsError

ROUTES: OrderedDict[str, dict] = OrderedDict()  # route_id -> everything about one planned route
MAX_ROUTES = 300  # in memory only; oldest dropped first
LOOPS_PATH = DATA_DIR / "loops.json"
_routes_lock = threading.Lock()  # tool calls in one model turn run in parallel threads
M_PER_MILE = 1609.34


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _miles(m: float) -> float:
    return round(m / M_PER_MILE, 2)


def _store(record: dict) -> str:
    route_id = uuid.uuid4().hex[:8]
    with _routes_lock:
        ROUTES[route_id] = record
        while len(ROUTES) > MAX_ROUTES:
            ROUTES.popitem(last=False)
    return route_id


def cue_sheet(steps: list[dict]) -> list[dict]:
    """Turn-by-turn steps for the rider. Unnamed ones ('Turn right.') say which street comes next."""
    out = []
    for i, s in enumerate(steps):
        text = s["instruction"]
        if not s["street"]:
            following = next((t["street"] for t in steps[i + 1:] if t["street"]), None)
            if following:
                text = f"{text.rstrip('.')} (path toward {following})."
        out.append({"instruction": text, "miles": _miles(s["distance_m"])})
    return out


def _stretch_summary(s: grade.Stretch, tiers: list[str], dist_m: list[float]) -> dict:
    between = " and ".join(n for n in (s.prev_street, s.next_street) if n)
    return {
        "street": s.street,
        "between": between or None,
        "direction": "downhill" if s.direction == "down" else "uphill",
        "max_grade_pct": s.max_grade_pct,
        "sustained_grade_pct": s.sustained_grade_pct,
        "length_m": round(s.length_m),
        "elevation_change_m": s.elevation_change_m,
        "at_mile": _miles(s.start_m),
        "bike_facility": bike_lanes.tier_between(tiers, dist_m, s.start_m, s.end_m),
        "walkable_climb": grade.is_walkable_climb(s),
        "possible_artifact": s.possible_artifact,
    }


def plan_skate_route(
    origin: str,
    destination: str,
    waypoints: list[str] | None = None,
    max_uphill_pct: float = 4,
    max_downhill_pct: float = 3,
    avoid: list[str] | None = None,
) -> str:
    """Plan skating routes between places and check every stretch's grade and bike-lane coverage.

    Returns up to 3 routes (fewer when waypoints are given), best first. Each has a route_id for
    get_grade_details, its distance and time, the share on protected bike lanes, the steepest climb
    and descent, stretches over the limits, and a verdict for beginner/intermediate/advanced riders.
    For a loop, pass the same place as origin and destination and add waypoints. For an out-and-back,
    use one waypoint at the turnaround.

    Args:
        origin: Where the ride starts: an address with house number ("2 Broadway"), an intersection
            ("Riverside Dr & W 96 St"), a landmark's official name ("The Battery"), a Central Park drive
            point ("East Drive at E 90 St"), or "lat,lng" from find_place or find_skate_loops.
        destination: Where the ride ends, in the same forms as origin.
        waypoints: Places to pass through in order, in the same forms. Use these to steer around a
            hill, to stay on the Central Park drives, or to shape a loop to a target distance.
        max_uphill_pct: Steepest climb the rider accepts, in percent. Beginner 4, intermediate 6, advanced 10.
        max_downhill_pct: Steepest descent the rider accepts, in percent. Beginner 3, intermediate 5, advanced 8.
        avoid: Corridor names the rider wants to stay off, as NYC DOT names them, e.g.
            ["Hudson River Greenway"]. Each route reports how many miles it still uses them; steer
            off with waypoints on a parallel street.
    """
    waypoints, avoid = waypoints or [], avoid or []
    try:
        stops = [places.resolve(p) for p in (origin, *waypoints, destination)]
    except MapsError as e:
        return _error(str(e))

    lanes = bike_lanes.load()
    try:
        routes = stadia.compute_bike_routes(
            stops, use_hills=0.0 if max_downhill_pct <= 5 else 0.3, alternates=0 if waypoints else 2)
    except MapsError as e:
        return _error(str(e))
    if not routes:
        names = " to ".join(p["name"] for p in stops)
        return _error(f"No skateable route found from {names}. Check the places with find_place, or try nearby ones.")

    results = []
    for r in routes:
        a = grade.analyze_route(r["steps"])
        tiers = lanes.match(a.points) if lanes else ["none"] * len(a.points)
        stretches = grade.find_stretches(a, max_uphill_pct, max_downhill_pct)
        real = [s for s in stretches if not s.possible_artifact]
        blocking = [s for s in real if not grade.is_walkable_climb(s)]
        gaps = grade.data_gaps(a)
        meets = False if blocking else "unknown" if gaps else True
        up, down = grade.steepest(a, "up"), grade.steepest(a, "down")
        on_avoided = lanes.miles_on(avoid, a.points, a.dist_m) if (lanes and avoid) else 0.0

        route_id = _store({
            "route": r, "analysis": a, "tiers": tiers, "stops": stops,
            "limits": {"max_uphill_pct": max_uphill_pct, "max_downhill_pct": max_downhill_pct},
        })
        summary = {
            "route_id": route_id,
            "description": r["description"],
            "miles": _miles(r["distance_m"]),
            "minutes": round(r["duration_s"] / 60),
            "bike_facilities": bike_lanes.shares(tiers, a.dist_m),
            "steepest_climb": {"grade_pct": up[0], "street": up[1]},
            "steepest_descent": {"grade_pct": down[0], "street": down[1]},
            "stretches_over_limits": {
                "downhill": sum(s.direction == "down" for s in real),
                "uphill_too_long_to_walk": sum(s.direction == "up" for s in blocking),
                "short_climbs_to_walk": len(real) - len(blocking),
            },
            "meets_limits": meets,
            "suitable_for": {lvl: v["verdict"] for lvl, v in grade.suitability(a).items()},
            "streets_in_order": list(dict.fromkeys(s["street"] for s in r["steps"] if s["street"])),
        }
        if stretches and len(real) < len(stretches):
            summary["possible_artifacts"] = len(stretches) - len(real)
        if gaps:
            summary["no_grade_data"] = [f"{g['streets'][0]} (mile {_miles(g['start_m'])}-{_miles(g['end_m'])})" for g in gaps]
        if avoid:
            summary["miles_on_avoided_corridors"] = on_avoided
        results.append(summary)

    # Best first: meets the limits, then more protected lane, then shorter.
    results.sort(key=lambda s: (s["meets_limits"] is not True, -s["bike_facilities"].get("protected", 0), s["miles"]))
    out = {
        "resolved_places": [p["name"] for p in stops],
        "limits": {"max_uphill_pct": max_uphill_pct, "max_downhill_pct": max_downhill_pct},
        "routes": results,
    }
    if all(s["meets_limits"] is not True for s in results):
        worst = results[0]["steepest_descent"]
        out["note"] = (f"No route meets the limits (e.g. a {worst['grade_pct']}% descent on {worst['street']}). "
                       "Call get_grade_details on the best route_id and add a waypoint to steer around the problem stretch.")
    if avoid and any(s["miles_on_avoided_corridors"] > 0.1 for s in results):
        out["note_avoid"] = ("Some routes use the avoided corridors (see miles_on_avoided_corridors). "
                             "Add waypoints on a parallel street to stay off them.")
    return json.dumps(out)


def get_grade_details(route_id: str) -> str:
    """The steep stretches of a planned route, to decide where a detour waypoint would help.

    Lists every stretch over the route's limits (or the steepest few if none are), with the street,
    the streets before and after it, how steep and long it is, and what bike facility it is on.

    Args:
        route_id: A route_id returned by plan_skate_route in this conversation.
    """
    record = ROUTES.get(route_id)
    if not record:
        return _error(f"route_id {route_id} not found. Call plan_skate_route first.")
    a, tiers, limits = record["analysis"], record["tiers"], record["limits"]
    stretches = grade.find_stretches(a, limits["max_uphill_pct"], limits["max_downhill_pct"])
    over_limits = bool(stretches)
    if not stretches:
        stretches = sorted(grade.find_stretches(a, 1.5, 1.5), key=lambda s: -s.max_grade_pct)[:3]
    return json.dumps({
        "route_id": route_id,
        "limits": limits,
        "showing": "stretches over the limits" if over_limits else "no stretch is over the limits; these are the steepest",
        "stretches": [_stretch_summary(s, tiers, a.dist_m) for s in stretches],
        "cue_sheet": cue_sheet(record["route"]["steps"]),
    })


def find_place(query: str) -> str:
    """Look up a place in Manhattan and get candidates to use as an origin, destination, or waypoint.

    Use this when a place name is ambiguous or plan_skate_route could not find it. Pass a landmark's
    official name without the city ("General Grant National Memorial", not "Grant's Tomb, NYC"), a
    street address with house number, an intersection ("Broadway & W 110 St"), or a Central Park drive
    point ("West Drive at W 86 St", "102nd Street Crossing").

    Args:
        query: The place to look up.
    """
    try:
        found = places.find_places(query)
    except MapsError as e:
        return _error(str(e))
    if not found:
        return _error(f"Nothing found for '{query}' in Manhattan. Try the official name, a street address, or an intersection.")
    return json.dumps({"candidates": [
        {"name": c["name"], "type": c["type"], "source": c["source"], "use_as": f"{c['lat']:.6f},{c['lng']:.6f}",
         **({"note": c["note"]} if c.get("note") else {})}
        for c in found
    ]})


_loops: list[dict] | None = None


def _load_loops() -> list[dict]:
    global _loops
    if _loops is None:
        _loops = json.loads(LOOPS_PATH.read_text())["loops"] if LOOPS_PATH.exists() else []
    return _loops


def find_skate_loops(level: str = "beginner", target_miles: float | None = None, near: str | None = None,
                     avoid: list[str] | None = None, limit: int = 5) -> str:
    """Ready-made skating loops and out-and-backs in Manhattan, already checked for a rider level.

    Use this first for open-ended requests ("suggest 5-mile beginner routes", "somewhere flat near
    me"). Every result runs mostly on protected bike lanes and greenways and has been graded with
    LiDAR; one-way lanes are paired so the loop follows each lane's direction. Short loops come with
    a lap count to reach target_miles. To show one on the map, call plan_skate_route with its
    plan_with places.

    Args:
        level: "beginner", "intermediate", or "advanced".
        target_miles: The ride length the rider wants; loops are ranked by how close laps get to it.
        near: A neighborhood, landmark, or address to rank nearby loops first.
        avoid: Street or corridor names to leave out, e.g. ["Hudson River Greenway"].
        limit: How many to return.
    """
    if level not in RIDER_LEVELS:
        return _error(f"Unknown level '{level}'. Use one of: {', '.join(RIDER_LEVELS)}.")
    loops = _load_loops()
    if not loops:
        return _error("The loop catalog is missing on the server (run scripts/prep_data.py).")
    center = None
    if near:
        try:
            center = places.resolve(near)
        except MapsError as e:
            return _error(str(e))
    avoid_upper = [a.upper() for a in (avoid or [])]

    def laps(loop):
        return max(1, round(target_miles / loop["miles"])) if target_miles else 1

    def score(loop):
        fit = abs(laps(loop) * loop["miles"] - target_miles) / target_miles + 0.05 * (laps(loop) - 1) if target_miles else -loop["miles"]
        if center:
            fit += 0.15 * math.hypot((loop["center"][0] - center["lat"]) * 111, (loop["center"][1] - center["lng"]) * 84)
        return fit

    fits = [
        loop for loop in loops
        if loop["suitable_for"][level] == "yes"
        and not any(a in loop["name"].upper() or any(a in s.upper() for s in loop["streets"]) for a in avoid_upper)
    ]
    fits.sort(key=score)
    picked, pairs = [], set()
    for loop in fits:
        pair = loop["name"].split(":")[0]  # one version per corridor pair
        if pair not in pairs:
            pairs.add(pair)
            picked.append(loop)
        if len(picked) == limit:
            break
    if not picked:
        return _error(f"No ready-made loop suits a {level} rider with those constraints. "
                      "Build one with plan_skate_route instead.")
    return json.dumps({"level": level, "limits": RIDER_LEVELS[level], "loops": [
        {
            "name": loop["name"], "kind": loop["kind"], "miles": loop["miles"], "minutes": loop["minutes"],
            **({"laps": laps(loop), "total_miles": round(laps(loop) * loop["miles"], 2)} if target_miles else {}),
            "bike_facilities": loop["bike_facilities"],
            "steepest_climb_pct": loop["steepest_climb_pct"], "steepest_descent_pct": loop["steepest_descent_pct"],
            "short_climbs_to_walk": loop["short_climbs_to_walk"][level],
            "plan_with": {"origin": loop["stops"][0], "waypoints": loop["stops"][1:-1], "destination": loop["stops"][-1]},
        }
        for loop in picked
    ]})


def route_geojson(route_id: str) -> dict | None:
    """A planned route as GeoJSON for the map: one feature per run of similar grade and lane type.

    "over_down" / "over_up" mark the same stretches the tools report as over the limits, so the
    map and the chat agree; "artifact" marks likely bridge or overpass readings. properties.steep_spots
    lists those stretches, with which way each one slopes and where it is.
    """
    record = ROUTES.get(route_id)
    if not record:
        return None
    a, tiers, limits = record["analysis"], record["tiers"], record["limits"]
    stretches = grade.find_stretches(a, limits["max_uphill_pct"], limits["max_downhill_pct"])

    def stretch_at(d: float):
        return next((s for s in stretches if s.start_m <= d <= s.end_m), None)

    features, run = [], None
    for i in range(len(a.points) - 1):
        g, s = a.short_pct[i], stretch_at(a.dist_m[i])
        if s:
            cls = "artifact" if s.possible_artifact else "over_down" if s.direction == "down" else "over_up"
        else:
            cls = "nodata" if g is None else "moderate" if abs(g) >= 2 else "flat"
        key = (cls, tiers[i])
        if run is None or run["key"] != key:
            run = {"key": key, "coords": [[a.points[i][1], a.points[i][0]]], "grades": []}
            features.append(run)
        run["coords"].append([a.points[i + 1][1], a.points[i + 1][0]])
        if g is not None:
            run["grades"].append(abs(g))

    def where(d: float) -> list[float]:
        i = min(range(len(a.dist_m)), key=lambda k: abs(a.dist_m[k] - d))
        return [round(a.points[i][0], 6), round(a.points[i][1], 6)]

    steep_spots = [
        {**_stretch_summary(s, tiers, a.dist_m), "start": where(s.start_m), "end": where(s.end_m)}
        for s in stretches if not s.possible_artifact
    ]
    return {
        "type": "FeatureCollection",
        "properties": {"limits": limits, "stops": record["stops"], "steep_spots": steep_spots,
                       "cue_sheet": cue_sheet(record["route"]["steps"])},
        "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": f["coords"]},
             "properties": {"grade_class": f["key"][0], "bike_facility": f["key"][1],
                            "max_grade_pct": round(max(f["grades"]), 1) if f["grades"] else None}}
            for f in features
        ],
    }
