"""Grade analysis for a route: resample it, look up elevations, and find stretches too steep to skate.

Grades are percent, signed by travel direction: positive is uphill, negative is downhill.
"""

import math
from collections import Counter
from dataclasses import dataclass

from elevation import get_elevations


# Grade limits (percent) by skating ability. Beginner comes from the project spec; the others are
# starting points to tune. Downhill limits are lower: braking on skates is the hard part.
RIDER_LEVELS = {
    "beginner": {"max_uphill_pct": 4, "max_downhill_pct": 3},
    "intermediate": {"max_uphill_pct": 6, "max_downhill_pct": 5},
    "advanced": {"max_uphill_pct": 10, "max_downhill_pct": 8},
}


SPACING_M = 5  # resample the route every this many meters
SHORT_WINDOW_M = 10  # catches brief steep stretches
LONG_WINDOW_M = 30  # measures sustained hills
ARTIFACT_PCT = 12  # steeper than this is probably a bridge/overpass, not a real hill
MERGE_GAP_M = 10  # join same-direction stretches closer than this
MIN_CHANGE_M = 1.0  # ignore hills that climb or drop less than this: too little to build speed


@dataclass
class Stretch:
    direction: str  # "up" or "down"
    start_m: float  # distance along the route
    end_m: float
    max_grade_pct: float  # steepest short-window grade, as a positive number
    sustained_grade_pct: float  # steepest long-window grade, as a positive number
    elevation_change_m: float  # meters climbed (up) or dropped (down) across the stretch
    street: str
    prev_street: str | None  # the streets before and after, as rough cross streets
    next_street: str | None
    possible_artifact: bool

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


@dataclass
class RouteAnalysis:
    points: list[tuple[float, float]]  # resampled (lat, lng)
    dist_m: list[float]  # distance along the route at each point
    streets: list[str]  # street at each point
    elevations: list[float | None]  # meters at each point; None where there's no data
    short_pct: list[float | None]  # short-window grade at each point
    long_pct: list[float | None]


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def step_streets(steps: list[dict]) -> list[str]:
    """Each step's street. Unnamed steps ('Turn right') take the previous step's street."""
    names = [s.get("street") for s in steps]
    first = next((n for n in names if n), "unnamed path")
    out, last = [], first
    for n in names:
        last = n or last
        out.append(last)
    return out


def resample(steps: list[dict], spacing_m: float) -> tuple[list[tuple[float, float]], list[float], list[str]]:
    """Points every spacing_m along the route's steps, with distance along the route and street at each."""
    streets = step_streets(steps)
    points, dists, names = [], [], []
    traveled, next_at, prev = 0.0, 0.0, None
    for step, street in zip(steps, streets):
        for p in step["points"]:
            if prev is None:
                points.append(p); dists.append(0.0); names.append(street)
                next_at, prev = spacing_m, p
                continue
            seg = haversine_m(prev, p)
            # Emit every sample that falls inside this segment, interpolating linearly.
            while seg > 0 and next_at <= traveled + seg:
                t = (next_at - traveled) / seg
                points.append((prev[0] + t * (p[0] - prev[0]), prev[1] + t * (p[1] - prev[1])))
                dists.append(next_at); names.append(street)
                next_at += spacing_m
            traveled += seg
            prev = p
    # Always end exactly at the destination.
    if prev is not None and traveled - dists[-1] > 0.5:
        points.append(prev); dists.append(traveled); names.append(streets[-1])
    return points, dists, names


def window_grades(dist_m: list[float], elev: list[float | None], window_m: float, spacing_m: float) -> list[float | None]:
    """Grade at each point over a window centered on it (clipped at the route's ends)."""
    half = max(1, round(window_m / 2 / spacing_m))
    n = len(dist_m)
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n - 1, i + half)
        if elev[a] is None or elev[b] is None or dist_m[b] <= dist_m[a]:
            out.append(None)
        else:
            out.append(100 * (elev[b] - elev[a]) / (dist_m[b] - dist_m[a]))
    return out


def analyze_route(steps: list[dict], elevation_fn=get_elevations) -> RouteAnalysis:
    """Resample a route (steps from stadia.compute_bike_routes), fetch elevations, and compute grades."""
    points, dists, streets = resample(steps, SPACING_M)
    elevations = elevation_fn(points)
    return RouteAnalysis(
        points=points,
        dist_m=dists,
        streets=streets,
        elevations=elevations,
        short_pct=window_grades(dists, elevations, SHORT_WINDOW_M, SPACING_M),
        long_pct=window_grades(dists, elevations, LONG_WINDOW_M, SPACING_M),
    )


def _direction(short: float | None, long: float | None, max_up: float, max_down: float) -> str | None:
    """'up' or 'down' if either window exceeds that direction's limit at this point, else None."""
    grades = [g for g in (short, long) if g is not None]
    if any(g > max_up for g in grades):
        return "up"
    if any(g < -max_down for g in grades):
        return "down"
    return None


# A hill continues while the grade stays steeper than this. Gentler than ~1.5%, rolling resistance
# keeps skates from picking up speed, so that ground does not add to the drop. Tunable.
HILL_PCT = 1.5


def _hill_extent(a: RouteAnalysis, lo: int, hi: int, sign: int) -> tuple[int, int]:
    """Widen [lo, hi] while the route keeps going the same way (up for sign 1, down for -1)."""
    g = a.short_pct
    while lo > 0 and g[lo - 1] is not None and sign * g[lo - 1] > HILL_PCT:
        lo -= 1
    while hi < len(g) - 1 and g[hi + 1] is not None and sign * g[hi + 1] > HILL_PCT:
        hi += 1
    return lo, hi


def find_stretches(a: RouteAnalysis, max_up: float, max_down: float) -> list[Stretch]:
    """Stretches where the short or long grade exceeds the limits, in route order.

    Stretches that change elevation by less than MIN_CHANGE_M are left out.
    """
    labels = [_direction(s, l, max_up, max_down) for s, l in zip(a.short_pct, a.long_pct)]

    # Runs of consecutive points with the same label, as [direction, first_index, last_index].
    runs = []
    for i, label in enumerate(labels):
        if label is None:
            continue
        if runs and runs[-1][0] == label and a.dist_m[i] - a.dist_m[runs[-1][2]] <= MERGE_GAP_M + SPACING_M:
            runs[-1][2] = i
        else:
            runs.append([label, i, i])

    half = max(1, round(SHORT_WINDOW_M / 2 / SPACING_M))
    elev = a.elevations
    # Speed builds over the whole hill, not just its steepest part: find where each climb or descent
    # begins and levels off. Steep runs on the same hill become one stretch, not several fragments
    # that each claim the whole hill's drop.
    hills = []
    for direction, i, j in runs:
        # Each point's grade spans half a window either side, so the stretch does too.
        lo, hi = max(0, i - half), min(len(a.dist_m) - 1, j + half)
        h0, h1 = _hill_extent(a, lo, hi, 1 if direction == "up" else -1)
        if hills and hills[-1]["direction"] == direction and lo <= hills[-1]["h1"]:
            hills[-1].update(j=j, hi=hi, h1=max(hills[-1]["h1"], h1))
        else:
            hills.append({"direction": direction, "i": i, "j": j, "lo": lo, "hi": hi, "h0": h0, "h1": h1})

    stretches = []
    for hill in hills:
        direction, i, j, lo, hi, h0, h1 = (hill[k] for k in ("direction", "i", "j", "lo", "hi", "h0", "h1"))
        short = [abs(g) for g in a.short_pct[i:j + 1] if g is not None]
        long = [abs(g) for g in a.long_pct[i:j + 1] if g is not None]
        change = abs(elev[h1] - elev[h0]) if elev[h1] is not None and elev[h0] is not None else 0.0
        street = Counter(a.streets[lo:hi + 1]).most_common(1)[0][0]
        before = [s for s in a.streets[:lo] if s != street]
        after = [s for s in a.streets[hi + 1:] if s != street]
        if change < MIN_CHANGE_M:
            continue
        max_grade = max(short, default=0.0)
        stretches.append(Stretch(
            direction=direction,
            start_m=a.dist_m[lo],
            end_m=a.dist_m[hi],
            max_grade_pct=round(max_grade, 1),
            sustained_grade_pct=round(max(long, default=0.0), 1),
            elevation_change_m=round(change, 1),
            street=street,
            prev_street=before[-1] if before else None,
            next_street=after[0] if after else None,
            possible_artifact=max_grade > ARTIFACT_PCT,
        ))
    return stretches


def steepest(a: RouteAnalysis, direction: str) -> tuple[float, str | None]:
    """Steepest real stretch in one direction: its short-window grade (as a positive %) and street.

    Uses the same rules as find_stretches, so bumps under min_change_m and likely artifacts are skipped.
    """
    candidates = [
        s for s in find_stretches(a, max_up=1.0, max_down=1.0)
        if s.direction == direction and not s.possible_artifact
    ]
    if not candidates:
        return 0.0, None
    best = max(candidates, key=lambda s: s.max_grade_pct)
    return best.max_grade_pct, best.street


def data_gaps(a: RouteAnalysis) -> list[dict]:
    """Parts of the route with no elevation data: start/end distance and the streets involved."""
    gaps, start = [], None
    elev = a.elevations + [0.0]  # sentinel closes a gap that runs to the end
    for i, e in enumerate(elev):
        if e is None and start is None:
            start = i
        elif e is not None and start is not None:
            streets = list(dict.fromkeys(a.streets[start:i]))
            gaps.append({"start_m": round(a.dist_m[start]), "end_m": round(a.dist_m[i - 1]), "streets": streets})
            start = None
    return gaps


# A climb this small can be walked, so it doesn't fail a level. Downhills are never excused:
# braking is the danger on skates.
WALKABLE_CLIMB_M = 3.0
WALKABLE_CLIMB_LENGTH_M = 75


def is_walkable_climb(s: Stretch) -> bool:
    return s.direction == "up" and s.elevation_change_m <= WALKABLE_CLIMB_M and s.length_m <= WALKABLE_CLIMB_LENGTH_M


def suitability(a: RouteAnalysis) -> dict[str, dict]:
    """For each rider level: {"verdict": "yes" | "no" | "unknown", "short_climbs_to_walk": n}.

    "no": a downhill over the level's limit, or a climb too long or high to walk.
    "unknown": nothing over the limits, but parts of the route have no elevation data.
    Likely artifacts (bridges, overpasses) don't count against a level.
    """
    gaps = bool(data_gaps(a))
    out = {}
    for level, limits in RIDER_LEVELS.items():
        over = [s for s in find_stretches(a, limits["max_uphill_pct"], limits["max_downhill_pct"]) if not s.possible_artifact]
        walkable = [s for s in over if is_walkable_climb(s)]
        verdict = "no" if len(walkable) < len(over) else "unknown" if gaps else "yes"
        out[level] = {"verdict": verdict, "short_climbs_to_walk": len(walkable)}
    return out
