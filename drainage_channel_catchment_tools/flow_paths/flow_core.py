"""Longest continuous downstream D8 path; no QGIS dependency."""

import math
from array import array

import numpy as np

from ..common.terrain import report


def border_cells(valid):
    """Cells adjoining an invalid neighbor, including the array edge."""
    border = np.zeros(valid.shape, dtype=bool)
    border[0] = border[-1] = True
    border[:, 0] = border[:, -1] = True
    h, w = valid.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if not (dx or dy):
                continue
            ya, yb = max(0, -dy), min(h, h - dy)
            xa, xb = max(0, -dx), min(w, w - dx)
            border[ya:yb, xa:xb] |= ~valid[ya + dy : yb + dy, xa + dx : xb + dx]
    return border & valid


def restrict_to_catchment(downstream, inside, finite):
    """Stop at first exit. NEVER redirect an exiting flow along the boundary.

    Terminal codes: 1 polygon exit, 2 DEM/NoData edge, 3 pit/flat.
    The geometry adapter additionally uses 4 for a sub-cell polygon gap.
    """
    if downstream.shape != inside.shape or inside.shape != finite.shape:
        raise ValueError("Grid dimensions do not match.")
    active = inside & finite
    result = np.array(downstream, dtype=np.int32, copy=True)
    ends = np.zeros(inside.shape, dtype=np.uint8)
    sink = (result < 0) & active
    ends[sink] = 3
    ends[sink & border_cells(finite)] = 2
    result[~active] = -1
    flat, valid, reason = result.ravel(), active.ravel(), ends.ravel()
    for start in range(0, flat.size, 262144):
        block = flat[start : start + 262144]
        local = np.flatnonzero(block >= 0)
        if len(local):
            outside = ~valid[block[local]]
            cut = local[outside]
            reason[start + cut] = 1
            block[cut] = -1
    return result, ends, active


def longest_path(downstream, cell, callback=None):
    """Return upstream-to-downstream flat indices and horizontal length in m.

    Sum lengths to EVERY terminal via immutable pointer-doubling passes, then
    trace the maximizing start on the original graph. All multipart components
    and all exits compete; they are never artificially connected.
    """
    original = np.asarray(downstream)
    if original.ndim != 2 or not math.isfinite(cell) or cell <= 0:
        raise ValueError("Invalid flow grid or cell size.")
    n = original.size
    if not n:
        return np.empty(0, dtype=np.int32), 0.0
    if np.any(original < -1) or np.any(original >= n):
        raise ValueError("Invalid downstream cell index.")
    width = original.shape[1]
    pointers = np.array(original, dtype=np.int32, copy=True).ravel()
    distances = np.zeros(n, dtype=np.float64)
    for start in range(0, n, 262144):
        stop = min(n, start + 262144)
        refs = pointers[start:stop]
        local = np.flatnonzero(refs >= 0)
        ids, targets = start + local, refs[local]
        dx, dy = targets % width - ids % width, targets // width - ids // width
        if np.any((np.abs(dx) > 1) | (np.abs(dy) > 1) | ((dx == 0) & (dy == 0))):
            raise ValueError("The downstream cell is not a D8 neighbor.")
        distances[ids] = np.hypot(dx, dy) * cell
        report(callback, 0.1 * stop / n, "Calculating flow step lengths")
    next_pointers, next_distances = np.empty_like(pointers), np.empty_like(distances)
    rounds = n.bit_length() + 1
    for iteration in range(rounds):
        unresolved = 0
        for start in range(0, n, 262144):
            stop = min(n, start + 262144)
            old = pointers[start:stop]
            good = old >= 0
            refs = old[good]
            new_p, new_d = next_pointers[start:stop], next_distances[start:stop]
            new_p[:] = -1
            new_d[:] = distances[start:stop]
            new_p[good] = pointers[refs]
            new_d[good] += distances[refs]
            unresolved += int(np.count_nonzero(new_p >= 0))
            report(
                callback,
                0.1 + 0.8 * (iteration + stop / n) / rounds,
                f"Calculating the longest flow path (iteration {iteration + 1})",
            )
        pointers, next_pointers = next_pointers, pointers
        distances, next_distances = next_distances, distances
        if not unresolved:
            break
    else:
        raise ValueError("A cycle was found in the flow directions; no line was generated.")
    start = int(np.argmax(distances))
    length = float(distances[start])
    del pointers, next_pointers, distances, next_distances
    if length <= 0:
        report(callback, 1, "No flow path containing at least two cells was found")
        return np.empty(0, dtype=np.int32), 0.0
    route = array("i")
    current, flat = start, original.ravel()
    while current >= 0:
        route.append(current)
        current = int(flat[current])
        if len(route) > n:
            raise ValueError("A flow path cycle was detected.")
        if len(route) % 8192 == 0:
            report(callback, 0.95, "Tracing the longest flow path")
    report(callback, 1, "Longest flow path completed")
    return np.frombuffer(route, dtype=np.int32), length


def path_values(indices, grid, raw, routed=None):
    """Original DEM elevations; filling can change routing but never heights."""
    ids = np.asarray(indices)
    if len(ids) < 2:
        raise ValueError("A flow path must contain at least two points.")
    y = grid.y0 + (ids // grid.width + 0.5) * grid.cell
    x = grid.x0 + (ids % grid.width + 0.5) * grid.cell
    z = np.asarray(raw).ravel()[ids]
    if not np.isfinite(z).all():
        raise ValueError("An invalid elevation was found on the flow path.")
    length = float(np.hypot(np.diff(x), np.diff(y)).sum())
    drop = float(z[0] - z[-1])
    stats = dict(
        len_m=length,
        len_km=length / 1000,
        z_start=float(z[0]),
        z_end=float(z[-1]),
        z_max=float(z.max()),
        z_min=float(z.min()),
        drop_m=drop,
        slope_m_m=drop / length,
        slope_pct=100 * drop / length,
        nodes=len(ids),
        rise_n=int(np.count_nonzero(np.diff(z) > 1e-8)),
        fill_dz=0.0,
    )
    if routed is not None:
        stats["fill_dz"] = float(np.max(np.asarray(routed).ravel()[ids] - z))
    return x, y, z, stats
