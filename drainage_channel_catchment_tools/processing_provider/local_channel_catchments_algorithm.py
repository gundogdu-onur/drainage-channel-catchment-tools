"""Processing algorithm: non-overlapping local catchments for drainage channels."""

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
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
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
    QgsVariantUtils,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QIcon

from ..catchments import core
from ..catchments.mask_io import apply_mask, clip_catchment, clip_channel, prepare_mask
from ..catchments.styling import ResultStyle
from ..catchments.targets import add_channel_targets
from ..catchments.vector_io import polygonize_mask, touches_domain
from ..common.gdal_io import read_dem
from ..common.terrain import (
    LARGE_GRID,
    MAX_CELLS,
    CanceledError,
    Grid,
    build_downstream,
    priority_flood,
    report,
)

ICON = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "icons", "local_channel_catchments.svg"
)
FIELD_TYPES = [
    ("channel_id", QMetaType.Type.QString),
    ("src_fid", QMetaType.Type.LongLong),
    ("area_m2", QMetaType.Type.Double),
    ("area_ha", QMetaType.Type.Double),
    ("cell_m", QMetaType.Type.Double),
    ("fill_pits", QMetaType.Type.Int),
    ("target_n", QMetaType.Type.Int),
    ("seed_n", QMetaType.Type.Int),
    ("cell_n", QMetaType.Type.Int),
    ("parts", QMetaType.Type.Int),
    ("holes", QMetaType.Type.Int),
    ("edge_touch", QMetaType.Type.Int),
    ("status", QMetaType.Type.QString),
    ("grid_m2", QMetaType.Type.Double),
    ("mask_on", QMetaType.Type.Int),
]


class LocalChannelCatchmentsAlgorithm(QgsProcessingAlgorithm):
    """Non-overlapping local catchments for drainage channel lines."""

    DEM = "DEM"
    BAND = "BAND"
    INPUT_CHANNELS = "INPUT_CHANNELS"
    ID_FIELD = "ID_FIELD"
    MASK = "MASK"
    CELL_SIZE = "CELL_SIZE"
    FILL_PITS = "FILL_PITS"
    RESAMPLING = "RESAMPLING"
    ALLOW_LARGE = "ALLOW_LARGE"
    OUTPUT = "OUTPUT"

    def name(self) -> str:
        return "delineate"

    def displayName(self) -> str:
        return "Local Channel Catchments"

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
            "<p>Delineates non-overlapping local catchments for drainage channel lines. "
            "Each DEM cell is assigned to the first channel its D8 flow path reaches, so an "
            "area captured by an upstream channel is not counted again downstream.</p>"
            "<p>The catchment areas are the <i>A</i> term of the Rational Method. Run Longest "
            "Flow Path by Catchment on the result for the flow path length and slope.</p>"
            "<p>Process all channels that should intercept one another in the <b>same run</b>; "
            "outputs from separate runs can overlap.</p>"
            "<p><b>Inputs:</b> a DEM in a projected CRS with meter units and the channel lines. "
            "The optional analysis mask limits the grid to the mask polygons: flow from outside "
            "the mask is ignored and the catchments are clipped to the mask boundary.</p>"
            "<p><b>Output:</b> one polygon per channel. The attribute table shows Channel ID, "
            "the source channel feature ID (FID) and Area (m²); diagnostic fields are kept "
            "but hidden. A channel left without target cells is written without geometry "
            "and with status <i>no_seed</i>.</p>"
            "<p>A target cell touched by several lines goes to the nearest line; the smaller "
            "FID breaks ties. Depression filling works on a temporary grid and never changes "
            "the source DEM. Grids above 5 million cells must be allowed explicitly; grids "
            "above 100 million cells are rejected.</p>"
        )

    def initAlgorithm(self, config: Optional[dict[str, Any]] = None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(self.DEM, "Elevation raster (DEM, meters)")
        )
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND, "Elevation band", parentLayerParameterName=self.DEM, defaultValue=1
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_CHANNELS,
                "Drainage channel polylines to process together",
                [QgsProcessing.SourceType.TypeVectorLine],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.ID_FIELD,
                "Channel ID field (optional; source feature ID if empty)",
                parentLayerParameterName=self.INPUT_CHANNELS,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.MASK,
                "Analysis mask (polygon, optional)",
                [QgsProcessing.SourceType.TypeVectorPolygon],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CELL_SIZE,
                "Cell size (m)",
                Qgis.ProcessingNumberParameterType.Double,
                defaultValue=1.0,
                minValue=0.001,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FILL_PITS, "Fill depressions in the temporary grid (no depth limit)", False
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
                self.ALLOW_LARGE,
                "Allow more than 5 million cells (increases memory and processing time)",
                False,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                "Non-overlapping local catchment polygons",
                QgsProcessing.SourceType.TypeVectorPolygon,
            )
        )

    def prepareAlgorithm(
        self,
        parameters: dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> bool:
        # Read raster layer properties on the main thread; GDAL datasets are
        # opened later, in the processing worker.
        dem = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if dem is None or not dem.isValid():
            raise QgsProcessingException("Select a valid DEM.")
        crs = dem.crs()
        if not crs.isValid() or crs.isGeographic() or crs.mapUnits() != Qgis.DistanceUnit.Meters:
            raise QgsProcessingException(
                "The DEM must use a projected CRS with meter units (for example, UTM). "
                "Reproject geographic latitude/longitude rasters before processing."
            )
        if dem.providerType() != "gdal":
            raise QgsProcessingException(
                "Save the DEM as a local GeoTIFF or VRT and select it again."
            )
        band = self.parameterAsInt(parameters, self.BAND, context)
        if band < 1 or band > dem.bandCount():
            raise QgsProcessingException("The selected DEM band is invalid.")
        if dem.dataProvider().userNoDataValues(band):
            raise QgsProcessingException(
                "The DEM has QGIS-specific NoData ranges. Apply these values "
                "as NoData in the raster file before processing."
            )
        uri = QgsProviderRegistry.instance().decodeUri("gdal", dem.source())
        path = uri.get("path") or dem.source()
        # Preserve GDAL subdataset URIs such as NETCDF:..., GPKG:... .
        if dem.source().startswith(("NETCDF:", "HDF", "GPKG:", "GTIFF_DIR:")):
            path = dem.source()
        ex = dem.extent()
        self._dem = (
            path,
            band,
            crs.toWkt(),
            (ex.xMinimum(), ex.yMinimum(), ex.xMaximum(), ex.yMaximum()),
        )
        return True

    def processAlgorithm(
        self,
        parameters: dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict[str, Any]:
        started = time.monotonic()
        written = skipped = empty_count = 0
        sink = None
        self._styles = []

        def stage(start, end):
            last = [None]

            def update(fraction, message):
                if feedback.isCanceled():
                    return False
                feedback.setProgress(start + (end - start) * fraction)
                if message != last[0]:
                    feedback.setProgressText(message)
                    last[0] = message
                return True

            return update

        try:
            path, band, wkt, bounds = self._dem
            cell = self.parameterAsDouble(parameters, self.CELL_SIZE, context)
            fill = self.parameterAsBool(parameters, self.FILL_PITS, context)
            crs = QgsCoordinateReferenceSystem()
            if not crs.createFromWkt(wkt):
                raise QgsProcessingException("Could not read the DEM coordinate reference system.")
            domain = None
            mask_source = self.parameterAsSource(parameters, self.MASK, context)
            raw_mask = parameters.get(self.MASK)
            if mask_source is None and not QgsVariantUtils.isNull(raw_mask) and raw_mask != "":
                raise QgsProcessingException(self.invalidSourceError(parameters, self.MASK))
            if mask_source is not None:
                domain, bounds = prepare_mask(mask_source, crs, context, bounds, cell, stage(0, 3))
                feedback.pushInfo(
                    "Mask enabled: the grid extent was reduced. Areas outside the mask and "
                    "polygon holes are excluded from flow routing."
                )
            grid = Grid.from_bounds(bounds, cell)
            memory = core.estimate_memory_gib(grid, fill)
            feedback.pushInfo(
                f"Grid: {grid.width:,} × {grid.height:,} = {grid.count:,} cells; "
                f"estimated working-array memory {memory:.2f} GiB (excluding QGIS, GDAL and geometry overhead)."
            )
            if grid.count > MAX_CELLS:
                minimum = cell * math.sqrt(grid.count / MAX_CELLS)
                raise QgsProcessingException(
                    f"The 100 million cell limit was exceeded. Use a cell size of at least "
                    f"approximately {minimum:.3f} m or reduce the extent with an analysis mask."
                )
            if grid.count > LARGE_GRID and not self.parameterAsBool(
                parameters, self.ALLOW_LARGE, context
            ):
                raise QgsProcessingException(
                    "The grid exceeds 5 million cells. Review the memory estimate and "
                    "enable the option to allow more than 5 million cells."
                )
            source = self.parameterAsSource(parameters, self.INPUT_CHANNELS, context)
            if source is None:
                raise QgsProcessingException(
                    self.invalidSourceError(parameters, self.INPUT_CHANNELS)
                )
            if source.featureCount() == 0:
                raise QgsProcessingException(
                    "No channels to process. Check the input layer and selected features."
                )
            if not source.sourceCrs().isValid():
                raise QgsProcessingException(
                    "The channel layer has no defined coordinate reference system."
                )
            transform = QgsCoordinateTransform(source.sourceCrs(), crs, context.transformContext())
            id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
            if id_field and source.fields().indexFromName(id_field) < 0:
                raise QgsProcessingException("The channel ID field was not found.")
            feedback.pushInfo(
                "All channels in this run are processed together. "
                "Each cell contributes only to the first channel reached downstream."
            )
            method = ["bilinear", "near"][
                self.parameterAsEnum(parameters, self.RESAMPLING, context)
            ]
            z = read_dem(path, band, wkt, grid, method, stage(3 if domain is not None else 0, 10))
            if domain is not None:
                inside_count = apply_mask(z, domain, grid, wkt, stage(10, 12))
                feedback.pushInfo(
                    f"Cell centers within the mask: {inside_count:,}. "
                    "Areas are calculated after clipping the final polygons to the mask boundary."
                )
            valid = np.isfinite(z)
            if not np.any(valid):
                raise QgsProcessingException(
                    "No valid DEM elevation cells were found in the analysis domain."
                )
            feedback.pushInfo(
                f"Valid elevation cells: {np.count_nonzero(valid):,}; resampling: {method}."
            )
            if fill:
                feedback.pushInfo(
                    "Depression filling enabled: no depth or area limit; the source DEM is unchanged."
                )
                z = priority_flood(z, cell, stage(12, 28))
            downstream = build_downstream(z, cell, stage(28, 40))
            del z

            # Prepare every channel's target cells before assigning catchments.
            # Labels are internal integers; source FIDs remain in the output.
            records = {}
            seeds = np.zeros(valid.shape, dtype=np.int32)
            total = max(1, source.featureCount())
            conflict_count = 0
            for number, feature in enumerate(source.getFeatures()):
                base, span = 40 + 18 * number / total, 18 / total
                report(stage(base, base + span), 0, "Preparing channel target cells")
                value = feature[id_field] if id_field else feature.id()
                channel_id = str(value) if value is not None else str(feature.id())
                try:
                    geom = QgsGeometry(feature.geometry())
                    if geom.isNull() or geom.isEmpty():
                        raise ValueError("Empty geometry.")
                    if source.sourceCrs() != crs:
                        geom.transform(transform)
                    if QgsWkbTypes.isCurvedType(geom.wkbType()):
                        geom.convertToStraightSegment(
                            cell / 100.0,
                            QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference,
                        )
                    parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
                    for part in parts:
                        if len(part) < 2:
                            raise ValueError("The line has fewer than two vertices.")
                        if part[0] == part[-1]:
                            raise ValueError(
                                "Closed polyline; select an open drainage channel polyline."
                            )
                    if domain is not None:
                        geom = clip_channel(geom, domain)
                        parts = geom.asMultiPolyline()
                    sampled = []
                    for part in parts:
                        sampled.append(core.sample_polyline([(p.x(), p.y()) for p in part], cell))
                    if not sampled:
                        raise ValueError("No valid open polyline was found.")
                    target = core.rasterize_targets(grid, sampled, stage(base, base + span * 0.7))
                    target &= valid
                    target_count = int(np.count_nonzero(target))
                    if not target_count:
                        raise ValueError(
                            "The line does not intersect valid DEM cells in the analysis domain."
                        )
                except (ValueError, OverflowError) as error:
                    skipped += 1
                    feedback.pushWarning(
                        f"Channel {channel_id} (FID {feature.id()}) skipped: {error}"
                    )
                    continue
                label = len(records) + 1
                records[label] = {
                    "id": channel_id,
                    "fid": feature.id(),
                    "geometry": geom,
                    "target_n": target_count,
                }
                conflicts = add_channel_targets(
                    seeds, target, label, grid, records, stage(base + span * 0.7, base + span)
                )
                conflict_count += conflicts
                feedback.pushInfo(
                    f"Channel {channel_id} (FID {feature.id()}): {target_count:,} target cells; "
                    f"{conflicts:,} target cells shared with previously processed channels."
                )
                del target, sampled
            if not records:
                raise QgsProcessingException(
                    "No valid target cells were found for any channel. Review the preceding messages."
                )
            seed_counts = core.label_counts(seeds, len(records))
            if conflict_count:
                feedback.pushWarning(
                    f"{conflict_count:,} shared-target encounters were resolved by assigning each cell to one channel. "
                    "Priority: nearest line to the cell center; smaller source feature ID breaks exact ties. "
                    "Cell size affects separation of closely spaced or overlapping lines."
                )
            owners = core.delineate_first_hit(downstream, seeds, valid, stage(58, 78))
            del seeds, downstream
            catchment_counts = core.label_counts(owners, len(records))
            feedback.pushInfo(
                f"A total of {int(catchment_counts[1:].sum()):,} cells were assigned uniquely to channels."
            )

            fields = QgsFields()
            for name, kind in FIELD_TYPES:
                fields.append(
                    QgsField(name, kind, len=254 if kind == QMetaType.Type.QString else 0)
                )
            sink, destination = self.parameterAsSink(
                parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon, crs
            )
            if sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
            if context.willLoadLayerOnCompletion(destination):
                style = ResultStyle()
                self._styles.append(style)
                context.layerToLoadOnCompletionDetails(destination).setPostProcessor(style)
            for label, record in records.items():
                base, span = 78 + 21 * (label - 1) / len(records), 21 / len(records)
                report(
                    stage(base, base + span),
                    0,
                    f"Generating the catchment polygon for channel {record['id']}",
                )
                count = int(catchment_counts[label])
                seed_count = int(seed_counts[label])
                grid_area = area = count * cell**2
                output = QgsFeature(fields)
                if count:
                    mask = owners == label
                    polygon_end = base + span * (0.85 if domain is not None else 1)
                    catchment, outer, holes = polygonize_mask(
                        mask, grid, wkt, stage(base, polygon_end)
                    )
                    edge = int(touches_domain(mask, valid))
                    del mask
                    parts_count, holes_count, status = len(outer), len(holes), "ok"
                    if domain is not None:
                        catchment, parts_count, holes_count = clip_catchment(
                            catchment, domain, stage(polygon_end, base + span)
                        )
                        area = catchment.area()
                    output.setGeometry(catchment)
                else:
                    # Keep the source identity visible instead of silently
                    # dropping a channel that lost all contested seed cells.
                    edge = parts_count = holes_count = 0
                    status = "no_seed"
                    empty_count += 1
                    feedback.pushWarning(
                        f"Channel {record['id']} (FID {record['fid']}): at this cell size, "
                        "no exclusive target cell remains. A zero-area record without geometry "
                        "was retained with status no_seed."
                    )
                output.setAttributes(
                    [
                        record["id"],
                        record["fid"],
                        area,
                        area / 10000,
                        cell,
                        int(fill),
                        record["target_n"],
                        seed_count,
                        count,
                        parts_count,
                        holes_count,
                        edge,
                        status,
                        grid_area,
                        int(domain is not None),
                    ]
                )
                if not sink.addFeature(output, QgsFeatureSink.Flag.FastInsert):
                    raise QgsProcessingException(
                        f"Could not write the catchment record: {sink.lastError()}"
                    )
                written += 1
                feedback.pushInfo(
                    f"Channel {record['id']}: {count:,} catchment cells; "
                    f"{area:.2f} m² ({area / 10000:.4f} ha); status={status}."
                )
            if not sink.flushBuffer():
                raise QgsProcessingException(f"Could not save the output: {sink.lastError()}")
            feedback.setProgress(100)
            feedback.pushInfo(
                f"Completed: {written - empty_count} catchment polygons, {empty_count} zero-area records, "
                f"{skipped} skipped features; elapsed time {time.monotonic() - started:.2f} s."
            )
            return {self.OUTPUT: destination}
        except CanceledError as error:
            feedback.pushWarning(
                f"Processing was canceled; {written} records may remain as partial output."
            )
            raise QgsProcessingException(str(error))
        except MemoryError:
            raise QgsProcessingException(
                "Insufficient memory. Increase the cell size or reduce the DEM analysis extent."
            )
        except QgsProcessingException:
            if written:
                feedback.pushWarning(
                    f"{written} records were written before the error; output may be incomplete."
                )
            raise
        except Exception as error:
            if written:
                feedback.pushWarning(
                    f"{written} records were written before the error; output may be incomplete."
                )
            raise QgsProcessingException(f"Local Channel Catchments: {error}") from error
        finally:
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink.flushBuffer()
                sink = None
