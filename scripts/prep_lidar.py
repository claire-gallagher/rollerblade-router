"""One-time prep: build a 1 m Manhattan elevation raster from the 2017 NYC LiDAR bare-earth DEM.

Source: NOAA's mirror of the 2017 NYC Topobathy LiDAR DEM (the same survey as NYC Open Data's 1 ft DEM),
1 ft Float32 cloud-optimized GeoTIFFs in NY State Plane Long Island (EPSG:6539, US survey feet).
https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NYC_topobathy_BE_DEM_2017_9307/index.html

Instead of downloading the ~20 GB of tiles, this streams only the Manhattan window, from the tiles'
built-in 2 ft overview, averages it to 1 m, converts feet to meters, and writes a compressed
cloud-optimized GeoTIFF. About 1 GB of transfer and a few minutes.

Run: uv run scripts/prep_lidar.py            (writes data/manhattan_dem_1m.tif)
"""

import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.windows import Window, from_bounds

# Stream only the byte ranges we need from S3.
os.environ.update(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.vrt",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MAX_RETRY="5",
    GDAL_HTTP_RETRY_DELAY="2",
)

SOURCE = (
    "/vsicurl/https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/"
    "NYC_topobathy_BE_DEM_2017_9307/NYC_topobathy_BE_DEM_2017_m9307_EPSG-6539.vrt"
)
# Manhattan plus a little margin (lng/lat). Covers Battery Park to Inwood/Marble Hill.
BBOX = (-74.025, 40.695, -73.905, 40.885)
US_FT = 1200 / 3937  # meters per US survey foot
PIXEL_M = 1.0
CHUNK = 2048  # output pixels per side per read
MAX_Z_ERROR_M = 0.005  # LERC keeps every value within 5 mm

OUT = Path(__file__).resolve().parent.parent / "data" / "manhattan_dem_1m.tif"


def main():
    OUT.parent.mkdir(exist_ok=True)
    staging = OUT.with_suffix(".staging.tif")

    with rasterio.open(SOURCE) as src:
        to_src = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        lngs, lats = (BBOX[0], BBOX[2], BBOX[0], BBOX[2]), (BBOX[1], BBOX[1], BBOX[3], BBOX[3])
        xs, ys = to_src.transform(lngs, lats)
        px = PIXEL_M / US_FT  # output pixel size in feet
        left, top = math.floor(min(xs)), math.ceil(max(ys))
        width, height = math.ceil((max(xs) - left) / px), math.ceil((top - min(ys)) / px)
        transform = from_origin(left, top, px, px)
        print(f"Output grid: {width} x {height} px at {PIXEL_M} m ({width * height / 1e6:.0f} M px)")

        profile = dict(
            driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=src.crs,
            transform=transform, nodata=np.nan, tiled=True, blockxsize=512, blockysize=512,
            compress="lerc_deflate", max_z_error=MAX_Z_ERROR_M, bigtiff="if_safer",
        )
        chunks = [(r, c) for r in range(0, height, CHUNK) for c in range(0, width, CHUNK)]
        start = time.time()
        with rasterio.open(staging, "w", **profile) as dst:
            dst.update_tags(
                SOURCE="2017 NYC Topobathy LiDAR bare-earth DEM (NOAA 9307), 2 ft overview averaged to 1 m",
                UNITS="meters, NAVD88",
            )
            for n, (row, col) in enumerate(chunks, 1):
                out = Window(col, row, min(CHUNK, width - col), min(CHUNK, height - row))
                b = rasterio.windows.bounds(out, transform)
                window = from_bounds(*b, transform=src.transform)
                feet = src.read(
                    1, window=window, out_shape=(int(out.height), int(out.width)),
                    resampling=Resampling.average, masked=True, boundless=True,
                )
                dst.write((feet.filled(np.nan) * US_FT).astype("float32"), 1, window=out)
                elapsed = time.time() - start
                print(f"  chunk {n}/{len(chunks)}  {elapsed:.0f}s elapsed, ~{elapsed / n * (len(chunks) - n):.0f}s left",
                      flush=True)

    print("Writing cloud-optimized GeoTIFF...")
    rasterio.shutil.copy(
        staging, OUT, driver="COG", compress="LERC_DEFLATE", max_z_error=MAX_Z_ERROR_M,
        blocksize=512, overview_resampling="average", bigtiff="IF_SAFER",
    )
    staging.unlink()
    print(f"Done: {OUT} ({OUT.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    sys.exit(main())
