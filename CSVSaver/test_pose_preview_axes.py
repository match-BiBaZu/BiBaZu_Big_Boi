import math
import unittest

import pose_preview


class PosePreviewAxesTests(unittest.TestCase):
    def test_view_matches_roadmap_axis_directions(self):
        view = pose_preview._view_matrix()
        axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        projected = []
        for axis in axes:
            camera = pose_preview._transform(axis, view)
            projected.append((camera[0], -camera[1]))

        horizontal = 1.0 / math.sqrt(2.0)
        vertical = 1.0 / math.sqrt(6.0)
        expected = (
            (-horizontal, vertical),
            (horizontal, vertical),
            (0.0, -2.0 * vertical),
        )
        for actual, target in zip(projected, expected, strict=True):
            self.assertAlmostEqual(actual[0], target[0])
            self.assertAlmostEqual(actual[1], target[1])


if __name__ == "__main__":
    unittest.main()
