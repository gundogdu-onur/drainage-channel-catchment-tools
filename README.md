# <img src="drainage_channel_catchment_tools/icons/icon.svg" width="40" alt="" align="top"> Drainage Channel Catchment Tools

A QGIS plugin that derives, from a digital elevation model (DEM), the **catchment area** of
each drainage channel and the **length** and **slope** of the **longest flow path** in that
catchment: the values needed to estimate design discharges with the **Rational Method**.

![QGIS 3.40+](https://img.shields.io/badge/QGIS-3.40%2B-589632)
![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0-blue)

## Purpose

The plugin prepares the values used to size drainage channels with the Rational
Method and to calculate the time of concentration, for all channels in one run:

| Value | Tool | Output field |
|---|---|---|
| Catchment area | Local Channel Catchments | `area_m2` |
| Length of the longest flow path | Longest Flow Path by Catchment | `len_m` |
| Start and end elevations of the longest flow path | Longest Flow Path by Catchment | `z_start`, `z_end` |
| Elevation drop along the longest flow path | Longest Flow Path by Catchment | `drop_m` |
| Mean slope of the longest flow path | Longest Flow Path by Catchment | `slope_m_m` |

Runoff coefficients, rainfall intensities and discharges are not calculated; they follow the
design standard you work with.

## Tools

### Local Channel Catchments

Creates one catchment polygon for each drainage channel line. The DEM is resampled to the
chosen cell size and a D8 flow direction is computed for every cell. Each cell is then
assigned to the **first channel its flow path reaches**, so every channel receives only its
own local contributing area:

- Catchments never overlap, and an area captured by an upstream channel is not counted again
  downstream. When a channel also collects water from upstream channels, add their areas as
  your design method requires.
- Process all channels that should intercept one another in the same run; catchments from
  separate runs can overlap.
- An optional polygon mask restricts the analysis, for example to the area inside a road
  corridor. Flow from outside the mask is ignored.
- Depressions in the DEM can be filled in a temporary grid; the source DEM is never changed.

### Longest Flow Path by Catchment

Traces the longest continuous D8 flow path inside each catchment polygon and writes it as a
3D line from upstream to downstream, with its length, endpoint elevations, elevation drop and
mean slope.

- Length is measured horizontally between cell centers.
- Elevations come from the original DEM, even when depressions are filled for routing.
- Mean slope is `(z_start − z_end) / len_m`.
- Paths stop where the flow first leaves the catchment and never cross gaps or holes in the
  polygon.
- Paths that end in a pit, a flat area or at the edge of the DEM are flagged in the
  termination reason, so they can be reviewed before the time of concentration is calculated.

The tool works on the output of Local Channel Catchments or on any other catchment polygons.

## Requirements

- QGIS 3.40 or later (3.x). The tools use the NumPy and GDAL bundled with QGIS; no other
  packages are needed.
- A DEM stored as a local GeoTIFF or VRT, in a projected CRS with horizontal and vertical
  units in meters (for example UTM).
- Drainage channels as a line layer.

## Installation

1. Download `drainage_channel_catchment_tools-<version>.zip` from the
   [Releases](https://github.com/gundogdu-onur/drainage-channel-catchment-tools/releases) page.
2. In QGIS, open **Plugins › Manage and Install Plugins › Install from ZIP** and select the file.
3. Open **Processing › Toolbox** and expand **Drainage Channel Catchment Tools**. The tools are
   also in the **Raster** menu and on the Plugins toolbar.

## Workflow

1. Load the DEM and the drainage channel lines.
2. Run **Local Channel Catchments** to get the catchment area of each channel.
3. Run **Longest Flow Path by Catchment** on the resulting catchments with the same DEM. With
   cell size `0` and *Automatic* depression filling, it reuses the settings stored in the
   catchment layer.
4. Check the termination reason of each flow path.
5. Export both attribute tables and continue the discharge calculation with your design
   standard.

Result layers are styled automatically. To restyle an existing result layer, select it and
use **Raster › Drainage Channel Catchment Tools › Update Active … Layer**.

In models and scripts, the algorithm IDs are `drainagecatchment:delineate` and
`drainagecatchment:longestflowpath`.

### Catchment fields

| Field | Shown as | Content |
|---|---|---|
| `channel_id` | Channel ID | Value of the selected ID field, or the source feature ID |
| `src_fid` | FID | Feature ID of the source channel |
| `area_m2` | Area (m²) | Catchment area, clipped to the mask when one is used |

Further fields, such as `area_ha`, the cell size and processing diagnostics, are stored but
hidden in the attribute table.

### Flow path fields

| Field | Shown as |
|---|---|
| `catch_id` | Catchment Name |
| `len_m` | Longest Flow Path Length (m) |
| `z_start` | Flow Path Start Elevation (m) |
| `z_end` | Flow Path End Elevation (m) |
| `slope_m_m` | Mean Flow Path Slope (m/m) |
| `area_m2` | Catchment Area (m²) |
| `end_type` | Flow Path Termination Reason: `catchment_edge`, `dem_edge`, `pit_flat` or `polygon_gap` |

Further fields, such as `drop_m`, `len_km`, `slope_pct` and processing diagnostics, are stored
but hidden in the attribute table.

## Example data

[`examples/`](examples) contains a DEM derived from real terrain data, two channels and the
expected results of both tools.

## Development

The plugin follows the layout of the
[PyQGIS Developer Cookbook](https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/index.html):

```text
drainage_channel_catchment_tools/
├── __init__.py              classFactory()
├── mainPlugin.py            plugin class: provider registration, menu and toolbar
├── metadata.txt
├── LICENSE
├── icon.png
├── icons/                   SVG icons
├── processing_provider/
│   ├── provider.py
│   ├── local_channel_catchments_algorithm.py
│   └── longest_flow_path_algorithm.py
├── catchments/              catchment delineation code
├── flow_paths/              flow path code
└── common/                  shared grid, D8 and GDAL helpers
```

To work on the plugin, add the repository folder to QGIS's plugin search path with the
`QGIS_PLUGINPATH` environment variable, enable the plugin in the Plugin Manager and reload
it after changes with the [Plugin Reloader](https://plugins.qgis.org/plugins/plugin_reloader/)
plugin.

Run the tests with the Python interpreter that ships with QGIS (on Windows, the OSGeo4W
Shell):

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t .
```

The core tests need only NumPy; the QGIS integration tests are skipped when QGIS is not
available. Code style is checked with [Ruff](https://docs.astral.sh/ruff/):

```sh
ruff format . && ruff check .
```

To build the installable ZIP in `dist/`:

```sh
python scripts/build_zip.py
```

## License

Copyright © 2026 Onur Gündoğdu. Released under the
[GNU General Public License v2.0](LICENSE).
