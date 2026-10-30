"""First-hit watershed tests against independent scalar path tracing."""

import unittest

import numpy as np

from drainage_channel_catchment_tools.catchments import core
from drainage_channel_catchment_tools.common.terrain import (
    CanceledError,
    build_downstream,
    priority_flood,
)


class FirstHitTests(unittest.TestCase):
    def test_serial_channels_are_disjoint(self):
        z = np.tile(np.arange(9, 0, -1, dtype=float), (7, 1))
        seeds = np.zeros(z.shape, np.int32)
        seeds[:, 2] = 1
        seeds[:, 5] = 2
        seeds[:, 7] = 3
        labels = core.delineate_first_hit(build_downstream(z, 1), seeds, np.isfinite(z))
        expected = np.tile([1, 1, 1, 2, 2, 2, 3, 3, 0], (7, 1))
        np.testing.assert_array_equal(labels, expected)
        self.assertEqual(core.label_counts(labels, 3).tolist(), [7, 21, 21, 14])
        self.assertEqual(int(sum((labels == i).sum() for i in (1, 2, 3))), int((labels > 0).sum()))

    def test_unselected_upstream_does_not_block(self):
        z = np.tile(np.arange(9, 0, -1, dtype=float), (7, 1))
        seeds = np.zeros(z.shape, np.int32)
        seeds[:, 5] = 1
        labels = core.delineate_first_hit(build_downstream(z, 1), seeds, np.isfinite(z))
        self.assertEqual(int((labels == 1).sum()), 42)

    def test_scalar_first_hit_and_union_conservation(self):
        rng = np.random.default_rng(9041)
        for fill in (False, True):
            for _ in range(15):
                z = rng.integers(0, 25, size=(19, 23)).astype(float)
                z[rng.random(z.shape) < 0.06] = np.nan
                if fill:
                    z = priority_flood(z, 0.25)
                valid = np.isfinite(z)
                d = build_downstream(z, 0.25)
                seeds = np.where(
                    rng.random(z.shape) < 0.1, rng.integers(1, 6, size=z.shape), 0
                ).astype(np.int32)
                seeds[~valid] = 0
                expected = np.zeros(z.size, np.int32)
                for i in range(z.size):
                    node = i
                    while node >= 0:
                        if seeds.ravel()[node]:
                            expected[i] = seeds.ravel()[node]
                            break
                        node = int(d.ravel()[node])
                actual = core.delineate_first_hit(d, seeds, valid)
                np.testing.assert_array_equal(actual.ravel(), expected)
                union = core.delineate(d, seeds > 0, valid)
                np.testing.assert_array_equal(actual > 0, union)

    def test_long_path_across_processing_chunks(self):
        n = 270001
        d = np.arange(1, n + 1, dtype=np.int32).reshape(1, n)
        d[0, -1] = -1
        seeds = np.zeros((1, n), np.int32)
        seeds[0, 262143] = 7
        seeds[0, 269999] = 8
        result = core.delineate_first_hit(d, seeds, np.ones((1, n), bool))
        self.assertTrue((result[0, :262144] == 7).all())
        self.assertTrue((result[0, 262144:270000] == 8).all())
        self.assertEqual(result[0, -1], 0)

    def test_flat_and_no_data(self):
        z = np.ones((4, 5))
        z[1, 2] = np.nan
        seeds = np.zeros(z.shape, np.int32)
        seeds[1, 2] = 4
        seeds[2, 3] = 5
        labels = core.delineate_first_hit(build_downstream(z, 1), seeds, np.isfinite(z))
        self.assertEqual(np.count_nonzero(labels), 1)
        self.assertEqual(labels[2, 3], 5)
        self.assertEqual(labels[1, 2], 0)

    def test_cancel(self):
        with self.assertRaises(CanceledError):
            core.delineate_first_hit(
                np.full((10, 10), -1), np.ones((10, 10)), np.ones((10, 10), bool), lambda *_: False
            )


if __name__ == "__main__":
    unittest.main()
