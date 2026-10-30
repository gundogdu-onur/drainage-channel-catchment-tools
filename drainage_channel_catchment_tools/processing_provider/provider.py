"""Processing provider that exposes the plugin's algorithms."""

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .local_channel_catchments_algorithm import LocalChannelCatchmentsAlgorithm
from .longest_flow_path_algorithm import LongestFlowPathAlgorithm

ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "icon.svg")


class Provider(QgsProcessingProvider):
    """The provider of the Drainage Channel Catchment Tools plugin."""

    def loadAlgorithms(self):
        self.addAlgorithm(LocalChannelCatchmentsAlgorithm())
        self.addAlgorithm(LongestFlowPathAlgorithm())

    def id(self) -> str:
        return "drainagecatchment"

    def name(self) -> str:
        return self.tr("Drainage Channel Catchment Tools")

    def icon(self) -> QIcon:
        return QIcon(ICON)
