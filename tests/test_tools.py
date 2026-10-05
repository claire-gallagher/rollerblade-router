"""Offline tests for tools.py: fake routing, places, and elevation; real grading and summaries."""

import json

import pytest

import grade
import places
import stadia
import tools
from grade import haversine_m

START = (40.78, -73.97)
M_PER_DEG = haversine_m((0, 0), (1, 0))


def north(m):
    return (START[0] + m / M_PER_DEG, START[1])


def fake_route(segments, street="Test Avenue"):
    """A route due north whose elevation follows (length_m, grade_pct) segments."""
    total = sum(length for length, _ in segments)
    steps = [
        {"instruction": f"Bike north on {street}.", "street": street, "distance_m": total / 2,
         "points": [north(d) for d in range(0, int(total / 2) + 1, 20)]},
        {"instruction": "Turn right.", "street": None, "distance_m": total / 2,
         "points": [north(d) for d in range(int(total / 2), int(total) + 1, 20)]},
    ]
    return {"distance_m": total, "duration_s": total / 3.6, "description": f"via {street}", "steps": steps}


def profile(segments):
    def elev(d):
        z, at = 50.0, 0.0
        for length, pct in segments:
            z += min(max(d - at, 0), length) * pct / 100
            at += length
        return z
    return elev


@pytest.fixture
def fakes(monkeypatch):
    """Route between any two places along a profile set by the test; no bike-lane data."""
    state = {"segments": [(500, 0)]}
    monkeypatch.setattr(places, "resolve", lambda p: {"name": p.title(), "lat": START[0], "lng": START[1]})
    monkeypatch.setattr(stadia, "compute_bike_routes", lambda stops, **kw: [fake_route(state["segments"])])
    real_analyze = grade.analyze_route

    def analyze(steps, elevation_fn=None):
        elev = profile(state["segments"])
        return real_analyze(steps, lambda pts: [elev(haversine_m(START, p)) for p in pts])

    monkeypatch.setattr(grade, "analyze_route", analyze)
    monkeypatch.setattr(tools.bike_lanes, "load", lambda: None)
    return state


def test_flat_route_meets_beginner_limits(fakes):
    out = json.loads(tools.plan_skate_route("Columbus Circle", "The Battery"))
    [r] = out["routes"]
    assert r["meets_limits"] is True
    assert r["suitable_for"]["beginner"] == "yes"
    assert out["resolved_places"] == ["Columbus Circle", "The Battery"]
    assert "note" not in out
    assert r["route_id"] in tools.ROUTES


def test_steep_descent_fails_with_actionable_note(fakes):
    fakes["segments"] = [(200, 0), (150, -6), (200, 0)]
    out = json.loads(tools.plan_skate_route("A", "B"))
    r = out["routes"][0]
    assert r["meets_limits"] is False
    assert r["stretches_over_limits"]["downhill"] == 1
    assert "get_grade_details" in out["note"]

    details = json.loads(tools.get_grade_details(r["route_id"]))
    [s] = details["stretches"]
    assert s["direction"] == "downhill" and s["max_grade_pct"] == pytest.approx(6, abs=0.3)
    assert details["showing"] == "stretches over the limits"


def test_short_climb_is_walked_not_failed(fakes):
    fakes["segments"] = [(200, 0), (30, 6), (200, 0)]
    r = json.loads(tools.plan_skate_route("A", "B"))["routes"][0]
    assert r["meets_limits"] is True
    assert r["stretches_over_limits"]["short_climbs_to_walk"] == 1


def test_place_not_found_is_returned_as_error(monkeypatch):
    def missing(p):
        raise stadia.MapsError("Could not find 'Nowhere' in Manhattan. Call find_place to look it up.")
    monkeypatch.setattr(places, "resolve", missing)
    assert "find_place" in json.loads(tools.plan_skate_route("Nowhere", "The Battery"))["error"]


def test_unknown_route_id():
    assert json.loads(tools.get_grade_details("abc123"))["error"] == "route_id abc123 not found. Call plan_skate_route first."


def test_cue_sheet_names_unnamed_steps():
    steps = [{"instruction": "Turn right.", "street": None, "distance_m": 100},
             {"instruction": "Turn left onto Hudson River Greenway.", "street": "Hudson River Greenway", "distance_m": 900}]
    assert tools.cue_sheet(steps)[0]["instruction"] == "Turn right (path toward Hudson River Greenway)."


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    def loop(name, miles, beginner, streets):
        return {"id": name, "kind": "loop", "name": name, "miles": miles, "minutes": round(miles * 7),
                "bike_facilities": {"protected": 0.9, "none": 0.1}, "steepest_climb_pct": 1.0, "steepest_descent_pct": 1.0,
                "suitable_for": {lvl: beginner if lvl == "beginner" else "yes" for lvl in grade.RIDER_LEVELS},
                "short_climbs_to_walk": {lvl: 0 for lvl in grade.RIDER_LEVELS},
                "streets": streets, "stops": ["40.74,-74.0", "40.75,-73.99", "40.74,-74.0"], "center": [40.745, -73.995]}
    path = tmp_path / "loops.json"
    path.write_text(json.dumps({"loops": [
        loop("Hudson River Greenway: out and back", 5.0, "yes", ["Hudson River Greenway"]),
        loop("8 Avenue / 9 Avenue: 8th Avenue, across West 30th Street", 1.7, "yes", ["8th Avenue", "9th Avenue"]),
        loop("8 Avenue / 9 Avenue: longer", 4.6, "no", ["8th Avenue", "9th Avenue"]),
        loop("12 St / 13 St: crosstown", 2.6, "yes", ["West 12th Street", "East 13th Street"]),
    ]}))
    monkeypatch.setattr(tools, "LOOPS_PATH", path)
    monkeypatch.setattr(tools, "_loops", None)


def test_loops_filter_by_level_and_avoid_and_rank_by_laps(catalog):
    out = json.loads(tools.find_skate_loops("beginner", target_miles=5, avoid=["hudson river greenway"]))
    names = [l["name"] for l in out["loops"]]
    # 12/13 St (2 laps = 5.2 mi) beats 8/9 Ave (3 laps = 5.1 mi): fewer laps is preferred.
    assert names == ["12 St / 13 St: crosstown", "8 Avenue / 9 Avenue: 8th Avenue, across West 30th Street"]
    assert [(l["laps"], l["total_miles"]) for l in out["loops"]] == [(2, 5.2), (3, 5.1)]
    assert out["loops"][1]["plan_with"]["waypoints"] == ["40.75,-73.99"]


def test_loops_unknown_level(catalog):
    assert "Unknown level" in json.loads(tools.find_skate_loops("expert"))["error"]
