"""Grade engine tests on synthetic routes: a straight line north with a known elevation profile."""

import pytest

from grade import ARTIFACT_PCT, analyze_route, data_gaps, find_stretches, haversine_m, resample, steepest, suitability

START = (40.80, -73.95)
M_PER_DEG_LAT = haversine_m((0, 0), (1, 0))


def north(m: float) -> tuple[float, float]:
    return (START[0] + m / M_PER_DEG_LAT, START[1])


def straight_steps(total_m: float, street: str = "Test Ave") -> list[dict]:
    """One step running due north, with a vertex every 20 m like a real polyline."""
    return [{"street": street, "points": [north(d) for d in range(0, int(total_m) + 1, 20)]}]


def profile(segments: list[tuple[float, float]]):
    """Elevation function of distance for consecutive (length_m, grade_pct) segments, starting at 50 m."""
    def elev(d: float) -> float:
        z, at = 50.0, 0.0
        for length, pct in segments:
            run = min(max(d - at, 0), length)
            z += run * pct / 100
            at += length
        return z
    return elev


def fake_source(elev, missing=lambda d: False):
    """An elevation_fn for analyze_route that reads a synthetic profile."""
    def fn(points):
        dists = [haversine_m(START, p) for p in points]
        return [None if missing(d) else elev(d) for d in dists]
    return fn


def analyze(segments, **kw):
    total = sum(length for length, _ in segments)
    return analyze_route(straight_steps(total), elevation_fn=fake_source(profile(segments), **kw))


def test_resample_spacing_and_end():
    points, dists, streets = resample(straight_steps(103), 5)
    assert dists[:3] == [0.0, 5.0, 10.0]
    assert dists[-1] == pytest.approx(100, abs=0.5)  # last polyline vertex is at 100 m
    assert all(5 - 1e-6 <= b - a <= 5 + 1e-6 for a, b in zip(dists, dists[1:]))
    assert set(streets) == {"Test Ave"}


def test_unnamed_steps_take_previous_street():
    steps = [
        {"street": "Broadway", "points": [north(0), north(50)]},
        {"street": None, "points": [north(50), north(100)]},
    ]
    _, _, streets = resample(steps, 5)
    assert set(streets) == {"Broadway"}


def test_flat_route_has_no_stretches():
    a = analyze([(300, 0)])
    assert find_stretches(a, 4, 3) == []
    assert steepest(a, "up") == (0.0, None)


def test_sustained_downhill():
    a = analyze([(100, 0), (100, -6), (100, 0)])
    [s] = find_stretches(a, max_up=4, max_down=3)
    assert s.direction == "down"
    assert s.max_grade_pct == pytest.approx(6, abs=0.2)
    assert s.sustained_grade_pct == pytest.approx(6, abs=0.2)
    assert s.elevation_change_m == pytest.approx(6, abs=0.5)
    assert 90 <= s.length_m <= 140
    assert s.street == "Test Ave" and not s.possible_artifact
    assert steepest(a, "down") == (pytest.approx(6, abs=0.2), "Test Ave")
    # The same hill is fine for a rider whose downhill limit is above 6%.
    assert find_stretches(a, max_up=4, max_down=7) == []


def test_uphill_is_signed_by_travel_direction():
    [s] = find_stretches(analyze([(100, 0), (100, 6), (100, 0)]), 4, 3)
    assert s.direction == "up"


def test_short_window_reports_brief_steep_peak():
    # 15 m at 10%: the 10 m window sees the full 10%, the 30 m window only about half.
    [s] = find_stretches(analyze([(100, 0), (15, -10), (100, 0)]), 4, 3)
    assert s.max_grade_pct > 8
    assert s.sustained_grade_pct < 6


def test_tiny_bumps_are_ignored():
    # 10 m at 6% is only 0.6 m of drop: under min_change_m, so not a stretch.
    a = analyze([(100, 0), (10, -6), (100, 0)])
    assert find_stretches(a, 4, 3) == []
    assert steepest(a, "down") == (0.0, None)


def test_implausible_grade_is_flagged_as_artifact():
    a = analyze([(100, 0), (20, -20), (100, 0)])
    [s] = find_stretches(a, 4, 3)
    assert s.possible_artifact
    assert steepest(a, "down")[0] <= ARTIFACT_PCT


def test_missing_elevation_is_reported_as_gap():
    a = analyze([(300, 0)], missing=lambda d: 99 < d < 151)
    [gap] = data_gaps(a)
    assert gap["start_m"] == 100 and gap["end_m"] == 150
    assert gap["streets"] == ["Test Ave"]
    assert a.short_pct[25] is None  # 125 m


def verdicts(a):
    return {level: v["verdict"] for level, v in suitability(a).items()}


def test_suitability_by_rider_level():
    # A sustained 5.5% downhill: too steep for beginners and intermediates (5%), fine for advanced (8%).
    a = analyze([(100, 0), (150, -5.5), (100, 0)])
    assert verdicts(a) == {"beginner": "no", "intermediate": "no", "advanced": "yes"}


def test_short_climb_is_walkable_but_short_descent_is_not():
    # 30 m at 6%: 1.8 m. Uphill, a beginner can walk it; downhill, it's a braking problem.
    up = suitability(analyze([(100, 0), (30, 6), (100, 0)]))
    assert up["beginner"] == {"verdict": "yes", "short_climbs_to_walk": 1}
    assert verdicts(analyze([(100, 0), (30, -6), (100, 0)]))["beginner"] == "no"


def test_long_climb_fails():
    assert verdicts(analyze([(100, 0), (150, 6), (100, 0)]))["beginner"] == "no"


def test_suitability_unknown_with_gaps():
    a = analyze([(300, 0)], missing=lambda d: 99 < d < 151)
    assert verdicts(a) == {"beginner": "unknown", "intermediate": "unknown", "advanced": "unknown"}


def test_artifacts_do_not_count_against_levels():
    assert verdicts(analyze([(100, 0), (20, -20), (100, 0)]))["beginner"] == "yes"


def test_short_steep_core_counts_the_whole_descent():
    # A long 2% descent with a 20 m core at 4.5%: the core alone drops < 1 m, but the hill drops ~5 m.
    a = analyze([(100, 0), (100, -2), (20, -4.5), (100, -2), (100, 0)])
    [s] = find_stretches(a, 4, 3)
    assert s.direction == "down" and s.max_grade_pct > 3
    assert s.elevation_change_m == pytest.approx(4.9, abs=0.4)
    assert verdicts(a)["beginner"] == "no"


def test_steep_bits_of_one_hill_are_one_stretch():
    # One 15 m climb with two steep pitches separated by a gentler middle: a single stretch.
    a = analyze([(100, 0), (40, 6), (40, 2.5), (40, 6), (100, 0)])
    [s] = find_stretches(a, 4, 3)
    assert s.direction == "up"
    assert s.elevation_change_m == pytest.approx(5.8, abs=0.4)
    assert s.length_m >= 110
