"""Polygon styling only. Area values stay in the attribute table."""

from qgis.core import (
    QgsFillSymbol,
    QgsProcessingLayerPostProcessorInterface,
    QgsSingleSymbolRenderer,
)
from qgis.PyQt import sip

VISIBLE_FIELDS = ("channel_id", "src_fid", "area_m2")

FIELD_ALIASES = {
    "channel_id": "Channel ID",
    "src_fid": "FID",
    "area_m2": "Area (m²)",
    "area_ha": "Catchment Area (ha)",
    "cell_m": "Analysis Cell Size (m)",
    "fill_pits": "Depression Filling Enabled",
    "target_n": "Channel Target Cell Count",
    "seed_n": "Exclusive Channel Target Cell Count",
    "cell_n": "Catchment Cell Count",
    "parts": "Polygon Part Count",
    "holes": "Polygon Interior Ring Count",
    "edge_touch": "Analysis Domain Boundary Contact",
    "status": "Processing Status",
    "grid_m2": "Catchment Grid Area (m²)",
    "mask_on": "Analysis Mask Enabled",
}


def configure_attribute_table(layer):
    """Display three ordered columns; retain provider IDs and diagnostics."""
    config = layer.attributeTableConfig()
    config.update(layer.fields())
    columns = config.columns()
    order = {name: index for index, name in enumerate(VISIBLE_FIELDS)}
    for column in columns:
        column.hidden = column.name not in order
    columns.sort(key=lambda column: order.get(column.name, len(order)))
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)
    for name, alias in FIELD_ALIASES.items():
        index = layer.fields().indexFromName(name)
        if index >= 0:
            layer.setFieldAlias(index, alias)


class ResultStyle(QgsProcessingLayerPostProcessorInterface):
    # Processing holds only a C++ pointer; keep the Python object alive until used.
    _live = []

    def __init__(self):
        super().__init__()
        ResultStyle._live[:] = [item for item in ResultStyle._live if not sip.isdeleted(item)]
        ResultStyle._live.append(self)

    def postProcessLayer(self, layer, context, feedback):
        symbol = QgsFillSymbol.createSimple(
            {"color": "0,174,190,38", "outline_color": "0,155,170,255", "outline_width": "0.35"}
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setLabelsEnabled(False)
        layer.setLabeling(None)
        configure_attribute_table(layer)
        layer.triggerRepaint()
        if self in ResultStyle._live:
            ResultStyle._live.remove(self)
