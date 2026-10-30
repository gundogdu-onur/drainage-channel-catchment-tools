"""Raster-mask polygonization; real holes and one MultiPolygon per channel."""

import math

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import Qgis, QgsGeometry, QgsWkbTypes

from ..common.gdal_io import memory_vector_source
from ..common.terrain import report


def polygonize_mask(mask, grid, crs_wkt, callback=None):
    ds = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((grid.x0, grid.cell, 0, grid.bounds[3], 0, -grid.cell))
    ds.SetProjection(crs_wkt)
    band = ds.GetRasterBand(1)
    for row in range(0, grid.height, 512):
        n = min(512, grid.height - row)
        band.WriteArray(
            mask[grid.height - row - n : grid.height - row][::-1].astype(np.uint8), 0, row
        )
        report(
            callback,
            0.15 * (row + n) / grid.height,
            "Converting the catchment raster mask to polygon boundaries",
        )
    vector = memory_vector_source()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs_wkt)
    layer = vector.CreateLayer("catchment", srs=srs, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))

    def progress(fraction, message, unused):
        try:
            report(callback, 0.15 + 0.7 * fraction, "Generating closed catchment boundaries")
            return 1
        except Exception:
            return 0

    # Four-connected polygonization keeps diagonal-only components as separate
    # valid polygon parts. The hydrological D8 connectivity remains unchanged.
    err = gdal.Polygonize(band, band, layer, 0, [], callback=progress)
    if err != 0:
        report(callback, 0.85, "Boundary generation stopped")
        raise ValueError("Could not generate the catchment boundary.")
    polygons = []
    layer.ResetReading()
    for i, feature in enumerate(layer):
        geom = QgsGeometry()
        geom.fromWkb(bytes(feature.GetGeometryRef().ExportToWkb()))
        if geom.isNull() or geom.isEmpty():
            raise ValueError("Could not read the catchment geometry.")
        polygons.append(geom)
        if i % 256 == 0:
            report(callback, 0.9, "Collecting catchment polygon parts")
    if not polygons:
        raise ValueError("Could not generate the catchment boundary.")
    result = QgsGeometry.collectGeometry(polygons)
    result.convertToMultiType()
    # Raster cell contacts can form pinched rings. Repair topology only; never
    # smooth/simplify across cell corners. Enforce unchanged net cell area.
    if not result.isGeosValid():
        result = result.makeValid()
        if QgsWkbTypes.geometryType(result.wkbType()) != Qgis.GeometryType.Polygon:
            raise ValueError("Could not repair the catchment geometry topology.")
        result.convertToMultiType()
    expected = int(np.count_nonzero(mask)) * grid.cell**2
    if not math.isclose(result.area(), expected, rel_tol=1e-8, abs_tol=grid.cell**2 * 1e-5):
        raise ValueError("Polygon area does not match cell area; the result was not written.")
    outer, holes = [], []
    for polygon in result.asMultiPolygon():
        outer.append(polygon[0])
        holes.extend(polygon[1:])
    vector = layer = band = ds = None
    report(callback, 1, "Catchment boundaries completed")
    return result, outer, holes


def touches_domain(mask, valid):
    """True if the catchment touches the DEM edge or any NoData neighbor."""
    if mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any():
        return True
    h, w = mask.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if not (dx or dy):
                continue
            ya, yb = max(0, -dy), min(h, h - dy)
            xa, xb = max(0, -dx), min(w, w - dx)
            if np.any(mask[ya:yb, xa:xb] & ~valid[ya + dy : yb + dy, xa + dx : xb + dx]):
                return True
    return False
