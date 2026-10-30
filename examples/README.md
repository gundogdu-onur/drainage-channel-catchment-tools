# Example data

A DEM derived from real terrain data of a hillside, with two drainage channels across the slope.
The DEM covers about 373 × 233 m with 0.1 m cells. Its coordinates were shifted to a made-up
location in EPSG:5254 (TUREF / TM30); the terrain itself is unchanged. A gully on the slope drains
into K101, which is why its catchment is much larger than that of K102.

| File | Content |
|---|---|
| `sample_dem.tif` | DEM |
| `sample_channels.gpkg` | Channel lines K101 and K102 |
| `sample_mask.gpkg` | Analysis mask |
| `sample_catchments.gpkg` | Expected catchments |
| `sample_flow_paths.gpkg` | Expected longest flow paths |

## Catchments

Run **Local Channel Catchments** with `sample_dem.tif`, the `channels` layer,
ID field `channel_id`, cell size 0.5 m, depression filling off and bilinear resampling.
Add `sample_mask.gpkg` as the analysis mask for the last column.

| Channel ID | FID | Area (m²) | Area with mask (m²) |
|---|---:|---:|---:|
| K101 | 1 | 7645.50 | 6959.97 |
| K102 | 2 | 427.25 | 427.25 |

The catchments do not overlap.

## Flow paths

Run **Longest Flow Path by Catchment** on `sample_catchments.gpkg` and `sample_dem.tif` with cell
size 0 and automatic depression filling. Cell size 0 reuses the 0.5 m stored in the catchment
layer.

| Catchment | Length (m) | Start elevation (m) | End elevation (m) | Mean slope (m/m) | Termination |
|---|---:|---:|---:|---:|---|
| K101 | 145.30 | 1381.31 | 1346.45 | 0.240 | catchment_edge |
| K102 | 61.95 | 1365.06 | 1351.06 | 0.226 | catchment_edge |

Length is measured between cell centers along the path, so a diagonal step counts as 1.41 times
the cell size.
