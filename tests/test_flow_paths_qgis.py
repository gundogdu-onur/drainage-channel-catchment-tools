"""Integration tests for Longest Flow Path by Catchment. Require QGIS with GDAL."""

import importlib.util
import unittest

if importlib.util.find_spec("qgis") is None or importlib.util.find_spec("osgeo") is None:
    raise unittest.SkipTest("A QGIS Python environment with GDAL is required.")

import gc
import math
from unittest.mock import patch

import numpy as np
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProcessingException,
    QgsProcessingFeatureSourceDefinition,
    QgsProcessingOutputLayerDefinition,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from drainage_channel_catchment_tools.common.gdal_io import read_dem
from drainage_channel_catchment_tools.flow_paths.styling import ResultStyle

from .utilities import QgisTestCase


class IntegrationTests(QgisTestCase):
    @staticmethod
    def rect(a, b, c, d):
        return (
            "POLYGON(("
            + ",".join(
                f"{500000 + x} {4300000 + y}" for x, y in [(a, b), (c, b), (c, d), (a, d), (a, b)]
            )
            + "))"
        )

    def catchments(self, wkts=None, crs="EPSG:32635", cell=1, fill=0, id_field="channel_id"):
        layer = QgsVectorLayer(
            f"MultiPolygon?crs={crs}&field={id_field}:string&field=src_fid:integer&field=cell_m:double&field=fill_pits:integer",
            "catchments",
            "memory",
        )
        if wkts is None:
            wkts = [self.rect(0, 0, 3, 7), self.rect(3, 0, 6, 7)]
        for i, wkt in enumerate(wkts):
            f = QgsFeature(layer.fields())
            f.setAttributes([f"K{i + 1}", 100 + i, cell, fill])
            if wkt is not None:
                geom = QgsGeometry.fromWkt(wkt)
                geom.convertToMultiType()
                f.setGeometry(geom)
            self.assertTrue(layer.dataProvider().addFeatures([f])[0])
        QgsProject.instance().addMapLayer(layer)
        return layer

    def run_algorithm(self, dem, catchments, prefix="out", **overrides):
        import processing

        params = {
            "DEM": dem,
            "BAND": 1,
            "CATCHMENTS": catchments,
            "ID_FIELD": "",
            "CELL_SIZE": 0,
            "FILL_MODE": 0,
            "RESAMPLING": 0,
            "ALLOW_LARGE": False,
            "OUTPUT": str(self.root / f"{prefix}.gpkg"),
        }
        params.update(overrides)
        result = processing.run(
            "drainagecatchment:longestflowpath",
            params,
            context=self.context,
            feedback=self.feedback,
        )
        self.assertEqual(set(result), {"OUTPUT"})
        layer = QgsVectorLayer(result["OUTPUT"], "flow_paths", "ogr")
        self.assertTrue(layer.isValid())
        return layer, list(layer.getFeatures()), result

    def test_two_catchments_attributes_order_z_and_containment(self):
        catchments = self.catchments()
        layer, features, _ = self.run_algorithm(self.dem(), catchments)
        self.assertEqual(QgsWkbTypes.flatType(layer.wkbType()), QgsWkbTypes.LineString)
        self.assertTrue(QgsWkbTypes.hasZ(layer.wkbType()))
        self.assertEqual(len(features), 2)
        for i, (poly, line) in enumerate(zip(catchments.getFeatures(), features)):
            self.assertEqual(line["catch_id"], f"K{i + 1}")
            self.assertEqual(line["channel_id"], f"K{i + 1}")
            self.assertEqual(line["poly_fid"], poly.id())
            self.assertEqual(line["src_fid"], 100 + i)
            self.assertEqual(line["len_m"], 2.0)
            self.assertEqual(line["drop_m"], 2.0)
            self.assertEqual(line["slope_pct"], 100.0)
            self.assertEqual(line["z_start"], 9.0 - 3 * i)
            self.assertEqual(line["z_end"], 7.0 - 3 * i)
            self.assertEqual((line["status"], line["end_type"]), ("ok", "catchment_edge"))
            self.assertEqual(line.geometry().difference(poly.geometry()).length(), 0.0)
            points = list(line.geometry().vertices())
            self.assertEqual(points[0].z(), line["z_start"])
            self.assertEqual(points[-1].z(), line["z_end"])
            self.assertAlmostEqual(line.geometry().length(), line["len_m"])
        ResultStyle().postProcessLayer(layer, self.context, self.feedback)
        self.assertFalse(layer.labelsEnabled())
        self.assertIsNone(layer.labeling())

    def test_diagonal_flow_length(self):
        y, x = np.indices((7, 9))
        z = 100.0 - x - y
        catchments = self.catchments([self.rect(0, 0, 5, 5)])
        _, features, _ = self.run_algorithm(self.dem(z), catchments)
        self.assertAlmostEqual(features[0]["len_m"], 4 * math.sqrt(2))
        self.assertEqual(features[0]["nodes"], 5)

    def test_selected_only_and_duplicate_ids(self):
        catchments = self.catchments()
        features = list(catchments.getFeatures())
        catchments.dataProvider().changeAttributeValues({features[1].id(): {0: "K1"}})
        _, both, _ = self.run_algorithm(self.dem(), catchments)
        self.assertEqual([f["catch_id"] for f in both], ["K1", "K1"])
        self.assertNotEqual(both[0]["poly_fid"], both[1]["poly_fid"])
        catchments.selectByIds([features[1].id()])
        _, selected, _ = self.run_algorithm(
            self.dem(),
            QgsProcessingFeatureSourceDefinition(catchments.id(), True),
            prefix="selected",
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["poly_fid"], features[1].id())

    def test_cell_size_metadata_override_and_native_dem(self):
        catchments = self.catchments([self.rect(0, 0, 3, 7)], cell=0.5)
        dem = self.dem()
        _, automatic, _ = self.run_algorithm(dem, catchments)
        _, override, _ = self.run_algorithm(dem, catchments, prefix="override", CELL_SIZE=1)
        self.assertEqual(automatic[0]["cell_m"], 0.5)
        self.assertEqual(automatic[0]["len_m"], 2.5)
        self.assertEqual(override[0]["cell_m"], 1.0)
        self.assertEqual(override[0]["len_m"], 2.0)
        native = self.catchments([self.rect(0, 0, 1, 1)], cell=None)
        _, quarter, _ = self.run_algorithm(self.dem(h=0.25), native, prefix="native")
        self.assertEqual(quarter[0]["cell_m"], 0.25)
        self.assertEqual(quarter[0]["len_m"], 0.75)

    def test_narrow_gap_not_bridged_between_inside_centers(self):
        left = QgsGeometry.fromWkt(self.rect(0, 0, 2.9, 5))
        right = QgsGeometry.fromWkt(self.rect(3.1, 0, 6, 5))
        shape = QgsGeometry.collectGeometry([left, right])
        shape.convertToMultiType()
        catchments = self.catchments([shape.asWkt()])
        _, features, _ = self.run_algorithm(self.dem(), catchments)
        self.assertEqual(features[0]["len_m"], 2.0)
        self.assertEqual(features[0]["end_type"], "polygon_gap")
        self.assertEqual(features[0].geometry().difference(shape).length(), 0.0)

    def test_polygon_hole_is_not_crossed(self):
        shape = QgsGeometry.fromWkt(self.rect(0, 0, 8, 7)).difference(
            QgsGeometry.fromWkt(self.rect(3, 2, 5, 5))
        )
        catchments = self.catchments([shape.asWkt()])
        _, features, _ = self.run_algorithm(self.dem(), catchments)
        self.assertEqual(features[0]["len_m"], 7.0)
        self.assertEqual(features[0].geometry().difference(shape).length(), 0.0)

    def test_multipart_selects_longest_one_component(self):
        shape = QgsGeometry.collectGeometry(
            [QgsGeometry.fromWkt(self.rect(0, 0, 3, 2)), QgsGeometry.fromWkt(self.rect(3, 4, 9, 7))]
        )
        shape.convertToMultiType()
        _, features, _ = self.run_algorithm(self.dem(), self.catchments([shape.asWkt()]))
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["len_m"], 5.0)
        self.assertTrue(all(p.y() > 4300004 for p in features[0].geometry().vertices()))

    def test_geographic_catchment_transformation(self):
        catchments = self.catchments([self.rect(0.2, 0.2, 3.2, 5.2)])
        geometry = next(catchments.getFeatures()).geometry()
        transform = QgsCoordinateTransform(
            catchments.crs(),
            QgsCoordinateReferenceSystem("EPSG:4326"),
            self.context.transformContext(),
        )
        geometry.transform(transform)
        geographic = self.catchments([geometry.asWkt(15)], crs="EPSG:4326")
        _, features, _ = self.run_algorithm(self.dem(), geographic)
        self.assertEqual(features[0]["len_m"], 2.0)

    def test_empty_outside_tiny_flat_and_nodata_results_visible(self):
        catchments = self.catchments(
            [None, self.rect(20, 20, 21, 21), self.rect(0.1, 0.1, 0.2, 0.2), self.rect(0, 0, 3, 3)]
        )
        _, features, _ = self.run_algorithm(self.dem(np.ones((7, 9))), catchments)
        self.assertEqual(
            [f["status"] for f in features],
            ["empty_catchment", "outside_dem", "no_cells", "no_flow"],
        )
        self.assertTrue(all(f.geometry().isEmpty() for f in features))
        nodata = self.catchments([self.rect(0, 0, 3, 3)])
        _, features, _ = self.run_algorithm(
            self.dem(np.full((7, 9), -9999.0), nodata=-9999), nodata, prefix="nodata"
        )
        self.assertEqual(features[0]["status"], "no_cells")

    def test_fill_uses_original_dem_z(self):
        y, x = np.indices((7, 9))
        z = 100.0 - x
        z[3, 4] = 0.0
        catchments = self.catchments([self.rect(0, 3, 9, 4)], fill=1)
        _, features, _ = self.run_algorithm(self.dem(z), catchments)
        f = features[0]
        self.assertEqual(f["fill_pits"], 1)
        vertices = list(f.geometry().vertices())
        self.assertGreater(len(vertices), 1)
        for p in vertices:
            col = int(p.x() - 500000)
            row = int(p.y() - 4300000)
            self.assertEqual(p.z(), z[row, col])
        heights = [p.z() for p in vertices]
        self.assertEqual(f["z_max"], max(heights))
        self.assertEqual(f["z_min"], min(heights))
        self.assertEqual(f["z_start"], heights[0])
        self.assertEqual(f["z_end"], heights[-1])
        self.assertGreater(f["fill_dz"], 0)
        self.assertGreater(f["rise_n"], 0)
        self.assertIn("filled_rise", f["status"])

    def test_pit_and_dem_boundary_have_explicit_status(self):
        z = np.tile(np.array([9.0, 8.0, 7.0, 6.0, 1.0, 5.0, 6.0, 7.0, 8.0]), (7, 1))
        _, features, _ = self.run_algorithm(
            self.dem(z), self.catchments([self.rect(0, 1, 9, 6)]), FILL_MODE=1
        )
        self.assertEqual(features[0]["end_type"], "pit_flat")
        self.assertIn("pit_flat", features[0]["status"])
        _, features, _ = self.run_algorithm(
            self.dem(), self.catchments([self.rect(0, 0, 9, 7)]), prefix="edge"
        )
        self.assertEqual(features[0]["end_type"], "dem_edge")

    def test_postprocessor_survives_algorithm_lifetime(self):
        output = QgsProcessingOutputLayerDefinition(
            str(self.root / "styled.gpkg"), QgsProject.instance()
        )
        layer, features, result = self.run_algorithm(self.dem(), self.catchments(), OUTPUT=output)
        gc.collect()
        processor = self.context.layerToLoadOnCompletionDetails(result["OUTPUT"]).postProcessor()
        self.assertIsNotNone(processor)
        processor.postProcessLayer(layer, self.context, self.feedback)
        self.assertFalse(layer.labelsEnabled())
        self.assertIsNone(layer.labeling())

    def test_invalid_inputs_limits_and_cancel(self):
        with self.assertRaises(QgsProcessingException):
            self.run_algorithm(self.dem(epsg=4326), self.catchments())
        with self.assertRaises(QgsProcessingException):
            self.run_algorithm(
                self.dem(),
                self.catchments(
                    [
                        "POLYGON((500000 4300000,500006 4300006,500006 4300000,500000 4300006,500000 4300000))"
                    ]
                ),
            )
        with patch(
            "drainage_channel_catchment_tools.processing_provider.longest_flow_path_algorithm.read_dem",
            wraps=read_dem,
        ) as reader:
            with self.assertRaisesRegex(QgsProcessingException, "100 million"):
                self.run_algorithm(self.dem(), self.catchments(), prefix="huge", CELL_SIZE=0.0001)
            reader.assert_not_called()
        self.feedback.cancel()
        with self.assertRaises(QgsProcessingException):
            self.run_algorithm(self.dem(), self.catchments(), prefix="cancel")
