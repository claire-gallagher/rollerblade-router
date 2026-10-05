"""Offline tests for places.py: query cleanup and how Geoclient and Pelias results are combined."""

import pytest

import geoclient
import places
import stadia


@pytest.mark.parametrize("raw, cleaned", [
    ("Bethesda Fountain, Central Park, New York, NY", "Bethesda Fountain, Central Park"),
    ("Pier 25, New York, NY 10013", "Pier 25 10013"),
    ("2 Broadway, Manhattan", "2 Broadway"),
    ("Little Island", "Little Island"),
    ("Riverside Dr & W 96 St, NYC", "Riverside Dr & W 96 St"),
])
def test_clean_query(raw, cleaned):
    assert places.clean_query(raw) == cleaned


@pytest.fixture
def sources(monkeypatch):
    found = {"geoclient": [], "pelias": []}
    monkeypatch.setattr(geoclient, "search", lambda q: found["geoclient"])
    monkeypatch.setattr(stadia, "search_places", lambda q, limit: found["pelias"])
    places._first_hit.cache_clear()
    return found


PELIAS = {"label": "The Battery, New York, NY, USA", "name": "The Battery", "lat": 40.7033, "lng": -74.017, "layer": "venue"}
GEOCLIENT = {"name": "Riverside Drive & West 96 Street, Manhattan 10025", "lat": 40.796, "lng": -73.975,
             "type": "intersection", "source": "NYC Geoclient"}


def test_geoclient_matches_come_first(sources):
    sources["geoclient"], sources["pelias"] = [GEOCLIENT], [PELIAS]
    found = places.find_places("Riverside Dr & W 96 St")
    assert [c["source"] for c in found] == ["NYC Geoclient", "OpenStreetMap (Pelias)"]
    assert found[1]["type"] == "venue"


def test_geoclient_failure_falls_back_to_pelias(sources, monkeypatch):
    def broken(q):
        raise geoclient.GeoclientError("HTTP 503")
    monkeypatch.setattr(geoclient, "search", broken)
    sources["pelias"] = [PELIAS]
    assert places.resolve("The Battery")["name"] == "The Battery, New York, NY, USA"


def test_resolve_latlng_without_lookup(sources):
    assert places.resolve("40.7681, -73.9819") == {"name": "40.7681, -73.9819", "lat": 40.7681, "lng": -73.9819}


def test_resolve_not_found_is_actionable(sources):
    with pytest.raises(stadia.MapsError, match="find_place"):
        places.resolve("Nowhere Plaza")


def test_results_outside_service_area_are_dropped(sources):
    hoboken_ny_typo = {"label": "Somewhere, NY", "name": "x", "lat": 40.95, "lng": -73.97, "layer": "venue"}
    sources["pelias"] = [hoboken_ny_typo, PELIAS]
    assert [c["name"] for c in places.find_places("x")] == ["The Battery, New York, NY, USA"]


def test_latlng_outside_service_area_is_rejected(sources):
    with pytest.raises(stadia.MapsError, match="outside the area"):
        places.resolve("40.87, -74.03")  # Bogota, NJ


@pytest.fixture
def gaz(monkeypatch, tmp_path):
    path = tmp_path / "gazetteer.json"
    path.write_text('{"places": [{"name": "Central Park East Drive at E 90 St", "aliases": ["East Drive at E 90 St"], '
                    '"lat": 40.7842, "lng": -73.9589, "note": "northbound only"}, '
                    '{"name": "Central Park 102nd Street Crossing", "aliases": ["102nd Street Cross Drive"], '
                    '"lat": 40.7941, "lng": -73.9564, "note": "cross drive"}]}')
    monkeypatch.setattr(places, "GAZETTEER_PATH", str(path))
    monkeypatch.setattr(places, "_gazetteer", None)


@pytest.mark.parametrize("query, expected", [
    ("102nd Street Cross Drive", "Central Park 102nd Street Crossing"),
    ("East Drive & 90th St", "Central Park East Drive at E 90 St"),
    ("east dr at e 90 st", "Central Park East Drive at E 90 St"),
])
def test_gazetteer_matches_variants(gaz, sources, query, expected):
    assert places.find_places(query)[0]["name"] == expected


def test_gazetteer_needs_every_word(gaz, sources):
    assert places.gazetteer_matches("East Drive at E 72 St") == []


def test_avenue_named_central_park_west_is_not_a_drive(gaz, sources):
    assert places.gazetteer_matches("Central Park West & W 90 St") == []


def test_resolve_stops_at_first_source_and_caches(sources, monkeypatch):
    calls = []
    monkeypatch.setattr(geoclient, "search", lambda q: calls.append("geoclient") or [GEOCLIENT])
    monkeypatch.setattr(stadia, "search_places", lambda q, limit: calls.append("pelias") or [PELIAS])
    places.resolve("Riverside Dr & W 96 St")
    places.resolve("Riverside Dr & W 96 St")
    assert calls == ["geoclient"]  # Pelias never asked; second lookup came from the cache
