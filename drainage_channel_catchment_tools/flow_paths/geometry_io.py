"""Catchment rasterization and exact containment of individual D8 links."""

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsWkbTypes

from ..common.gdal_io import memory_vector_source
from ..common.terrain import CanceledError, report


def polygon_only(geometry):
    if geometry.isNull() or geometry.isEmpty():
        return QgsGeometry()
    if QgsWkbTypes.geometryType(geometry.wkbType()) == Qgis.GeometryType.Polygon:
        parts = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        return QgsGeometry.fromMultiPolygonXY(parts)
    if QgsWkbTypes.flatType(geometry.wkbType()) == Qgis.WkbType.GeometryCollection:
        parts = []
        for child in geometry.asGeometryCollection():
            result = polygon_only(child)
            if not result.isEmpty():
                parts.extend(result.asMultiPolygon())
        if parts:
            return QgsGeometry.fromMultiPolygonXY(parts)
    return QgsGeometry()


def catchment_cells(geometry, grid, wkt, callback=None):
    """Center-inside domain plus an all-touched boundary candidate raster."""
    raster = vector = layer = edge_layer = feature = None
    try:
        raster = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 2, gdal.GDT_Byte)
        raster.SetGeoTransform((grid.x0, grid.cell, 0, grid.bounds[3], 0, -grid.cell))
        raster.SetProjection(wkt)
        for b in (1, 2):
            raster.GetRasterBand(b).Fill(0)
        vector = memory_vector_source()
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        area = ogr.CreateGeometryFromWkb(bytes(geometry.asWkb()))
        layer = vector.CreateLayer("catchment", srs=srs, geom_type=ogr.wkbMultiPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(area)
        if layer.CreateFeature(feature) != 0:
            raise ValueError("Could not rasterize the catchment polygon.")
        edge_layer = vector.CreateLayer("boundary", srs=srs, geom_type=ogr.wkbUnknown)
        feature = ogr.Feature(edge_layer.GetLayerDefn())
        feature.SetGeometry(area.Boundary())
        if edge_layer.CreateFeature(feature) != 0:
            raise ValueError("Could not read the catchment boundary.")
        feature = None

        def progress(fraction, message, unused):
            try:
                report(callback, fraction * 0.8, "Identifying catchment cells")
                return 1
            except CanceledError:
                return 0

        for b, source, option in [
            (1, layer, "ALL_TOUCHED=FALSE"),
            (2, edge_layer, "ALL_TOUCHED=TRUE"),
        ]:
            error = gdal.RasterizeLayer(
                raster, [b], source, burn_values=[1], options=[option], callback=progress
            )
            report(callback, 0.8, "Reading the catchment mask")
            if error != 0:
                raise ValueError("Could not rasterize the catchment.")
        inside = np.empty((grid.height, grid.width), dtype=bool)
        boundary = np.empty_like(inside)
        rows = max(1, 262144 // grid.width)
        for top in range(0, grid.height, rows):
            count = min(rows, grid.height - top)
            sl = slice(grid.height - top - count, grid.height - top)
            for b, destination in ((1, inside), (2, boundary)):
                block = raster.GetRasterBand(b).ReadAsArray(0, top, grid.width, count)
                if block is None:
                    raise ValueError("Could not read catchment cells.")
                destination[sl] = block[::-1] != 0
            report(callback, 0.8 + 0.2 * (top + count) / grid.height, "Reading the catchment mask")
        return inside, boundary
    finally:
        feature = layer = edge_layer = vector = raster = None


def constrain_links(downstream, ends, boundary, domain, grid, callback=None):
    """Remove links crossing holes/gaps even narrower than one pixel.

    Dilation protects against rasterization edge-tie conventions. Interior links
    cannot cross a boundary and need no GEOS call. Cutting a link ends its path;
    it must not choose an artificial alternative direction along the boundary.
    """
    candidates = boundary.copy()
    h, w = boundary.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            ya, yb = max(0, -dy), min(h, h - dy)
            xa, xb = max(0, -dx), min(w, w - dx)
            candidates[ya:yb, xa:xb] |= boundary[ya + dy : yb + dy, xa + dx : xb + dx]
    candidates &= downstream >= 0
    engine = QgsGeometry.createGeometryEngine(domain.constGet())
    engine.prepareGeometry()
    flat, reasons = downstream.ravel(), ends.ravel()
    cut = 0
    for start in range(0, flat.size, 262144):
        for relative in np.flatnonzero(candidates.ravel()[start : start + 262144]):
            i = start + int(relative)
            j = int(flat[i])
            points = [
                QgsPointXY(
                    grid.x0 + (k % w + 0.5) * grid.cell, grid.y0 + (k // w + 0.5) * grid.cell
                )
                for k in (i, j)
            ]
            segment = QgsGeometry.fromPolylineXY(points)
            # DE-9IM covers: no interior/boundary point of B in exterior of A.
            if not engine.contains(segment.constGet()) and not engine.relatePattern(
                segment.constGet(), "******FF*"
            ):
                flat[i] = -1
                reasons[i] = 4
                cut += 1
            if int(relative) % 4096 == 0:
                report(
                    callback,
                    (start + int(relative)) / flat.size,
                    "Checking flow path containment within the catchment",
                )
        report(
            callback,
            min(1, (start + 262144) / flat.size),
            "Checking flow path containment within the catchment",
        )
    return cut
