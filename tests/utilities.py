"""Shared setup for the QGIS integration tests."""

import gc
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import qgis
from osgeo import gdal, osr
from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject

from drainage_channel_catchment_tools.processing_provider.provider import Provider

_app = None


def start_qgis():
    global _app
    if QgsApplication.instance() is None:
        _app = QgsApplication([], False)
        _app.initQgis()
    # Processing is installed as a core plugin, outside the default Python path.
    plugins = str(Path(qgis.__file__).resolve().parent.parent / "plugins")
    if plugins not in sys.path:
        sys.path.append(plugins)
    from processing.core.Processing import Processing

    Processing.initialize()


class QgisTestCase(unittest.TestCase):
    """Registers the provider; each test gets a temporary folder and processing context."""

    @classmethod
    def setUpClass(cls):
        start_qgis()
        cls.provider = Provider()
        QgsApplication.processingRegistry().addProvider(cls.provider)

    @classmethod
    def tearDownClass(cls):
        QgsApplication.processingRegistry().removeProvider(cls.provider)
        cls.provider = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="drainage-catchment-test-"))
        self.context = QgsProcessingContext()
        self.context.setProject(QgsProject.instance())
        self.feedback = QgsProcessingFeedback()

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()
        self.context.temporaryLayerStore().removeAllMapLayers()
        self.context = None
        gc.collect()
        shutil.rmtree(self.root, ignore_errors=True)

    def dem(self, z=None, h=1.0, epsg=32635, nodata=None):
        """Write a GeoTIFF with its lower-left corner at (500000, 4300000); z rows run south to north."""
        if z is None:
            z = np.tile(np.arange(9, 0, -1, dtype=float), (7, 1))
        path = str(self.root / f"dem{len(list(self.root.glob('*.tif')))}.tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, z.shape[1], z.shape[0], 1, gdal.GDT_Float64)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform((500000, h, 0, 4300000 + h * z.shape[0], 0, -h))
        ds.GetRasterBand(1).WriteArray(z[::-1])
        if nodata is not None:
            ds.GetRasterBand(1).SetNoDataValue(nodata)
        ds = None
        return path
