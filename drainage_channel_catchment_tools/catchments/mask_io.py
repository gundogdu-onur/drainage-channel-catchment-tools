"""Optional polygon analysis domain, on the existing world-aligned DEM grid."""

import math

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (
    Qgis,
    QgsAbstractGeometry,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsRectangle,
    QgsWkbTypes,
)

from ..common.gdal_io import memory_vector_source
from ..common.terrain import CanceledError, report


def _polygon_parts(geometry):
    """Discard zero-area line/point contacts from overlay collections."""
    if geometry.isNull() or geometry.isEmpty():
        return []
    kind = QgsWkbTypes.geometryType(geometry.wkbType())
    if kind == Qgis.GeometryType.Polygon:
        return geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    if QgsWkbTypes.flatType(geometry.wkbType()) == Qgis.WkbType.GeometryCollection:
        return [part for child in geometry.asGeometryCollection() for part in _polygon_parts(child)]
    return []


def _multipolygon(geometry, message):
    parts = [p for p in _polygon_parts(geometry) if p and len(p[0]) >= 4]
    if not parts:
        raise ValueError(message)
    result = QgsGeometry.fromMultiPolygonXY(parts)
    if not result.isGeosValid() or not math.isfinite(result.area()) or result.area() <= 0:
        raise ValueError(message)
    return result


def prepare_mask(source, crs, context, dem_bounds, cell, callback=None):
    """Union the supplied features and intersect with the DEM BEFORE allocation.

    Processing feature sources retain the user's selected-features/filter rules.
    A supplied but empty or invalid source must never fall back to the full DEM.
    """
    if not source.sourceCrs().isValid():
        raise ValueError("The mask layer has no defined coordinate reference system.")
    transform = QgsCoordinateTransform(source.sourceCrs(), crs, context.transformContext())
    polygons = []
    total = max(1, source.featureCount())
    for number, feature in enumerate(source.getFeatures()):
        report(callback, 0.6 * number / total, "Preparing the analysis mask")
        geometry = QgsGeometry(feature.geometry())
        if geometry.isNull() or geometry.isEmpty():
            raise ValueError(f"Mask FID {feature.id()}: empty geometry. Repair the mask layer.")
        if source.sourceCrs() != crs:
            geometry.transform(transform)
        if QgsWkbTypes.isCurvedType(geometry.wkbType()):
            geometry.convertToStraightSegment(
                cell / 100.0, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference
            )
        polygons.append(
            _multipolygon(
                geometry,
                f"Mask FID {feature.id()}: invalid polygon. Repair the geometry before processing.",
            )
        )
    if not polygons:
        raise ValueError("No mask polygons to process. Check the layer and selected features.")
    report(callback, 0.65, "Merging mask polygons")
    union = QgsGeometry.unaryUnion(polygons)
    union = _multipolygon(union, "Could not merge the mask polygons.")
    report(callback, 0.85, "Intersecting the analysis mask with the DEM extent")
    domain = union.intersection(QgsGeometry.fromRect(QgsRectangle(*dem_bounds)))
    domain = _multipolygon(domain, "The mask and DEM have no overlapping polygon area.")
    extent = domain.boundingBox()
    bounds = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
    report(callback, 1, "Analysis mask prepared")
    return domain, bounds


def apply_mask(z, geometry, grid, crs_wkt, callback=None):
    """Set cells outside the center-based polygon domain to NaN, in place.

    Rasterization honors holes. The temporary byte raster is released before
    hydrology arrays are allocated; blocks are flipped into our south-up grid.
    """
    raster = vector = layer = feature = band = None
    try:
        raster = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 1, gdal.GDT_Byte)
        raster.SetGeoTransform((grid.x0, grid.cell, 0, grid.bounds[3], 0, -grid.cell))
        raster.SetProjection(crs_wkt)
        band = raster.GetRasterBand(1)
        band.Fill(0)
        vector = memory_vector_source()
        srs = osr.SpatialReference()
        srs.ImportFromWkt(crs_wkt)
        layer = vector.CreateLayer("domain", srs=srs, geom_type=ogr.wkbMultiPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
        if layer.CreateFeature(feature) != 0:
            raise ValueError("Could not pass the mask geometry to the rasterizer.")
        feature = None

        def progress(fraction, message, unused):
            try:
                report(callback, 0.6 * fraction, "Applying the mask to the analysis cells")
                return 1
            except CanceledError:
                return 0

        error = gdal.RasterizeLayer(
            raster, [1], layer, burn_values=[1], options=["ALL_TOUCHED=FALSE"], callback=progress
        )
        report(callback, 0.6, "Excluding cells outside the analysis mask")
        if error != 0:
            raise ValueError("Could not rasterize the analysis mask.")
        inside_count = 0
        rows = max(1, 262144 // grid.width)
        for top in range(0, grid.height, rows):
            count = min(rows, grid.height - top)
            values = band.ReadAsArray(0, top, grid.width, count)
            if values is None:
                raise ValueError("Could not read the mask cells.")
            inside = values[::-1] != 0
            inside_count += int(np.count_nonzero(inside))
            z[grid.height - top - count : grid.height - top][~inside] = np.nan
            report(
                callback,
                0.6 + 0.4 * (top + count) / grid.height,
                "Excluding cells outside the analysis mask",
            )
        if not inside_count:
            raise ValueError(
                "The mask contains no cell centers at this cell size. "
                "Reduce the cell size or expand the mask."
            )
        return inside_count
    finally:
        feature = layer = vector = band = raster = None


def clip_catchment(catchment, domain, callback=None):
    """Exact vector clipping: no output area may protrude beyond the mask."""
    report(callback, 0, "Clipping the catchment polygon to the mask boundary")
    result = _multipolygon(
        catchment.intersection(domain),
        "Could not clip the catchment to the mask boundary; the result was not written.",
    )
    parts = result.asMultiPolygon()
    report(callback, 1, "Masked catchment polygon completed")
    return result, len(parts), sum(len(p) - 1 for p in parts)


def clip_channel(geometry, domain):
    """Exclude outside line segments even if they touch an included edge cell."""
    clipped = geometry.intersection(domain)
    if clipped.isNull() and clipped.lastError():
        raise ValueError(f"Could not clip the channel to the mask: {clipped.lastError()}")

    def lines(part):
        if part.isNull() or part.isEmpty():
            return []
        if QgsWkbTypes.geometryType(part.wkbType()) == Qgis.GeometryType.Line:
            return part.asMultiPolyline() if part.isMultipart() else [part.asPolyline()]
        if QgsWkbTypes.flatType(part.wkbType()) == Qgis.WkbType.GeometryCollection:
            return [line for child in part.asGeometryCollection() for line in lines(child)]
        return []

    parts = [p for p in lines(clipped) if len(p) >= 2 and any(point != p[0] for point in p[1:])]
    if not parts:
        raise ValueError("The channel has no segment of positive length inside the mask.")
    return QgsGeometry.fromMultiPolylineXY(parts)
