"""Resolve multiple channel lines touching a single DEM target cell."""

import math

import numpy as np
from qgis.core import QgsGeometry, QgsPointXY

from ..common.terrain import report


def add_channel_targets(owners, target, label, grid, records, callback=None):
    """Nearest line to the cell center wins; exact ties use the smaller FID.

    records maps positive label -> {'geometry': QgsGeometry, 'fid': int, ...}.
    Distance is evaluated only in contested cells. Neither input iteration order
    nor a user-defined (possibly duplicate) channel ID controls ownership.
    """
    current = records[label]
    flat = owners.ravel()
    target_flat = target.ravel()
    conflicts = 0
    for start in range(0, flat.size, 262144):
        end = min(flat.size, start + 262144)
        occupied = flat[start:end]
        wanted = target_flat[start:end]
        contested = np.flatnonzero(wanted & (occupied > 0))
        conflicts += len(contested)
        occupied[wanted & (occupied == 0)] = label
        for j, relative in enumerate(contested):
            i = start + int(relative)
            old_label = int(flat[i])
            previous = records[old_label]
            row, col = divmod(i, grid.width)
            center = QgsGeometry.fromPointXY(
                QgsPointXY(grid.x0 + (col + 0.5) * grid.cell, grid.y0 + (row + 0.5) * grid.cell)
            )
            new_distance = current["geometry"].distance(center)
            old_distance = previous["geometry"].distance(center)
            if (
                not (math.isfinite(new_distance) and math.isfinite(old_distance))
                or min(new_distance, old_distance) < 0
            ):
                raise ValueError(
                    "Could not calculate geometric distance for a shared channel cell."
                )
            if new_distance < old_distance or (
                new_distance == old_distance and current["fid"] < previous["fid"]
            ):
                flat[i] = label
            if j % 1024 == 0:
                report(
                    callback,
                    (start + j) / max(1, flat.size),
                    "Resolving shared channel target cells",
                )
        report(callback, end / max(1, flat.size), "Adding channels to the shared target grid")
    return conflicts
