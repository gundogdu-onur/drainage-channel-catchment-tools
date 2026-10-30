"""Integration tests for Local Channel Catchments. Require QGIS with GDAL."""

import importlib.util
import unittest

if importlib.util.find_spec("qgis") is None or importlib.util.find_spec("osgeo") is None:
    raise unittest.SkipTest("A QGIS Python environment with GDAL is required.")

import gc
from unittest.mock import patch

import numpy as np
from osgeo import osr
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsProcessingException,
    QgsProcessingFeatureSourceDefinition,
    QgsProcessingOutputLayerDefinition,
    QgsProcessingParameterDefinition,
    QgsProject,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from drainage_channel_catchment_tools.catchments.styling import ResultStyle
from drainage_channel_catchment_tools.catchments.targets import add_channel_targets
from drainage_channel_catchment_tools.catchments.vector_io import polygonize_mask
from drainage_channel_catchment_tools.common.gdal_io import read_dem
from drainage_channel_catchment_tools.common.terrain import Grid

from .utilities import QgisTestCase


class IntegrationTests(QgisTestCase):
    def setUp(self):
        super().setUp()
        self.srs = osr.SpatialReference()
        self.srs.ImportFromEPSG(32635)

    def lines(self, cols=(2.5, 5.5), crs="EPSG:32635", wkts=None):
        layer = QgsVectorLayer(
            f"MultiLineString?crs={crs}&field=canal:string", "channels", "memory"
        )
        if wkts is None:
            wkts = [f"LINESTRING({500000 + x} 4300000.1,{500000 + x} 4300006.9)" for x in cols]
        for i, wkt in enumerate(wkts):
            feature = QgsFeature(layer.fields())
            feature.setAttributes([f"K{i + 1}"])
            geom = QgsGeometry.fromWkt(wkt)
            geom.convertToMultiType()
            feature.setGeometry(geom)
            layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        return layer

    def run_algorithm(self, dem, lines, prefix="out", **overrides):
        import processing

        params = {
            "DEM": dem,
            "BAND": 1,
            "INPUT_CHANNELS": lines,
            "ID_FIELD": "canal",
            "CELL_SIZE": 1.0,
            "FILL_PITS": False,
            "RESAMPLING": 0,
            "ALLOW_LARGE": False,
        }
        params["OUTPUT"] = str(self.root / f"{prefix}_OUTPUT.gpkg")
        params.update(overrides)
        return processing.run(
            "drainagecatchment:delineate", params, context=self.context, feedback=self.feedback
        )

    def polygons(self, wkts=(), crs="EPSG:32635"):
        layer = QgsVectorLayer(f"MultiPolygon?crs={crs}", "mask", "memory")
        for wkt in wkts:
            feature = QgsFeature(layer.fields())
            geometry = QgsGeometry.fromWkt(wkt)
            geometry.convertToMultiType()
            feature.setGeometry(geometry)
            self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        QgsProject.instance().addMapLayer(layer)
        return layer

    @staticmethod
    def rectangle(x1, y1, x2, y2):
        return (
            "POLYGON(("
            + ",".join(
                f"{500000 + x:.9f} {4300000 + y:.9f}"
                for x, y in [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
            )
            + "))"
        )

    def output_features(self, result):
        layer = QgsVectorLayer(result["OUTPUT"], "catchments", "ogr")
        self.assertTrue(layer.isValid())
        return list(layer.getFeatures())

    def test_gpkg_only_polygons_no_labels_and_no_overlap(self):
        result = self.run_algorithm(self.dem(), self.lines())
        self.assertEqual(set(result), {"OUTPUT"})
        self.assertEqual(len(list(self.root.glob("*.gpkg"))), 1)
        layer = QgsVectorLayer(result["OUTPUT"], "catchments", "ogr")
        self.assertTrue(layer.isValid())
        ResultStyle().postProcessLayer(layer, self.context, self.feedback)
        self.assertFalse(layer.labelsEnabled())
        features = list(layer.getFeatures())
        self.assertEqual([f["channel_id"] for f in features], ["K1", "K2"])
        self.assertEqual([f["area_m2"] for f in features], [21.0, 21.0])
        self.assertTrue(all(f.geometry().isGeosValid() for f in features))
        self.assertEqual(features[0].geometry().intersection(features[1].geometry()).area(), 0.0)
        self.assertEqual(QgsGeometry.unaryUnion([f.geometry() for f in features]).area(), 42.0)

    def test_processing_postprocessor_disables_labels(self):
        output = QgsProcessingOutputLayerDefinition(
            str(self.root / "styled.gpkg"), QgsProject.instance()
        )
        result = self.run_algorithm(self.dem(), self.lines(), OUTPUT=output)
        gc.collect()
        layer = QgsVectorLayer(result["OUTPUT"], "catchments", "ogr")
        settings = QgsPalLayerSettings()
        settings.fieldName = "area_m2"
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        post = self.context.layerToLoadOnCompletionDetails(result["OUTPUT"]).postProcessor()
        self.assertIsNotNone(post)
        post.postProcessLayer(layer, self.context, self.feedback)
        self.assertFalse(layer.labelsEnabled())
        self.assertIsNone(layer.labeling())

    def test_only_polygon_output_in_algorithm_definition(self):
        algorithm = QgsApplication.processingRegistry().algorithmById("drainagecatchment:delineate")
        self.assertEqual([output.name() for output in algorithm.outputDefinitions()], ["OUTPUT"])
        self.assertTrue(
            algorithm.parameterDefinition("MASK").flags()
            & QgsProcessingParameterDefinition.FlagOptional
        )

    def test_mask_omitted_empty_and_full_extent_preserve_results(self):
        dem = self.dem()
        lines = self.lines()
        baseline = self.output_features(self.run_algorithm(dem, lines, prefix="baseline"))
        full = self.polygons([self.rectangle(0, 0, 9, 7)])
        for index, mask in enumerate((None, "", full)):
            actual = self.output_features(
                self.run_algorithm(dem, lines, prefix=f"equal{index}", MASK=mask)
            )
            for old, new in zip(baseline, actual):
                self.assertEqual(old["area_m2"], new["area_m2"])
                self.assertEqual(old["cell_n"], new["cell_n"])
                self.assertTrue(old.geometry().isGeosEqual(new.geometry()))
                self.assertEqual(new["grid_m2"], new["area_m2"])
                self.assertEqual(new["mask_on"], int(index == 2))

    def test_mask_rectangle_crops_grid_and_excludes_upstream_area(self):
        mask = self.polygons([self.rectangle(1, 1, 6, 5)])
        with patch(
            "drainage_channel_catchment_tools.processing_provider.local_channel_catchments_algorithm.read_dem",
            wraps=read_dem,
        ) as reader:
            features = self.output_features(self.run_algorithm(self.dem(), self.lines(), MASK=mask))
        grid = reader.call_args.args[3]
        self.assertEqual((grid.width, grid.height, grid.count), (5, 4, 20))
        self.assertEqual([f["area_m2"] for f in features], [8.0, 12.0])
        self.assertEqual(features[0].geometry().intersection(features[1].geometry()).area(), 0.0)
        self.assertTrue(all(f["mask_on"] == 1 and f["edge_touch"] == 1 for f in features))

    def test_mask_boundary_clipping_recalculates_area(self):
        mask = self.polygons([self.rectangle(1.2, 1.2, 6.2, 5.2)])
        features = self.output_features(self.run_algorithm(self.dem(), self.lines(), MASK=mask))
        domain = next(mask.getFeatures()).geometry()
        self.assertEqual([f["grid_m2"] for f in features], [8.0, 12.0])
        for feature, expected in zip(features, [6.84, 11.4]):
            self.assertAlmostEqual(feature["area_m2"], expected, places=7)
            self.assertAlmostEqual(feature["area_ha"], expected / 10000, places=10)
            self.assertAlmostEqual(feature.geometry().area(), feature["area_m2"], places=8)
            self.assertAlmostEqual(feature.geometry().difference(domain).area(), 0.0, places=9)
        self.assertEqual(features[0].geometry().intersection(features[1].geometry()).area(), 0.0)

    def test_mask_triangle_uses_shape_not_only_bounding_box(self):
        geometry = QgsGeometry.fromWkt(
            "POLYGON((500000 4300000,500009 4300000,500009 4300007,500000 4300000))"
        )
        mask = self.polygons([geometry.asWkt()])
        features = self.output_features(self.run_algorithm(self.dem(), self.lines(), MASK=mask))
        self.assertEqual(len(features), 2)
        for feature in features:
            self.assertTrue(feature.geometry().isGeosValid())
            self.assertEqual(feature.geometry().difference(geometry).area(), 0.0)
            self.assertLess(feature["area_m2"], 21.0)
        self.assertEqual(features[0].geometry().intersection(features[1].geometry()).area(), 0.0)

    def test_mask_holes_remain_excluded_with_and_without_filling(self):
        geometry = QgsGeometry.fromWkt(self.rectangle(0, 0, 9, 7)).difference(
            QgsGeometry.fromWkt(self.rectangle(1, 2, 2, 4))
        )
        mask = self.polygons([geometry.asWkt()])
        hole = QgsGeometry.fromWkt(self.rectangle(1, 2, 2, 4))
        dem = self.dem()
        lines = self.lines()
        for fill in (False, True):
            features = self.output_features(
                self.run_algorithm(dem, lines, prefix=f"holes{fill}", MASK=mask, FILL_PITS=fill)
            )
            self.assertEqual([f["area_m2"] for f in features], [19.0, 21.0])
            self.assertEqual(features[0]["holes"], 1)
            self.assertTrue(all(f.geometry().intersection(hole).area() == 0 for f in features))

    def test_mask_union_multipart_and_selected_polygons(self):
        mask = self.polygons(
            [self.rectangle(0, 0, 3, 3), self.rectangle(0, 4, 3, 7), self.rectangle(1, 1, 3, 2)]
        )
        dem = self.dem()
        lines = self.lines(cols=(2.5,))
        all_features = self.output_features(self.run_algorithm(dem, lines, MASK=mask))
        self.assertEqual((all_features[0]["area_m2"], all_features[0]["parts"]), (18.0, 2))
        mask.selectByIds([next(mask.getFeatures()).id()])
        selected = self.output_features(
            self.run_algorithm(
                dem,
                lines,
                prefix="selected_mask",
                MASK=QgsProcessingFeatureSourceDefinition(mask.id(), True),
            )
        )
        self.assertEqual((selected[0]["area_m2"], selected[0]["parts"]), (9.0, 1))

    def test_mask_geographic_crs_is_transformed(self):
        mask = self.polygons([self.rectangle(1.2, 1.2, 6.2, 5.2)])
        transform = QgsCoordinateTransform(
            mask.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), self.context.transformContext()
        )
        geometry = next(mask.getFeatures()).geometry()
        geometry.transform(transform)
        geographic = self.polygons([geometry.asWkt(15)], crs="EPSG:4326")
        dem = self.dem()
        lines = self.lines()
        expected = self.output_features(self.run_algorithm(dem, lines, MASK=mask))
        actual = self.output_features(
            self.run_algorithm(dem, lines, prefix="geographic_mask", MASK=geographic)
        )
        for one, two in zip(expected, actual):
            self.assertEqual(one["cell_n"], two["cell_n"])
            self.assertAlmostEqual(one["area_m2"], two["area_m2"], places=6)

    def test_mask_applied_before_cell_limit_and_allocation(self):
        # The full 20x20 m DEM at 1 mm would exceed 100 million cells.
        # Only the tiny analysis domain must be allocated/read.
        mask = self.polygons([self.rectangle(2.49, 3, 2.51, 3.02)])
        with patch(
            "drainage_channel_catchment_tools.processing_provider.local_channel_catchments_algorithm.read_dem",
            wraps=read_dem,
        ) as reader:
            features = self.output_features(
                self.run_algorithm(
                    self.dem(np.ones((20, 20))), self.lines(cols=(2.5,)), MASK=mask, CELL_SIZE=0.001
                )
            )
        self.assertLess(reader.call_args.args[3].count, 1000)
        self.assertGreater(features[0]["area_m2"], 0)

    def test_mask_excludes_outside_channel_in_boundary_cell(self):
        # x=2.2 is outside the polygon but intersects the center-inside x=2 cell.
        mask = self.polygons([self.rectangle(2.4, 1, 6, 5)])
        features = self.output_features(
            self.run_algorithm(self.dem(), self.lines(cols=(2.2, 5.5)), MASK=mask)
        )
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["channel_id"], "K2")
        self.assertAlmostEqual(features[0]["area_m2"], 14.4, places=7)
        self.assertIn("inside the mask", self.feedback.textLog())

    def test_empty_nonintersecting_and_invalid_masks_fail(self):
        dem = self.dem()
        lines = self.lines()
        empty = self.polygons()
        outside = self.polygons([self.rectangle(20, 20, 22, 22)])
        invalid = self.polygons(
            [
                "POLYGON((500000 4300000,500006 4300006,"
                "500006 4300000,500000 4300006,500000 4300000))"
            ]
        )
        no_selection = self.polygons([self.rectangle(0, 0, 9, 7)])
        no_crs = self.polygons([self.rectangle(0, 0, 9, 7)])
        no_crs.setCrs(QgsCoordinateReferenceSystem())
        for index, mask in enumerate(
            (
                empty,
                outside,
                invalid,
                no_crs,
                QgsProcessingFeatureSourceDefinition(no_selection.id(), True),
                str(self.root / "missing_mask.gpkg"),
            )
        ):
            with self.subTest(index=index), self.assertRaises(QgsProcessingException):
                self.run_algorithm(dem, lines, prefix=f"badmask{index}", MASK=mask)

    def test_mask_without_centers_or_valid_elevations_fails(self):
        tiny = self.polygons([self.rectangle(2.01, 2.01, 2.1, 2.1)])
        with self.assertRaisesRegex(QgsProcessingException, "cell centers"):
            self.run_algorithm(self.dem(), self.lines(), MASK=tiny)
        nodata = self.polygons([self.rectangle(2, 2, 3, 3)])
        z = np.ones((7, 9))
        z[2, 2] = -9999
        with self.assertRaisesRegex(QgsProcessingException, "valid DEM elevation"):
            self.run_algorithm(self.dem(z, nodata=-9999), self.lines(), MASK=nodata)

    def test_mask_cancellation_before_dem_read(self):
        mask = self.polygons([self.rectangle(1, 1, 6, 5)])
        self.feedback.cancel()
        with patch(
            "drainage_channel_catchment_tools.processing_provider.local_channel_catchments_algorithm.read_dem",
            wraps=read_dem,
        ) as reader:
            with self.assertRaises(QgsProcessingException):
                self.run_algorithm(self.dem(), self.lines(), MASK=mask)
        reader.assert_not_called()

    def test_three_nested_channels(self):
        result = self.run_algorithm(self.dem(), self.lines(cols=(2.5, 5.5, 7.5)))
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        features = list(layer.getFeatures())
        self.assertEqual([f["area_m2"] for f in features], [21.0, 21.0, 14.0])
        for i, one in enumerate(features):
            for two in features[i + 1 :]:
                self.assertEqual(one.geometry().intersection(two.geometry()).area(), 0.0)
        self.assertEqual(QgsGeometry.unaryUnion([f.geometry() for f in features]).area(), 56.0)

    def test_partial_upstream_capture(self):
        lines = self.lines(
            wkts=[
                "LINESTRING(500002.5 4300000.1,500002.5 4300003.9)",
                "LINESTRING(500005.5 4300000.1,500005.5 4300006.9)",
            ]
        )
        result = self.run_algorithm(self.dem(), lines)
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        fs = list(layer.getFeatures())
        self.assertEqual([f["area_m2"] for f in fs], [12.0, 30.0])
        self.assertEqual(fs[0].geometry().intersection(fs[1].geometry()).area(), 0.0)

    def test_unselected_upstream_not_interceptor(self):
        lines = self.lines()
        fs = list(lines.getFeatures())
        lines.selectByIds([fs[1].id()])
        result = self.run_algorithm(
            self.dem(), QgsProcessingFeatureSourceDefinition(lines.id(), True)
        )
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        self.assertEqual(layer.featureCount(), 1)
        self.assertEqual(next(layer.getFeatures())["area_m2"], 42.0)

    def test_duplicate_user_ids_do_not_merge_channels(self):
        lines = self.lines()
        for f in lines.getFeatures():
            lines.dataProvider().changeAttributeValues({f.id(): {0: "same"}})
        result = self.run_algorithm(self.dem(), lines)
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        fs = list(layer.getFeatures())
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual([f["area_m2"] for f in fs], [21.0, 21.0])
        self.assertNotEqual(fs[0]["src_fid"], fs[1]["src_fid"])

    def test_duplicate_geometry_has_visible_no_seed_record(self):
        lines = self.lines(cols=(2.5, 2.5))
        result = self.run_algorithm(self.dem(), lines)
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        fs = list(layer.getFeatures())
        self.assertEqual([f["area_m2"] for f in fs], [21.0, 0.0])
        self.assertEqual(fs[1]["status"], "no_seed")
        self.assertTrue(fs[1].geometry().isNull() or fs[1].geometry().isEmpty())
        self.assertIn("no_seed", self.feedback.textLog())

    def test_shared_target_uses_nearest_then_fid_independent_of_order(self):
        grid = Grid(1, 3, 0, 0, 1)
        target = np.ones((3, 1), bool)
        for positions, expected in [((0.25, 0.75), 1), ((0.2, 0.6), 2)]:
            records = {
                i: {"fid": i * 10, "geometry": QgsGeometry.fromWkt(f"LINESTRING({x} 0,{x} 3)")}
                for i, x in enumerate(positions, 1)
            }
            for order in ((1, 2), (2, 1)):
                owners = np.zeros((3, 1), np.int32)
                for label in order:
                    add_channel_targets(owners, target, label, grid, records)
                self.assertTrue((owners == expected).all())

    def test_selected_only_and_fill(self):
        lines = self.lines()
        lines.selectByIds([next(lines.getFeatures()).id()])
        result = self.run_algorithm(
            self.dem(), QgsProcessingFeatureSourceDefinition(lines.id(), True), FILL_PITS=True
        )
        layer = QgsVectorLayer(result["OUTPUT"], "selected", "ogr")
        self.assertEqual(layer.featureCount(), 1)
        self.assertEqual(next(layer.getFeatures())["area_m2"], 21.0)

    def test_quarter_meter_dem_and_no_data(self):
        z = np.arange(80, dtype=float).reshape(8, 10)
        z[2, 3] = -9999
        path = self.dem(z, h=0.25, nodata=-9999)
        g = Grid(10, 8, 500000, 4300000, 0.25)
        actual = read_dem(path, 1, self.srs.ExportToWkt(), g, "bilinear")
        expected = z.copy()
        expected[2, 3] = np.nan
        np.testing.assert_array_equal(actual, expected)

    def test_crs_transform_from_geographic_lines(self):
        lines = self.lines(cols=(2.5,))
        transform = QgsCoordinateTransform(
            lines.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), self.context.transformContext()
        )
        feature = next(lines.getFeatures())
        geom = feature.geometry()
        geom.transform(transform)
        geographic = self.lines(crs="EPSG:4326", wkts=[geom.asWkt(15)])
        result = self.run_algorithm(self.dem(), geographic)
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        self.assertEqual(next(layer.getFeatures())["area_m2"], 21.0)

    def test_multipart_single_record(self):
        lines = self.lines(
            wkts=[
                "MULTILINESTRING((500001.2 4300001.2,500001.8 4300001.8),"
                "(500006.2 4300005.2,500006.8 4300005.8))"
            ]
        )
        result = self.run_algorithm(self.dem(np.ones((7, 9))), lines)
        layer = QgsVectorLayer(result["OUTPUT"], "out", "ogr")
        self.assertEqual(layer.featureCount(), 1)
        feature = next(layer.getFeatures())
        self.assertEqual(feature["parts"], 2)
        self.assertEqual(feature["area_m2"], 2.0)

    def test_polygon_holes_and_corner_contacts(self):
        mask = np.zeros((10, 12), bool)
        mask[1:8, 1:8] = True
        mask[3:5, 3:5] = False
        mask[1:3, 10:12] = True
        geom, outer, holes = polygonize_mask(
            mask, Grid(12, 10, 500000, 4300000, 1), self.srs.ExportToWkt()
        )
        self.assertTrue(geom.isGeosValid())
        self.assertEqual((len(outer), len(holes), geom.area()), (2, 1, float(mask.sum())))
        for m in [np.eye(5, dtype=bool), np.array([[1, 1, 1], [1, 0, 1], [1, 1, 0]], bool)]:
            geom, _, _ = polygonize_mask(
                m, Grid(m.shape[1], m.shape[0], 500000, 4300000, 0.25), self.srs.ExportToWkt()
            )
            self.assertTrue(geom.isGeosValid())
            self.assertAlmostEqual(geom.area(), m.sum() * 0.25**2)

    def test_geographic_dem_rejected(self):
        with self.assertRaises(QgsProcessingException):
            self.run_algorithm(self.dem(epsg=4326), self.lines())
        self.assertIn("meter units", self.feedback.textLog())

    def test_closed_line_rejected(self):
        lines = self.lines(wkts=["LINESTRING(500001 4300001,500003 4300003,500001 4300001)"])
        with self.assertRaisesRegex(QgsProcessingException, "No valid target"):
            self.run_algorithm(self.dem(), lines)

    def test_cell_limit_before_allocating(self):
        with self.assertRaisesRegex(QgsProcessingException, "100 million"):
            self.run_algorithm(self.dem(np.ones((20, 20))), self.lines(), CELL_SIZE=0.001)

    def test_cancellation(self):
        self.feedback.cancel()
        with self.assertRaises(QgsProcessingException):
            self.run_algorithm(self.dem(), self.lines())
