"""Main plugin class: registers the Processing provider and the Raster menu and toolbar actions."""

from qgis.core import Qgis, QgsApplication, QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .catchments.styling import VISIBLE_FIELDS as CATCHMENT_FIELDS
from .catchments.styling import ResultStyle as CatchmentStyle
from .flow_paths.styling import VISIBLE_FIELDS as FLOW_PATH_FIELDS
from .flow_paths.styling import ResultStyle as FlowPathStyle
from .processing_provider.local_channel_catchments_algorithm import ICON as CATCHMENT_ICON
from .processing_provider.longest_flow_path_algorithm import ICON as FLOW_PATH_ICON
from .processing_provider.provider import Provider


class DrainageChannelCatchmentPlugin:
    MENU = "Drainage Channel Catchment Tools"

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []

    def initProcessing(self):
        if self.provider is None:
            provider = Provider()
            if not QgsApplication.processingRegistry().addProvider(provider):
                raise RuntimeError(
                    "Could not register Drainage Channel Catchment Tools; check for duplicate installations."
                )
            self.provider = provider

    def initGui(self):
        self.initProcessing()
        if self.iface is None or self.actions:
            return
        entries = (
            (
                "LocalChannelCatchments",
                CATCHMENT_ICON,
                "Local Channel Catchments",
                self.run_catchments,
                True,
            ),
            (
                "LongestFlowPath",
                FLOW_PATH_ICON,
                "Longest Flow Path by Catchment",
                self.run_flow_paths,
                True,
            ),
            (
                "UpdateCatchmentLayer",
                CATCHMENT_ICON,
                "Update Active Catchment Layer (Style and Table)",
                self.update_catchment_layer,
                False,
            ),
            (
                "UpdateFlowPathLayer",
                FLOW_PATH_ICON,
                "Update Active Flow Path Layer (Arrows and Table)",
                self.update_flow_path_layer,
                False,
            ),
        )
        for name, icon, label, callback, toolbar in entries:
            action = QAction(QIcon(icon), label, self.iface.mainWindow())
            action.setObjectName(f"DrainageChannelCatchmentTools{name}")
            action.triggered.connect(callback)
            self.iface.addPluginToRasterMenu(self.MENU, action)
            if toolbar:
                self.iface.addToolBarIcon(action)
            self.actions.append((action, toolbar))

    def run_catchments(self):
        import processing

        processing.execAlgorithmDialog("drainagecatchment:delineate")

    def run_flow_paths(self):
        import processing

        processing.execAlgorithmDialog("drainagecatchment:longestflowpath")

    def _update_layer(self, geometry_type, required_fields, style_class, warning, success):
        layer = self.iface.activeLayer()
        if (
            not isinstance(layer, QgsVectorLayer)
            or not layer.isValid()
            or layer.geometryType() != geometry_type
            or not set(required_fields).issubset(set(layer.fields().names()))
        ):
            self.iface.messageBar().pushWarning(self.MENU, warning)
            return
        style_class().postProcessLayer(layer, None, None)
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        QgsProject.instance().setDirty(True)
        self.iface.messageBar().pushSuccess(self.MENU, success)

    def update_catchment_layer(self):
        self._update_layer(
            Qgis.GeometryType.Polygon,
            CATCHMENT_FIELDS,
            CatchmentStyle,
            "Select a Local Channel Catchments result layer in the Layers panel.",
            "Channel ID, source channel FID and Area (m²) are displayed. Save your QGIS project.",
        )

    def update_flow_path_layer(self):
        self._update_layer(
            Qgis.GeometryType.Line,
            FLOW_PATH_FIELDS,
            FlowPathStyle,
            "Select a Longest Flow Path by Catchment result layer in the Layers panel.",
            "Downstream arrows and the seven-column table are applied. Save your QGIS project.",
        )

    def unload(self):
        for action, toolbar in reversed(self.actions):
            if self.iface is not None:
                self.iface.removePluginRasterMenu(self.MENU, action)
                if toolbar:
                    self.iface.removeToolBarIcon(action)
            action.deleteLater()
        self.actions.clear()
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
