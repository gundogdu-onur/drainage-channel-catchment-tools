"""Catchment delineation core tests. NumPy only."""

import unittest

import numpy as np

from drainage_channel_catchment_tools.catchments import core
from drainage_channel_catchment_tools.common.terrain import (
    CanceledError,
    Grid,
    build_downstream,
    priority_flood,
)


class CoreTests(unittest.TestCase):
    def test_planar_known_catchment(self):
        z = np.tile(np.arange(9, 0, -1, dtype=float), (7, 1))
        g = Grid(9, 7, 0.0, 0.0, 1.0)
        target = core.rasterize_targets(g, [core.sample_polyline([(4.5, 0.1), (4.5, 6.9)], 1)])
        mask = core.delineate(build_downstream(z, 1), target, np.isfinite(z))
        expected = np.zeros((7, 9), bool)
        expected[:, :5] = True
        np.testing.assert_array_equal(mask, expected)

    def test_closed_cell_edge_and_corner_contacts(self):
        g = Grid(3, 3, 0.0, 0.0, 1.0)
        mask = core.rasterize_targets(g, [np.array([[1.0, 0.2], [1.0, 2.8]])])
        expected = np.zeros((3, 3), bool)
        expected[:, :2] = True
        np.testing.assert_array_equal(mask, expected)
        corner = core.rasterize_targets(g, [np.array([[0.2, 0.2], [1.0, 1.0]])])
        self.assertEqual(int(corner.sum()), 4)

    def test_strict_slope_and_tie_order(self):
        z = np.full((3, 3), 20.0)
        z[1, 1] = 10
        z[1, 2] = z[2, 1] = 9
        self.assertEqual(build_downstream(z, 1)[1, 1], 5)
        z[2, 2] = 8.5
        self.assertEqual(build_downstream(z, 1)[1, 1], 8)
        self.assertTrue((build_downstream(np.ones((3, 3)), 1) == -1).all())

    def test_fill_does_not_modify_input_and_creates_descent(self):
        z = np.full((7, 7), 12.0)
        z[1:-1, 1:-1] = 0.0
        original = z.copy()
        filled = priority_flood(z, 0.25)
        np.testing.assert_array_equal(z, original)
        self.assertTrue((filled[1:-1, 1:-1] > 12).all())
        self.assertTrue((build_downstream(filled, 0.25)[1:-1, 1:-1] >= 0).all())

    def test_no_data_targets_excluded(self):
        z = np.ones((4, 4))
        z[2, 2] = np.nan
        targets = np.ones((4, 4), bool)
        mask = core.delineate(build_downstream(z, 1), targets, np.isfinite(z))
        self.assertEqual(int(mask.sum()), 15)
        self.assertFalse(mask[2, 2])

    def test_independent_channels_overlap(self):
        z = np.tile(np.arange(8, 0, -1, dtype=float), (5, 1))
        down = build_downstream(z, 1)
        masks = []
        for col in (2, 5):
            target = np.zeros(z.shape, bool)
            target[:, col] = True
            masks.append(core.delineate(down, target, np.isfinite(z)))
        self.assertEqual(int(masks[0].sum()), 15)
        self.assertEqual(int(masks[1].sum()), 30)
        self.assertTrue((masks[0] & masks[1] == masks[0]).all())

    def test_pointer_doubling_matches_independent_scalar_walk(self):
        rng = np.random.default_rng(118)
        for _ in range(20):
            z = rng.normal(size=(17, 19))
            target = rng.random(z.shape) < 0.03
            down = build_downstream(z, 1)
            expected = np.zeros(z.size, bool)
            for i in range(z.size):
                current = i
                while current >= 0:
                    if target.ravel()[current]:
                        expected[i] = True
                        break
                    current = int(down.ravel()[current])
            np.testing.assert_array_equal(
                core.delineate(down, target, np.isfinite(z)).ravel(), expected
            )

    def test_cancellation(self):
        for action in (
            lambda: build_downstream(np.ones((10, 10)), 1, lambda *_: False),
            lambda: priority_flood(np.ones((10, 10)), 1, lambda *_: False),
            lambda: core.delineate(
                np.full((10, 10), -1),
                np.ones((10, 10), bool),
                np.ones((10, 10), bool),
                lambda *_: False,
            ),
            lambda: core.rasterize_targets(
                Grid(10, 10, 0, 0, 1), [np.array([[1.0, 1.0], [2.0, 2.0]])], lambda *_: False
            ),
        ):
            with self.assertRaises(CanceledError):
                action()


if __name__ == "__main__":
    unittest.main()
