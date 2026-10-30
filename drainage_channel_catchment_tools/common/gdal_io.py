"""GDAL helpers shared by both tools."""

import numpy as np
from osgeo import gdal, ogr

from .terrain import report


def memory_vector_source():
    # GDAL 3.11 merged the vector 'Memory' driver into 'MEM'.
    name = "MEM" if int(gdal.VersionInfo()) >= 3110000 else "Memory"
    return ogr.GetDriverByName(name).CreateDataSource("")


def read_dem(path, band_number, crs_wkt, grid, resampling, callback=None):
    """Resample one DEM band onto the grid as a south-up array; NoData becomes NaN."""
    source = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
    selected = warped = original_band = band = mask_band = None
    try:
        if source is None:
            raise ValueError("GDAL could not open the DEM. Use a local GeoTIFF or VRT.")
        if not 1 <= band_number <= source.RasterCount:
            raise ValueError("The selected DEM band was not found.")
        original_band = source.GetRasterBand(band_number)
        scale = original_band.GetScale()
        offset = original_band.GetOffset()
        unit = (original_band.GetUnitType() or "").strip().lower()
        if unit and unit not in ("m", "meter", "meters", "metre", "metres"):
            raise ValueError(f"DEM vertical unit is '{unit}'. Convert elevations to meters.")
        selected = gdal.Translate("", source, format="VRT", bandList=[band_number])
        if selected is None:
            raise ValueError("Could not read the DEM band.")
        warped = gdal.Warp(
            "",
            selected,
            format="VRT",
            outputBounds=grid.bounds,
            width=grid.width,
            height=grid.height,
            srcSRS=crs_wkt,
            dstSRS=crs_wkt,
            resampleAlg=resampling,
            outputType=gdal.GDT_Float64,
            dstNodata=float("nan"),
            errorThreshold=0.0,
        )
        if warped is None:
            raise ValueError("Could not resample the DEM to the analysis grid.")
        z = np.full((grid.height, grid.width), np.nan, dtype=np.float64)
        band = warped.GetRasterBand(1)
        mask_band = band.GetMaskBand()
        rows = max(1, 262144 // grid.width)
        for top in range(0, grid.height, rows):
            count = min(rows, grid.height - top)
            block = band.ReadAsArray(0, top, grid.width, count)
            if block is None:
                raise ValueError("Could not read the DEM pixel block.")
            valid = np.isfinite(block)
            if mask_band is not None:
                mask = mask_band.ReadAsArray(0, top, grid.width, count)
                if mask is None:
                    raise ValueError("Could not read the DEM NoData mask.")
                valid &= mask != 0
            if scale is not None:
                block *= scale
            if offset is not None:
                block += offset
            block[~valid | ~np.isfinite(block)] = np.nan
            z[grid.height - top - count : grid.height - top] = block[::-1]
            report(callback, (top + count) / grid.height, "Sampling DEM elevations at cell centers")
        return z
    finally:
        # Release handles even when progress callbacks cancel the operation.
        mask_band = band = warped = selected = original_band = source = None
