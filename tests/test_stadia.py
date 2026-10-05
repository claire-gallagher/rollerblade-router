"""Offline tests for stadia.py: parsing a canned Valhalla response (no API key or network needed)."""

import polyline
import pytest

import stadia

A = {"name": "start", "lat": 40.7681, "lng": -73.9819}
B = {"name": "end", "lat": 40.7700, "lng": -73.9840}
SHAPE = [(40.7681, -73.9819), (40.7685, -73.9825), (40.7690, -73.9832), (40.7700, -73.9840)]


class FakeResponse:
    def __init__(self, data, ok=True, status=200):
        self._data, self.ok, self.status_code = data, ok, status

    def json(self):
        return self._data


def valhalla_trip(street_a="West 55th Street", street_b=None):
    return {
        "summary": {"length": 0.35, "time": 97.0},
        "legs": [{
            "shape": polyline.encode(SHAPE, 6),
            "maneuvers": [
                {"instruction": "Bike northwest on West 55th Street.", "street_names": [street_a],
                 "length": 0.2, "begin_shape_index": 0, "end_shape_index": 2},
                {"instruction": "Turn right onto the path.", "street_names": [street_b] if street_b else [],
                 "length": 0.15, "begin_shape_index": 2, "end_shape_index": 3},
                {"instruction": "You have arrived.", "length": 0.0, "begin_shape_index": 3, "end_shape_index": 3},
            ],
        }],
    }


@pytest.fixture
def post(monkeypatch):
    """Capture the routing request and return a canned response."""
    calls = {}

    def fake_post(url, json, params, timeout):
        calls["body"] = json
        return calls.get("response", FakeResponse({"trip": valhalla_trip(), "alternates": [{"trip": valhalla_trip("Broadway")}]}))

    monkeypatch.setattr(stadia.requests, "post", fake_post)
    monkeypatch.setenv("STADIA_API_KEY", "test")
    return calls


def test_parses_trip_and_alternates(post):
    routes = stadia.compute_bike_routes([A, B])
    assert len(routes) == 2
    r = routes[0]
    assert r["distance_m"] == 350 and r["duration_s"] == 97
    assert [s["street"] for s in r["steps"]] == ["West 55th Street", None]  # arrival maneuver dropped
    assert r["steps"][0]["points"] == pytest.approx(SHAPE[:3])
    assert r["steps"][1]["points"] == pytest.approx(SHAPE[2:])
    assert r["description"] == "via West 55th Street"
    assert routes[1]["description"] == "via Broadway"


def test_request_uses_skate_settings(post):
    stadia.compute_bike_routes([A, {"name": "mid", "lat": 40.769, "lng": -73.983}, B], use_hills=0.0)
    body = post["body"]
    assert [loc["type"] for loc in body["locations"]] == ["break", "via", "break"]
    bike = body["costing_options"]["bicycle"]
    assert bike["use_hills"] == 0.0 and bike["cycling_speed"] == stadia.SKATE_SPEED_KMH


def test_no_path_returns_empty(post):
    post["response"] = FakeResponse({"error_code": 442, "error": "No path could be found for input"}, ok=False, status=400)
    assert stadia.compute_bike_routes([A, B]) == []


def test_far_from_path_is_actionable(post):
    post["response"] = FakeResponse({"error_code": 171, "error": "No suitable edges near location"}, ok=False, status=400)
    with pytest.raises(stadia.MapsError, match="isn't near a bikeable path"):
        stadia.compute_bike_routes([A, B])


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("STADIA_API_KEY", raising=False)
    with pytest.raises(stadia.MapsError, match="STADIA_API_KEY"):
        stadia.search_places("Pier 25")
