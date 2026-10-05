"""The one place elevation data comes from. Swap the source here without touching the tools.

Source: the 2017 NYC LiDAR bare-earth DEM, prepared by scripts/prep_lidar.py as a 1 m raster of
Manhattan in NY State Plane (EPSG:6539). Points the raster doesn't cover (outside Manhattan, or no
data) get None, and the tools report them as gaps. There is deliberately no fallback to a coarser
source: a grade we can't trust is worse than no grade.
"""

import math
import threading
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

from settings import DATA_DIR

DEM_PATH = str(DATA_DIR / "manhattan_dem_1m.tif")
LIDAR_CHUNK = 200  # points per raster window read (~1 km of route at 5 m spacing)


class Dem:
    """A LiDAR raster opened once and sampled with bilinear interpolation."""

    def __init__(self, path: str):
        self.ds = rasterio.open(path)
        self.to_dem = Transformer.from_crs("EPSG:4326", self.ds.crs, always_xy=True)
        self.lock = threading.Lock()  # rasterio datasets aren't thread-safe

    def sample(self, points: list[tuple[float, float]]) -> list[float | None]:
        """Elevation in meters at each (lat, lng), or None outside the raster or over no data."""
        out = []
        for start in range(0, len(points), LIDAR_CHUNK):
            out += self._sample_chunk(points[start:start + LIDAR_CHUNK])
        return out

    def _sample_chunk(self, points):
        lats, lngs = zip(*points)
        xs, ys = self.to_dem.transform(lngs, lats)
        # Fractional pixel coordinates, measured from pixel centers.
        inv = ~self.ds.transform
        cols, rows = zip(*(inv @ (x, y) for x, y in zip(xs, ys)))
        cols, rows = np.array(cols) - 0.5, np.array(rows) - 0.5

        c0, r0 = max(0, math.floor(cols.min())), max(0, math.floor(rows.min()))
        c1, r1 = min(self.ds.width - 1, math.floor(cols.max()) + 1), min(self.ds.height - 1, math.floor(rows.max()) + 1)
        if c1 < c0 or r1 < r0:
            return [None] * len(points)  # chunk entirely outside the raster
        with self.lock:
            grid = self.ds.read(1, window=((r0, r1 + 1), (c0, c1 + 1)))

        out = []
        for c, r in zip(cols, rows):
            ci, ri = math.floor(c) - c0, math.floor(r) - r0
            if ci < 0 or ri < 0 or ci + 1 >= grid.shape[1] or ri + 1 >= grid.shape[0]:
                out.append(None)
                continue
            fc, fr = c - math.floor(c), r - math.floor(r)
            q = grid[ri:ri + 2, ci:ci + 2]
            v = (q[0, 0] * (1 - fc) * (1 - fr) + q[0, 1] * fc * (1 - fr)
                 + q[1, 0] * (1 - fc) * fr + q[1, 1] * fc * fr)
            out.append(None if np.isnan(v) else float(v))
        return out


_dem: Dem | None = None


def load_dem() -> Dem:
    """The LiDAR raster, opened on first use. Fails loudly if scripts/prep_lidar.py hasn't been run."""
    global _dem
    if _dem is None:
        if not Path(DEM_PATH).exists():
            raise RuntimeError(f"LiDAR raster not found at {DEM_PATH}. Run: uv run scripts/prep_lidar.py")
        _dem = Dem(DEM_PATH)
    return _dem


def get_elevations(points: list[tuple[float, float]]) -> list[float | None]:
    """Elevation in meters for each (lat, lng) from the LiDAR raster; None where it has no data."""
    return load_dem().sample(points)
