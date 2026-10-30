"""World-aligned analysis grid, D8 flow directions and Priority-Flood filling.

NumPy only. Grid rows increase northward; coordinates and elevations are in meters.
"""

import math
from array import array
from dataclasses import dataclass

import numpy as np

MAX_CELLS = 100_000_000
LARGE_GRID = 5_000_000
NEIGHBORS = (
    (1, 0, 1.0),
    (1, 1, math.sqrt(2)),
    (0, 1, 1.0),
    (-1, 1, math.sqrt(2)),
    (-1, 0, 1.0),
    (-1, -1, math.sqrt(2)),
    (0, -1, 1.0),
    (1, -1, math.sqrt(2)),
)


class CanceledError(RuntimeError):
    pass


def report(callback, fraction, message):
    if callback and callback(min(1.0, max(0.0, fraction)), message) is False:
        raise CanceledError("Operation canceled by the user.")


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    x0: float
    y0: float
    cell: float

    @property
    def count(self):
        return self.width * self.height

    @property
    def bounds(self):
        return (
            self.x0,
            self.y0,
            self.x0 + self.width * self.cell,
            self.y0 + self.height * self.cell,
        )

    @classmethod
    def from_bounds(cls, bounds, cell):
        if not math.isfinite(cell) or cell <= 0:
            raise ValueError("Cell size must be positive and finite.")
        xmin, ymin, xmax, ymax = map(float, bounds)
        if not all(map(math.isfinite, bounds)) or xmax <= xmin or ymax <= ymin:
            raise ValueError("Invalid analysis extent.")
        # Snap the extent outward to multiples of the cell size.
        x0, y0 = math.floor(xmin / cell) * cell, math.floor(ymin / cell) * cell
        x1, y1 = math.ceil(xmax / cell) * cell, math.ceil(ymax / cell) * cell
        return cls(
            max(1, math.ceil((x1 - x0) / cell)), max(1, math.ceil((y1 - y0) / cell)), x0, y0, cell
        )


class _QuaternaryHeap:
    """Min-heap with four children per node and strict comparisons.

    Equal priorities are not popped in insertion order, unlike heapq with an
    index tie-breaker. The order decides how filled flats drain, so keep it.
    """

    def __init__(self):
        self.nodes = array("i")
        self.priorities = array("d")

    def __bool__(self):
        return bool(self.nodes)

    def push(self, node, priority):
        i = len(self.nodes)
        self.nodes.append(node)
        self.priorities.append(priority)
        while i > 0:
            parent = (i - 1) >> 2
            if priority >= self.priorities[parent]:
                break
            self.nodes[i] = self.nodes[parent]
            self.priorities[i] = self.priorities[parent]
            i = parent
        self.nodes[i], self.priorities[i] = node, priority

    def pop(self):
        result = self.nodes[0]
        node, priority = self.nodes.pop(), self.priorities.pop()
        size = len(self.nodes)
        if not size:
            return result
        i = 0
        while (i << 2) + 1 < size:
            child = (i << 2) + 1
            minimum = child
            for c in range(child + 1, min(child + 4, size)):
                if self.priorities[c] < self.priorities[minimum]:
                    minimum = c
            if priority <= self.priorities[minimum]:
                break
            self.nodes[i], self.priorities[i] = self.nodes[minimum], self.priorities[minimum]
            i = minimum
        self.nodes[i], self.priorities[i] = node, priority
        return result


def priority_flood(elevations, cell, callback=None):
    """Return a filled copy in which every valid cell drains to the domain edge."""
    z = np.array(elevations, dtype=np.float64, order="C", copy=True)
    if z.ndim != 2:
        raise ValueError("The elevation array must be two-dimensional.")
    h, w = z.shape
    valid = np.isfinite(z)
    interior = valid.copy()
    for dx, dy, _ in NEIGHBORS:
        y0, y1 = max(0, -dy), min(h, h - dy)
        x0, x1 = max(0, -dx), min(w, w - dx)
        interior[y0:y1, x0:x1] &= valid[y0 + dy : y1 + dy, x0 + dx : x1 + dx]
    interior[0, :] = False
    interior[-1, :] = False
    interior[:, 0] = False
    interior[:, -1] = False
    visited = valid & ~interior
    del interior
    heap = _QuaternaryHeap()
    flat = z.ravel()
    seen, finite = visited.ravel(), valid.ravel()
    # Seed edge cells from south to north and west to east; the order affects ties.
    for start in range(0, z.size, 262144):
        for relative in np.flatnonzero(seen[start : start + 262144]):
            i = start + int(relative)
            heap.push(i, float(flat[i]))
        report(
            callback,
            0.1 * min(z.size, start + 262144) / z.size,
            "Preparing depression-filling boundaries",
        )
    eps = max(1e-8, cell * 1e-9)
    processed = 0
    count = int(np.count_nonzero(valid))
    while heap:
        i = heap.pop()
        row, col = divmod(i, w)
        level = float(flat[i])
        for dx, dy, _ in NEIGHBORS:
            x, y = col + dx, row + dy
            if 0 <= x < w and 0 <= y < h:
                j = y * w + x
                if finite[j] and not seen[j]:
                    seen[j] = True
                    new_z = max(float(flat[j]), level + eps)
                    flat[j] = new_z
                    heap.push(j, new_z)
        processed += 1
        if processed % 8192 == 0:
            report(
                callback,
                0.1 + 0.9 * processed / max(1, count),
                "Filling depressions in the temporary grid",
            )
    report(callback, 1, "Depression filling completed")
    return z


def build_downstream(elevations, cell, callback=None):
    """Flat index of each cell's steepest-descent D8 neighbor, or -1."""
    z = np.asarray(elevations, dtype=np.float64)
    h, w = z.shape
    downstream = np.full(z.shape, -1, dtype=np.int32)
    rows_per_chunk = max(1, 262144 // max(1, w))
    for y0 in range(0, h, rows_per_chunk):
        y1 = min(h, y0 + rows_per_chunk)
        best = np.zeros((y1 - y0, w), dtype=np.float64)
        for dx, dy, factor in NEIGHBORS:
            ya, yb = max(y0, -dy), min(y1, h - dy)
            xa, xb = max(0, -dx), min(w, w - dx)
            if ya >= yb or xa >= xb:
                continue
            a = z[ya:yb, xa:xb]
            b = z[ya + dy : yb + dy, xa + dx : xb + dx]
            with np.errstate(invalid="ignore"):
                slope = (a - b) / (cell * factor)
            previous = best[ya - y0 : yb - y0, xa:xb]
            replace = np.isfinite(a) & np.isfinite(b) & (slope > previous)
            previous[replace] = slope[replace]
            target = downstream[ya:yb, xa:xb]
            indices = (
                np.arange(ya + dy, yb + dy, dtype=np.int32)[:, None] * w
                + np.arange(xa + dx, xb + dx, dtype=np.int32)[None, :]
            )
            target[replace] = indices[replace]
        report(callback, y1 / h, "Calculating D8 flow directions")
    return downstream
