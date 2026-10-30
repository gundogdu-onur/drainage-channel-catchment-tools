"""Channel target cells and catchment assignment on the D8 grid. NumPy only.

`delineate` returns the union catchment of all targets and serves as a test
reference; `delineate_first_hit` assigns each cell to the first channel reached.
"""

import math

import numpy as np

from ..common.terrain import report


def estimate_memory_gib(grid, fill):
    # Working arrays plus a worst-case filling queue. Excludes GDAL cache,
    # polygon vertices, QGIS and output providers.
    return grid.count * (52 if fill else 32) / 1024**3


def sample_polyline(vertices, cell):
    """Sample an open, linearized polyline in projected XY at equal steps.

    The step is a quarter cell, enlarged when needed to stay within one
    million segments.
    """
    xy = np.asarray(vertices, dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 2 or not np.isfinite(xy).all():
        raise ValueError("The polyline must contain at least two finite XY points.")
    lengths = np.hypot(*(np.diff(xy, axis=0).T))
    keep = np.r_[True, lengths > 0]
    xy = xy[keep]
    if len(xy) < 2:
        raise ValueError("The polyline has zero length.")
    lengths = np.hypot(*(np.diff(xy, axis=0).T))
    cum = np.r_[0.0, np.cumsum(lengths)]
    length = float(cum[-1])
    step = max(cell * 0.25, length / 1_000_000.0)
    n = max(1, math.ceil(length / step))
    distances = np.arange(n + 1, dtype=np.float64) * length / n
    distances[-1] = length
    seg = np.minimum(np.searchsorted(cum, distances, side="right") - 1, len(lengths) - 1)
    t = (distances - cum[seg]) / lengths[seg]
    return xy[seg] + (xy[seg + 1] - xy[seg]) * t[:, None]


def _clip_hits(ax, ay, bx, by, xmin, ymin, xmax, ymax):
    """Vectorized Liang-Barsky test of segments against closed cells (1e-12 tolerance)."""
    dx, dy = bx - ax, by - ay
    t0 = np.zeros(np.broadcast(ax, xmin).shape)
    t1 = np.ones_like(t0)
    hit = np.ones(t0.shape, dtype=bool)
    for p, q in ((-dx, ax - xmin), (dx, xmax - ax), (-dy, ay - ymin), (dy, ymax - ay)):
        parallel = np.abs(p) <= 1e-12
        hit &= (~parallel) | (q >= -1e-12)
        r = np.divide(q, p, out=np.zeros_like(t0), where=~parallel)
        entering = (p < 0) & ~parallel
        leaving = (p >= 0) & ~parallel
        hit &= ~entering | (r <= t1)
        t0 = np.where(entering & (r > t0), r, t0)
        hit &= ~leaving | (r >= t0)
        t1 = np.where(leaving & (r < t1), r, t1)
    return hit


def rasterize_targets(grid, sampled_parts, callback=None):
    """All closed cells touched by the sampled line, including corner touches.

    GDAL ALL_TOUCHED has different edge tie rules; deliberately not used here.
    """
    target = np.zeros((grid.height, grid.width), dtype=bool)
    total = max(1, sum(max(0, len(p) - 1) for p in sampled_parts))
    done = 0
    for points in sampled_parts:
        pts = np.asarray(points, dtype=np.float64)
        for start in range(0, len(pts) - 1, 4096):
            end = min(len(pts) - 1, start + 4096)
            a, b = pts[start:end], pts[start + 1 : end + 1]
            # Test long chords whole: subdividing them can change
            # floating-point corner-touch decisions.
            max_len = float(np.max(np.abs(b - a))) if len(a) else 0
            if max_len > grid.cell:
                for pa, pb in zip(a, b):
                    if not bool(_clip_hits(pa[0], pa[1], pb[0], pb[1], *grid.bounds)):
                        continue
                    dy = pb[1] - pa[1]
                    rmin = max(0, math.floor((min(pa[1], pb[1]) - grid.y0) / grid.cell) - 1)
                    rmax = min(
                        grid.height - 1, math.floor((max(pa[1], pb[1]) - grid.y0) / grid.cell) + 1
                    )
                    for row in range(rmin, rmax + 1):
                        ymin = grid.y0 + row * grid.cell
                        if abs(dy) <= 1e-12:
                            left, right = min(pa[0], pb[0]), max(pa[0], pb[0])
                        else:
                            u = np.clip(
                                [(ymin - pa[1]) / dy, (ymin + grid.cell - pa[1]) / dy], 0, 1
                            )
                            xx = pa[0] + u * (pb[0] - pa[0])
                            left, right = float(min(xx)), float(max(xx))
                        cmin = max(0, math.floor((left - grid.x0) / grid.cell) - 1)
                        cmax = min(grid.width - 1, math.floor((right - grid.x0) / grid.cell) + 1)
                        cols = np.arange(cmin, cmax + 1)
                        xmin = grid.x0 + cols * grid.cell
                        hit = _clip_hits(
                            pa[0],
                            pa[1],
                            pb[0],
                            pb[1],
                            xmin,
                            ymin,
                            xmin + grid.cell,
                            ymin + grid.cell,
                        )
                        target[row, cols[hit]] = True
                        if row % 128 == 0:
                            report(callback, done / total, "Identifying channel target cells")
                done += end - start
                report(callback, done / total, "Identifying channel target cells")
                continue
            c0 = np.floor((np.minimum(a[:, 0], b[:, 0]) - grid.x0) / grid.cell).astype(np.int64) - 1
            r0 = np.floor((np.minimum(a[:, 1], b[:, 1]) - grid.y0) / grid.cell).astype(np.int64) - 1
            c1 = np.floor((np.maximum(a[:, 0], b[:, 0]) - grid.x0) / grid.cell).astype(np.int64) + 1
            r1 = np.floor((np.maximum(a[:, 1], b[:, 1]) - grid.y0) / grid.cell).astype(np.int64) + 1
            for dr in range(int(np.max(r1 - r0)) + 1):
                rows = r0 + dr
                for dc in range(int(np.max(c1 - c0)) + 1):
                    cols = c0 + dc
                    eligible = (
                        (rows >= 0)
                        & (rows < grid.height)
                        & (rows <= r1)
                        & (cols >= 0)
                        & (cols < grid.width)
                        & (cols <= c1)
                    )
                    if not eligible.any():
                        continue
                    idx = np.flatnonzero(eligible)
                    rr, cc = rows[idx], cols[idx]
                    xmin, ymin = grid.x0 + cc * grid.cell, grid.y0 + rr * grid.cell
                    hits = _clip_hits(
                        a[idx, 0],
                        a[idx, 1],
                        b[idx, 0],
                        b[idx, 1],
                        xmin,
                        ymin,
                        xmin + grid.cell,
                        ymin + grid.cell,
                    )
                    target[rr[hits], cc[hits]] = True
            done += end - start
            report(callback, done / total, "Identifying channel target cells")
    report(callback, 1, "Channel target cells prepared")
    return target


def delineate(downstream, target, valid, callback=None):
    """Union of all cells whose path reaches ANY valid target cell.

    Pointer doubling examines path prefixes of lengths 1,2,4,... . Each pass
    reads immutable previous arrays, so chunk boundaries cannot affect results.
    """
    shape = np.shape(downstream)
    if np.shape(target) != shape or np.shape(valid) != shape:
        raise ValueError("Grid dimensions do not match.")
    p = np.array(downstream, dtype=np.int32, copy=True).ravel()
    reach = (np.asarray(target, dtype=bool) & np.asarray(valid, dtype=bool)).ravel()
    p[reach | ~np.asarray(valid, dtype=bool).ravel()] = -1
    n = p.size
    next_p = np.empty_like(p)
    next_reach = np.empty_like(reach)
    max_rounds = max(1, n.bit_length() + 1)
    chunk = 262144
    for iteration in range(max_rounds):
        active = 0
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            old = p[start:end]
            ok = old >= 0
            refs = old[ok]
            pr = next_p[start:end]
            rr = next_reach[start:end]
            pr[:] = -1
            rr[:] = reach[start:end]
            pr[ok] = p[refs]
            rr[ok] |= reach[refs]
            active += int(np.count_nonzero(pr >= 0))
            report(
                callback,
                (iteration + (end / n)) / max_rounds,
                f"Identifying cells draining to the channel (iteration {iteration + 1})",
            )
        p, next_p = next_p, p
        reach, next_reach = next_reach, reach
        if not active:
            break
    # Strictly decreasing D8 cannot cycle. Limit is a defensive guard only.
    report(callback, 1, "Catchment cells prepared")
    return reach.reshape(shape)


def delineate_first_hit(downstream, seed_owner, valid, callback=None):
    """Assign each cell to the FIRST channel reached along its D8 path.

    0 = no channel; positive labels identify distinct source features.
    Every seeded cell is absorbing: its outgoing terrain pointer is removed
    before jumping, so an upstream channel's catchment cannot reach a lower one.
    Immutable previous-pass arrays make results independent of chunk boundaries.
    """
    shape = np.shape(downstream)
    if np.shape(seed_owner) != shape or np.shape(valid) != shape:
        raise ValueError("Grid dimensions do not match.")
    owners = np.array(seed_owner, dtype=np.int32, copy=True).ravel()
    if np.any(owners < 0):
        raise ValueError("Channel labels cannot be negative.")
    owners[~np.asarray(valid, dtype=bool).ravel()] = 0
    pointers = np.array(downstream, dtype=np.int32, copy=True).ravel()
    n = pointers.size
    if np.any(pointers >= n) or np.any(pointers < -1):
        raise ValueError("Invalid downstream D8 cell index.")
    pointers[(owners > 0) | ~np.asarray(valid, dtype=bool).ravel()] = -1
    next_pointers = np.empty_like(pointers)
    next_owners = np.empty_like(owners)
    rounds = max(1, n.bit_length() + 1)
    for iteration in range(rounds):
        active = 0
        for start in range(0, n, 262144):
            end = min(n, start + 262144)
            old = pointers[start:end]
            unresolved = (owners[start:end] == 0) & (old >= 0)
            out_p = next_pointers[start:end]
            out_o = next_owners[start:end]
            out_p[:] = -1
            out_o[:] = owners[start:end]
            refs = old[unresolved]
            out_o[unresolved] = owners[refs]
            out_p[unresolved] = pointers[refs]
            out_p[out_o > 0] = -1
            active += int(np.count_nonzero(out_p >= 0))
            report(
                callback,
                (iteration + end / max(1, n)) / rounds,
                f"Assigning catchments to the first channel reached (iteration {iteration + 1})",
            )
        pointers, next_pointers = next_pointers, pointers
        owners, next_owners = next_owners, owners
        if not active:
            break
    # For defensively supplied cyclic pointers, unseeded cycles remain label 0.
    report(callback, 1, "Non-overlapping local catchment cells prepared")
    return owners.reshape(shape)


def label_counts(labels, maximum_label):
    """Count a large int32 label raster without a full-size int64 conversion."""
    counts = np.zeros(maximum_label + 1, dtype=np.int64)
    flat = np.asarray(labels).ravel()
    for start in range(0, flat.size, 262144):
        counts += np.bincount(flat[start : start + 262144], minlength=maximum_label + 1)
    return counts
