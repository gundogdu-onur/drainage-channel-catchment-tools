"""Processing algorithm: longest D8 flow path in each catchment polygon."""

import contextlib
import math
import os
import time
from typing import Any, Optional

import numpy as np
from qgis.core import (
    Qgis,
    QgsAbstractGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterBand,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProviderRegistry,
    QgsRectangle,
    QgsVariantUtils,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon

from ..common import terrain
from ..common.gdal_io import read_dem
from ..flow_paths.flow_core import longest_path, path_values, restrict_to_catchment
from ..flow_paths.geometry_io import catchment_cells, constrain_links, polygon_only
from ..flow_paths.styling import ResultStyle

ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "longest_flow_path.svg")
FIELD_TYPES = [
    ("catch_id", QMetaType.Type.QString),
    ("poly_fid", QMetaType.Type.LongLong),
    ("channel_id", QMetaType.Type.QString),
    ("src_fid", QMetaType.Type.LongLong),
    ("len_m", QMetaType.Type.Double),
    ("len_km", QMetaType.Type.Double),
    ("z_start", QMetaType.Type.Double),
    ("z_end", QMetaType.Type.Double),
    ("z_max", QMetaType.Type.Double),
    ("z_min", QMetaType.Type.Double),
    ("drop_m", QMetaType.Type.Double),
    ("slope_m_m", QMetaType.Type.Double),
    ("slope_pct", QMetaType.Type.Double),
    ("cell_m", QMetaType.Type.Double),
    ("fill_pits", QMetaType.Type.Int),
    ("nodes", QMetaType.Type.Int),
    ("area_m2", QMetaType.Type.Double),
    ("end_type", QMetaType.Type.QString),
    ("status", QMetaType.Type.QString),
    ("rise_n", QMetaType.Type.Int),
    ("fill_dz", QMetaType.Type.Double),
]
END_NAMES = {1: "catchment_edge", 2: "dem_edge", 3: "pit_flat", 4: "polygon_gap"}
CHANNEL_FIELD = "channel_id"
NAME_FIELDS = (CHANNEL_FIELD, "catch_id", "id", "fid")


def field_value(feature, name):
    if not name or feature.fields().indexFromName(name) < 0:
        return None
    value = feature[name]
    return None if QgsVariantUtils.isNull(value) else value


def first_field(fields, names):
    return next((name for name in names if fields.indexFromName(name) >= 0), "")


class LongestFlowPathAlgorithm(QgsProcessingAlgorithm):
    """Longest D8 flow path in each catchment polygon."""

    CATCHMENTS = "CATCHMENTS"
    ID_FIELD = "ID_FIELD"
    DEM = "DEM"
    BAND = "BAND"
    CELL_SIZE = "CELL_SIZE"
    FILL_MODE = "FILL_MODE"
    RESAMPLING = "RESAMPLING"
    ALLOW_LARGE = "ALLOW_LARGE"
    OUTPUT = "OUTPUT"

    def name(self) -> str:
        return "longestflowpath"

    def displayName(self) -> str:
        return "Longest Flow Path by Catchment"

    def icon(self) -> QIcon:
        return QIcon(ICON)

    def group(self) -> str:
        return ""

    def groupId(self) -> str:
        return ""

    def createInstance(self):
        return self.__class__()

    def shortHelpString(self) -> str:
        return (
            "<p>Traces the <b>longest continuous D8 flow path</b> inside each catchment polygon. "
            "The output is a LineStringZ ordered from upstream to downstream; length is "
            "horizontal, in meters.</p>"
            "<p><b>Catchment name:</b> the selected field; otherwise channel_id, catch_id or id; "
            "otherwise the polygon FID.</p>"
            "<p><b>Cell size 0</b> uses the catchment cell_m attribute when present, otherwise the "
            "DEM resolution. <b>Automatic</b> depression filling follows the fill_pits attribute "
            "and is otherwise off. Use the same DEM and resampling as the catchment analysis.</p>"
            "<p>Elevations come from the original, unfilled DEM. Mean slope = (start elevation − "
            "end elevation) / length. Paths stop at the first catchment exit and never cross polygon "
            "gaps or holes. Paths ending in a pit, flat area or DEM edge are flagged in the "
            "termination reason. Catchments without a path of at least two cells are written "
            "without geometry.</p>"
            "<p>The attribute table shows seven columns; diagnostic fields are kept but hidden. "
            "The length and slope are the inputs for time-of-concentration formulas such as "
            "Kirpich, used with the Rational Method; the tool does not compute travel times or "
            "discharges.</p>"
        )

    def initAlgorithm(self, config: Optional[dict[str, Any]] = None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.CATCHMENTS,
                "Catchment polygons (Local Channel Catchments output or another polygon layer)",
                [QgsProcessing.SourceType.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.ID_FIELD,
                "Catchment name field (optional; automatic identifier if empty)",
                parentLayerParameterName=self.CATCHMENTS,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(self.DEM, "Elevation raster (DEM, meters)")
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND, "Elevation band", parentLayerParameterName=self.DEM, defaultValue=1
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CELL_SIZE,
                "Cell size (m; 0 = cell_m field or DEM resolution)",
                Qgis.ProcessingNumberParameterType.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.FILL_MODE,
                "Depression filling in the temporary grid",
                ["Automatic (fill_pits field; otherwise disabled)", "Disabled", "Enabled"],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                "DEM resampling method",
                ["Bilinear", "Nearest neighbor"],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ALLOW_LARGE, "Allow more than 5 million cells per catchment", False
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                "Longest flow paths and catchment attributes",
                QgsProcessing.SourceType.TypeVectorLine,
            )
        )

    def prepareAlgorithm(
        self,
        parameters: dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> bool:
        dem = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if dem is None or not dem.isValid():
            raise QgsProcessingException("Select a valid DEM.")
        crs = dem.crs()
        if not crs.isValid() or crs.isGeographic() or crs.mapUnits() != Qgis.DistanceUnit.Meters:
            raise QgsProcessingException(
                "The DEM must use a projected CRS with meter units (for example, UTM)."
            )
        if dem.providerType() != "gdal":
            raise QgsProcessingException("Save the DEM as a local GeoTIFF or VRT and select it.")
        band = self.parameterAsInt(parameters, self.BAND, context)
        if not 1 <= band <= dem.bandCount():
            raise QgsProcessingException("The selected DEM band is invalid.")
        if dem.dataProvider().userNoDataValues(band):
            raise QgsProcessingException(
                "Apply QGIS-specific NoData ranges to the raster file before processing."
            )
        uri = QgsProviderRegistry.instance().decodeUri("gdal", dem.source())
        path = uri.get("path") or dem.source()
        if dem.source().startswith(("NETCDF:", "HDF", "GPKG:", "GTIFF_DIR:")):
            path = dem.source()
        ex = dem.extent()
        self._dem = (
            path,
            band,
            crs.toWkt(),
            (ex.xMinimum(), ex.yMinimum(), ex.xMaximum(), ex.yMaximum()),
            max(abs(dem.rasterUnitsPerPixelX()), abs(dem.rasterUnitsPerPixelY())),
        )
        return True

    def processAlgorithm(
        self,
        parameters: dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict[str, Any]:
        started = time.monotonic()
        sink = None
        written = lines_written = 0
        self._styles = []

        def stage(start, end):
            last = [None]

            def update(fraction, message):
                if feedback.isCanceled():
                    return False
                feedback.setProgress(start + (end - start) * fraction)
                if last[0] != message:
                    feedback.setProgressText(message)
                    last[0] = message
                return True

            return update

        try:
            terrain.report(stage(0, 1), 0, "Preparing the flow path analysis")
            source = self.parameterAsSource(parameters, self.CATCHMENTS, context)
            if source is None:
                raise QgsProcessingException(self.invalidSourceError(parameters, self.CATCHMENTS))
            if source.featureCount() == 0:
                raise QgsProcessingException(
                    "No catchments to process. Check the input layer and selected features."
                )
            if not source.sourceCrs().isValid():
                raise QgsProcessingException(
                    "The catchment layer has no defined coordinate reference system."
                )
            path, band, wkt, dem_bounds, native_cell = self._dem
            crs = QgsCoordinateReferenceSystem()
            crs.createFromWkt(wkt)
            transform = QgsCoordinateTransform(source.sourceCrs(), crs, context.transformContext())
            dem_box = QgsGeometry.fromRect(QgsRectangle(*dem_bounds))
            id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
            if id_field and source.fields().indexFromName(id_field) < 0:
                raise QgsProcessingException("The catchment name field was not found.")
            if not id_field:
                id_field = first_field(source.fields(), NAME_FIELDS)
            channel_field = first_field(source.fields(), (CHANNEL_FIELD,))
            fixed_cell = self.parameterAsDouble(parameters, self.CELL_SIZE, context)
            mode = self.parameterAsEnum(parameters, self.FILL_MODE, context)
            method = ["bilinear", "near"][
                self.parameterAsEnum(parameters, self.RESAMPLING, context)
            ]
            fields = QgsFields()
            for name, kind in FIELD_TYPES:
                fields.append(
                    QgsField(name, kind, len=254 if kind == QMetaType.Type.QString else 0)
                )
            sink, destination = self.parameterAsSink(
                parameters, self.OUTPUT, context, fields, Qgis.WkbType.LineStringZ, crs
            )
            if sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
            if context.willLoadLayerOnCompletion(destination):
                style = ResultStyle()
                self._styles.append(style)
                context.layerToLoadOnCompletionDetails(destination).setPostProcessor(style)
            feedback.pushInfo(
                "Longest D8 flow path per catchment. Elevations come from the original DEM; "
                "lengths are horizontal."
            )
            total = max(1, source.featureCount())
            request = QgsFeatureRequest()
            features = source.getFeatures(
                request, Qgis.ProcessingFeatureSourceFlag.SkipGeometryValidityChecks
            )
            for number, feature in enumerate(features):
                base, span = 99 * number / total, 99 / total

                def update(a, b):
                    return stage(base + span * a, base + span * b)

                terrain.report(update(0, 1), 0, f"Preparing catchment FID {feature.id()}")
                identity = field_value(feature, id_field)
                values = dict(
                    catch_id=str(feature.id() if identity is None else identity),
                    poly_fid=feature.id(),
                    channel_id=field_value(feature, channel_field),
                    src_fid=field_value(feature, "src_fid"),
                    len_m=0.0,
                    len_km=0.0,
                    nodes=0,
                    status="empty_catchment",
                )
                geometry = QgsGeometry(feature.geometry())
                result_geometry = None
                cell_value = fixed_cell if fixed_cell > 0 else field_value(feature, "cell_m")
                cell = float(native_cell if cell_value is None else cell_value)
                if not math.isfinite(cell) or cell <= 0:
                    raise QgsProcessingException(
                        f"Catchment {values['catch_id']}: invalid cell_m value or cell size."
                    )
                fill_value = field_value(feature, "fill_pits") if mode == 0 else int(mode == 2)
                if fill_value is not None and float(fill_value) not in (0.0, 1.0):
                    raise QgsProcessingException(
                        f"Catchment {values['catch_id']}: fill_pits must be 0 or 1."
                    )
                fill = bool(float(fill_value)) if fill_value is not None else False
                values.update(cell_m=cell, fill_pits=int(fill))
                if not geometry.isNull() and not geometry.isEmpty():
                    if source.sourceCrs() != crs:
                        geometry.transform(transform)
                    if QgsWkbTypes.isCurvedType(geometry.wkbType()):
                        geometry.convertToStraightSegment(
                            cell / 100,
                            QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference,
                        )
                    geometry = polygon_only(geometry)
                    if geometry.isEmpty() or not geometry.isGeosValid():
                        raise QgsProcessingException(
                            f"Catchment {values['catch_id']}: invalid polygon; repair the geometry before processing."
                        )
                    values["area_m2"] = geometry.area()
                    domain = polygon_only(geometry.intersection(dem_box))
                    if domain.isEmpty() or domain.area() <= 0:
                        values["status"] = "outside_dem"
                    else:
                        ex = domain.boundingBox()
                        # One-cell halo preserves the actual steepest exit direction.
                        grid = terrain.Grid.from_bounds(
                            (
                                ex.xMinimum() - cell,
                                ex.yMinimum() - cell,
                                ex.xMaximum() + cell,
                                ex.yMaximum() + cell,
                            ),
                            cell,
                        )
                        memory = grid.count * (64 if fill else 48) / 1024**3
                        feedback.pushInfo(
                            f"Catchment {values['catch_id']} (FID {feature.id()}): "
                            f"{grid.width:,} × {grid.height:,} cells, h={cell:g} m; "
                            f"depression filling={int(fill)}; estimated array memory {memory:.2f} GiB "
                            "(excluding QGIS, GDAL and geometry overhead)."
                        )
                        if grid.count > terrain.MAX_CELLS:
                            raise QgsProcessingException(
                                "The 100 million cell limit per catchment was exceeded. Increase the cell size."
                            )
                        if grid.count > terrain.LARGE_GRID and not self.parameterAsBool(
                            parameters, self.ALLOW_LARGE, context
                        ):
                            raise QgsProcessingException(
                                "A catchment exceeds 5 million cells. Check the memory estimate "
                                "and enable large-grid processing."
                            )
                        raw = read_dem(path, band, wkt, grid, method, update(0.02, 0.14))
                        inside, boundary = catchment_cells(domain, grid, wkt, update(0.14, 0.24))
                        finite = np.isfinite(raw)
                        if not np.any(inside & finite):
                            values["status"] = "no_cells"
                        else:
                            routed = (
                                terrain.priority_flood(raw, cell, update(0.24, 0.46))
                                if fill
                                else raw
                            )
                            full = terrain.build_downstream(routed, cell, update(0.46, 0.55))
                            downstream, ends, active = restrict_to_catchment(full, inside, finite)
                            del full, inside, finite, active
                            removed = constrain_links(
                                downstream, ends, boundary, domain, grid, update(0.55, 0.68)
                            )
                            del boundary
                            if removed:
                                feedback.pushInfo(
                                    f"{removed:,} cell links were stopped at a polygon gap or boundary."
                                )
                            indices, expected_length = longest_path(
                                downstream, cell, update(0.68, 0.95)
                            )
                            del downstream
                            if len(indices) < 2:
                                values["status"] = "no_flow"
                            else:
                                x, y, z, stats = path_values(indices, grid, raw, routed)
                                result_geometry = QgsGeometry(
                                    QgsLineString(
                                        [
                                            QgsPoint(float(a), float(b), float(c))
                                            for a, b, c in zip(x, y, z)
                                        ]
                                    )
                                )
                                if not math.isclose(
                                    stats["len_m"],
                                    expected_length,
                                    rel_tol=1e-9,
                                    abs_tol=cell * 1e-6,
                                ):
                                    raise QgsProcessingException(
                                        "Line length does not match the flow path calculation."
                                    )
                                outside = result_geometry.difference(domain)
                                if outside.isNull() and outside.lastError():
                                    raise QgsProcessingException(
                                        "Could not verify flow path containment within the polygon."
                                    )
                                if outside.length() > max(1e-8, cell * 1e-7):
                                    raise QgsProcessingException(
                                        "The flow path extends outside the catchment; the result was not written."
                                    )
                                end_type = END_NAMES[int(ends.ravel()[indices[-1]])]
                                flags = []
                                if end_type in ("pit_flat", "dem_edge"):
                                    flags.append(end_type)
                                if stats["rise_n"]:
                                    flags.append("filled_rise")
                                values.update(
                                    stats, end_type=end_type, status=";".join(flags) or "ok"
                                )
                            del raw, routed, ends, indices
                        # Only one catchment's numerical arrays are retained at a time.
                        if values["status"] == "no_cells":
                            del raw, inside, boundary, finite
                output = QgsFeature(fields)
                output.setAttributes([values.get(name) for name, kind in FIELD_TYPES])
                if result_geometry is not None:
                    output.setGeometry(result_geometry)
                if not sink.addFeature(output, QgsFeatureSink.Flag.FastInsert):
                    raise QgsProcessingException(
                        f"Could not write the flow path record: {sink.lastError()}"
                    )
                written += 1
                if result_geometry is not None:
                    lines_written += 1
                    feedback.pushInfo(
                        f"Catchment {values['catch_id']}: L={values['len_m']:.3f} m; "
                        f"start/end={values['z_start']:.3f}/{values['z_end']:.3f} m; "
                        f"status={values['status']}; termination={values['end_type']}."
                    )
                    if values["status"] != "ok":
                        feedback.pushWarning(
                            "Review the termination reason and warnings before estimating "
                            "time of concentration. Diagnostic fields remain in the layer."
                        )
                else:
                    feedback.pushWarning(
                        f"Catchment {values['catch_id']}: no line was generated; "
                        f"a record without geometry was retained. status={values['status']}."
                    )
                terrain.report(update(0, 1), 1, "Catchment flow path completed")
            if not written:
                raise QgsProcessingException("No catchments to process.")
            if not sink.flushBuffer():
                raise QgsProcessingException(f"Could not save the output: {sink.lastError()}")
            feedback.setProgress(100)
            feedback.pushInfo(
                f"Completed: {lines_written} flow paths, {written - lines_written} "
                f"records without geometry; {time.monotonic() - started:.2f} s."
            )
            return {self.OUTPUT: destination}
        except terrain.CanceledError as error:
            feedback.pushWarning(
                f"Processing was canceled; {written} records may remain as partial output."
            )
            raise QgsProcessingException(str(error))
        except Exception as error:
            if written:
                feedback.pushWarning(
                    f"{written} records were written before the error; output may be incomplete."
                )
            if isinstance(error, MemoryError):
                raise QgsProcessingException("Insufficient memory. Increase the cell size.")
            if isinstance(error, QgsProcessingException):
                raise
            raise QgsProcessingException(f"Longest Flow Path by Catchment: {error}") from error
        finally:
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink.flushBuffer()
                sink = None
