"""Drainage Channel Catchment Tools QGIS plugin."""


def classFactory(iface):
    from .mainPlugin import DrainageChannelCatchmentPlugin

    return DrainageChannelCatchmentPlugin(iface)
