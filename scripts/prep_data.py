"""Build everything in data/ in order. Run once: uv run scripts/prep_data.py

  1. prep_lidar.py       1 m Manhattan elevation raster (~4 min, streams ~1 GB from NOAA)
  2. prep_bike_lanes.py  NYC DOT bike facilities
  3. prep_gazetteer.py   Central Park drive waypoints (needs NYC_GEOCLIENT_KEY)
  4. prep_corridors.py   graded protected corridors (needs 1 and 2)
  5. prep_loops.py       ready-made loops and out-and-backs (needs 4 and STADIA_API_KEY; ~5 min)

Steps whose output already exists are skipped; delete a file in data/ to rebuild it.
"""

import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
STEPS = [
    ("prep_lidar.py", "manhattan_dem_1m.tif"),
    ("prep_bike_lanes.py", "bike_routes_manhattan.geojson"),
    ("prep_gazetteer.py", "gazetteer.json"),
    ("prep_corridors.py", "corridors.json"),
    ("prep_loops.py", "loops.json"),
]

for script, output in STEPS:
    if (DATA / output).exists():
        print(f"skip {script}: data/{output} exists")
        continue
    print(f"run  {script}")
    runpy.run_path(str(HERE / script), run_name="__main__")
