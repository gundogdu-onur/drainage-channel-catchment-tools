"""Longest flow path core tests. NumPy only."""

import math
import unittest

import numpy as np

from drainage_channel_catchment_tools.common.terrain import (
    CanceledError,
    Grid,
    build_downstream,
    priority_flood,
)
from drainage_channel_catchment_tools.flow_paths.flow_core import (
    longest_path,
    path_values,
    restrict_to_catchment,
)


class CoreTests(unittest.TestCase):
    def test_straight_path_and_end_heights(self):
        z = np.tile(np.arange(9, 0, -1, dtype=float), (7, 1))
        path, length = longest_path(build_downstream(z, 1), 1)
        self.assertEqual(length, 8.0)
        np.testing.assert_array_equal(path, np.arange(9))
        x, y, heights, stats = path_values(path, Grid(9, 7, 500000, 4300000, 1), z)
        self.assertEqual(
            (stats["z_start"], stats["z_end"], stats["drop_m"], stats["slope_pct"]),
            (9.0, 1.0, 8.0, 100.0),
        )
        self.assertTrue((np.diff(x) > 0).all())

    def test_diagonal_length(self):
        rows, cols = np.indices((5, 5))
        z = 100.0 - rows - cols
        path, length = longest_path(build_downstream(z, 0.25), 0.25)
        self.assertAlmostEqual(length, math.sqrt(2))
        np.testing.assert_array_equal(path, [0, 6, 12, 18, 24])

    def test_first_exit_is_not_redirected(self):
        # Center flows steeply east, outside the catchment. A gentler northern
        # neighbor inside must not become a substitute downstream direction.
        z = np.array([[100.0, 10.0, 0.0], [100.0, 9.0, 0.0]])
        inside = np.array([[False, True, False], [False, True, False]])
        graph, ends, active = restrict_to_catchment(build_downstream(z, 1), inside, np.isfinite(z))
        self.assertTrue((graph[inside] == -1).all())
        self.assertTrue((ends[inside] == 1).all())
        self.assertEqual(longest_path(graph, 1)[1], 0.0)

    def test_multiple_outlets_and_disconnected_parts(self):
        graph = np.full((3, 7), -1, np.int32)
        graph[0, :5] = np.arange(1, 6)
        graph[2, :3] = np.arange(15, 18)
        path, length = longest_path(graph, 2.0)
        np.testing.assert_array_equal(path, np.arange(6))
        self.assertEqual(length, 10.0)

    def test_random_graphs_match_independent_scalar_walk(self):
        rng = np.random.default_rng(819)
        for fill in (False, True):
            for case in range(15):
                raw = rng.normal(size=(9, 13))
                raw[rng.random(raw.shape) < 0.07] = np.nan
                z = priority_flood(raw, 1) if fill else raw
                graph, _, valid = restrict_to_catchment(
                    build_downstream(z, 1), rng.random(z.shape) > 0.15, np.isfinite(z)
                )
                flat = graph.ravel()
                expected = []
                for start in range(flat.size):
                    at = start
                    length = 0.0
                    seen = set()
                    while flat[at] >= 0:
                        self.assertNotIn(at, seen)
                        seen.add(at)
                        to = int(flat[at])
                        length += math.hypot(to % 13 - at % 13, to // 13 - at // 13)
                        at = to
                    expected.append(length)
                path, length = longest_path(graph, 1)
                self.assertAlmostEqual(length, max(expected), places=10)
                if len(path):
                    self.assertTrue(valid.ravel()[path].all())
                    np.testing.assert_array_equal(flat[path[:-1]], path[1:])

    def test_long_path_crosses_processing_chunks(self):
        graph = np.arange(1, 270002, dtype=np.int32).reshape(1, -1)
        graph[0, -1] = -1
        path, length = longest_path(graph, 0.25)
        self.assertEqual(len(path), 270001)
        self.assertEqual(length, 67500.0)

    def test_original_heights_are_not_filled_heights(self):
        raw = np.array([[80.0, 100.0, 10.0, 20.0]])
        routed = np.array([[104.0, 103.0, 102.0, 101.0]])
        _, _, z, s = path_values(np.arange(4), Grid(4, 1, 0, 0, 2), raw, routed)
        np.testing.assert_array_equal(z, raw[0])
        self.assertEqual(
            (s["z_start"], s["z_end"], s["z_max"], s["z_min"]), (80.0, 20.0, 100.0, 10.0)
        )
        self.assertEqual((s["len_m"], s["drop_m"], s["rise_n"], s["fill_dz"]), (6.0, 60.0, 2, 92.0))

    def test_flat_or_no_edges_gives_no_fabricated_line(self):
        self.assertEqual(longest_path(build_downstream(np.ones((5, 7)), 1), 1)[1], 0.0)
        self.assertEqual(len(longest_path(np.full((3, 4), -1, np.int32), 1)[0]), 0)

    def test_cycle_and_non_d8_links_rejected(self):
        for graph in (np.array([[1, 0]]), np.array([[2, -1, -1]])):
            with self.assertRaises(ValueError):
                longest_path(graph, 1)

    def test_cancel(self):
        with self.assertRaises(CanceledError):
            longest_path(np.array([[1, -1]]), 1, lambda *_: False)


if __name__ == "__main__":
    unittest.main()
