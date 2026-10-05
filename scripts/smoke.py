"""Check that STADIA_API_KEY and NYC_GEOCLIENT_KEY work and the LiDAR raster is in place. Run: uv run scripts/smoke.py"""

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import elevation  # noqa: E402
import geoclient  # noqa: E402
import places  # noqa: E402
import stadia  # noqa: E402

load_dotenv()


def check(name, fn):
    try:
        print(f"[ok]   {name}: {fn()}")
        return True
    except stadia.MapsError as e:
        print(f"[FAIL] {name}: {e}")
        return False


def geocoding():
    ps = stadia.search_places("Central Park Boathouse")
    return f"{ps[0]['label']} ({ps[0]['lat']:.5f}, {ps[0]['lng']:.5f})" if ps else "no results"


def nyc_geoclient():
    try:
        found = geoclient.search("Riverside Dr and W 96 St")
    except geoclient.GeoclientError as e:
        raise stadia.MapsError(str(e)) from e
    return f"{found[0]['name']} ({found[0]['lat']:.5f}, {found[0]['lng']:.5f})" if found else "no match"


def routing():
    rs = stadia.compute_bike_routes([places.resolve("Columbus Circle"), places.resolve("The Battery")])
    return f"{len(rs)} route(s); first is {rs[0]['distance_m']} m {rs[0]['description']}, {len(rs[0]['steps'])} steps"


def lidar():
    try:
        meters = elevation.get_elevations([(40.7681, -73.9819), (40.7033, -74.0170)])
    except RuntimeError as e:
        raise stadia.MapsError(str(e)) from e
    return ", ".join(f"{m:.1f} m" for m in meters)


results = [check("Stadia geocoding", geocoding), check("NYC Geoclient", nyc_geoclient),
           check("Stadia routing", routing), check("LiDAR raster", lidar)]
sys.exit(0 if all(results) else 1)
