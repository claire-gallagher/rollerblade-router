"""LiDAR sampler tests on a tiny synthetic raster: a tilted plane in NY State Plane (EPSG:6539)."""

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

import elevation
from elevation import Dem

PIXEL_FT = 3.28  # ~1 m
TO_DEM = Transformer.from_crs("EPSG:4326", "EPSG:6539", always_xy=True)
CENTER = (40.78, -73.97)


@pytest.fixture
def dem(tmp_path):
    """200 x 200 px raster whose elevation rises 0.5 m per pixel eastward, with a no-data hole."""
    x, y = TO_DEM.transform(CENTER[1], CENTER[0])
    left, top = x - 100 * PIXEL_FT, y + 100 * PIXEL_FT
    data = np.tile(np.arange(200, dtype="float32") * 0.5, (200, 1))
    data[0:10, 0:10] = np.nan
    path = tmp_path / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", width=200, height=200, count=1, dtype="float32",
                       crs="EPSG:6539", transform=from_origin(left, top, PIXEL_FT, PIXEL_FT), nodata=np.nan) as d:
        d.write(data, 1)
    return Dem(str(path))


def test_bilinear_between_pixel_centers(dem):
    [z] = dem.sample([CENTER])
    # CENTER sits on the boundary between columns 99 and 100 (49.5 m and 50 m).
    assert z == pytest.approx(49.75, abs=0.01)


def test_outside_and_nodata_are_none(dem):
    x, y = TO_DEM.transform(CENTER[1], CENTER[0])
    to_ll = Transformer.from_crs("EPSG:6539", "EPSG:4326", always_xy=True)
    hole_lng, hole_lat = to_ll.transform(x - 95 * PIXEL_FT, y + 95 * PIXEL_FT)
    assert dem.sample([(40.70, -74.00), (hole_lat, hole_lng)]) == [None, None]


def test_missing_raster_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "DEM_PATH", str(tmp_path / "nope.tif"))
    monkeypatch.setattr(elevation, "_dem", None)
    with pytest.raises(RuntimeError, match="prep_lidar"):
        elevation.get_elevations([CENTER])
