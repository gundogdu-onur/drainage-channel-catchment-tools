"""Downstream arrows on line symbols; geometry and metrics remain untouched."""

from qgis.core import (
    Qgis,
    QgsLineSymbol,
    QgsMarkerLineSymbolLayer,
    QgsMarkerSymbol,
    QgsProcessingLayerPostProcessorInterface,
    QgsSingleSymbolRenderer,
)
from qgis.PyQt import sip

VISIBLE_FIELDS = ("catch_id", "len_m", "z_start", "z_end", "slope_m_m", "area_m2", "end_type")

FIELD_ALIASES = {
    "catch_id": "Catchment Name",
    "len_m": "Longest Flow Path Length (m)",
    "z_start": "Flow Path Start Elevation (m)",
    "z_end": "Flow Path End Elevation (m)",
    "slope_m_m": "Mean Flow Path Slope (m/m)",
    "area_m2": "Catchment Area (m²)",
    "end_type": "Flow Path Termination Reason",
    "poly_fid": "Source Catchment Feature ID",
    "channel_id": "Channel ID",
    "src_fid": "Source Channel Feature ID",
    "len_km": "Flow Path Length (km)",
    "z_max": "Maximum Flow Path Elevation (m)",
    "z_min": "Minimum Flow Path Elevation (m)",
    "drop_m": "Endpoint Elevation Difference (m)",
    "slope_pct": "Mean Flow Path Slope (%)",
    "cell_m": "Analysis Cell Size (m)",
    "fill_pits": "Depression Filling Enabled",
    "nodes": "Flow Path Vertex Count",
    "status": "Processing Status",
    "rise_n": "Uphill Step Count in Original DEM",
    "fill_dz": "Maximum Depression-Filling Depth Along Path (m)",
}


def configure_attribute_table(layer):
    """Display seven ordered columns without deleting or recalculating data."""
    config = layer.attributeTableConfig()
    config.update(layer.fields())
    columns = config.columns()
    order = {name: index for index, name in enumerate(VISIBLE_FIELDS)}
    for column in columns:
        column.hidden = column.name not in order
    columns.sort(key=lambda column: order.get(column.name, len(order)))
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)


def flow_symbol():
    symbol = QgsLineSymbol.createSimple({"line_color": "20,92,186,255", "line_width": "0.55"})
    # Screen/paper units keep arrows legible at different map scales. Interval
    # arrows follow the route; the last-vertex arrow also covers short lines.
    for placement, size in (
        (Qgis.MarkerLinePlacement.Interval, 2.4),
        (Qgis.MarkerLinePlacement.LastVertex, 2.7),
    ):
        arrows = QgsMarkerLineSymbolLayer()
        arrows.setPlacements(placement)
        arrows.setRotateSymbols(True)
        # Use the local segment direction at sharp D8 bends, without averaging
        # angles across the vertex (which can visually reverse a corner arrow).
        arrows.setAverageAngleLength(0.0)
        arrows.setInterval(12.0)
        arrows.setIntervalUnit(Qgis.RenderUnit.Millimeters)
        if placement == Qgis.MarkerLinePlacement.Interval:
            arrows.setOffsetAlongLine(6.0)
            arrows.setOffsetAlongLineUnit(Qgis.RenderUnit.Millimeters)
        marker = QgsMarkerSymbol.createSimple(
            {
                "name": "filled_arrowhead",
                "color": "8,48,107,255",
                "outline_style": "no",
                "size": str(size),
                "angle": "0",
            }
        )
        marker.setSizeUnit(Qgis.RenderUnit.Millimeters)
        arrows.setSubSymbol(marker)
        symbol.appendSymbolLayer(arrows)
    return symbol


class ResultStyle(QgsProcessingLayerPostProcessorInterface):
    # Processing holds only a C++ pointer; keep the Python object alive until used.
    _live = []

    def __init__(self):
        super().__init__()
        ResultStyle._live[:] = [item for item in ResultStyle._live if not sip.isdeleted(item)]
        ResultStyle._live.append(self)

    def postProcessLayer(self, layer, context, feedback):
        layer.setRenderer(QgsSingleSymbolRenderer(flow_symbol()))
        layer.setLabelsEnabled(False)
        layer.setLabeling(None)
        for name, alias in FIELD_ALIASES.items():
            index = layer.fields().indexFromName(name)
            if index >= 0:
                layer.setFieldAlias(index, alias)
        configure_attribute_table(layer)
        layer.triggerRepaint()
        if self in ResultStyle._live:
            ResultStyle._live.remove(self)
