"""Layout solver tests: the invariants a language model cannot be trusted with."""

from __future__ import annotations

import copy
import math
import unittest
from pathlib import Path

from kentsugi.catalog import load_catalog
from kentsugi.document import load_document, normalize
from kentsugi.layout import forward, right, solve, yaw_towards

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "rainy_city.json"


def distance(a, b) -> float:
    return math.hypot(b.position[0] - a.position[0], b.position[1] - a.position[1])


class FrameConventionTests(unittest.TestCase):
    def test_yaw_zero_faces_north(self) -> None:
        fx, fy = forward(0.0)
        self.assertAlmostEqual(fx, 0.0)
        self.assertAlmostEqual(fy, 1.0)

    def test_right_of_north_is_east(self) -> None:
        rx, ry = right(0.0)
        self.assertAlmostEqual(rx, 1.0)
        self.assertAlmostEqual(ry, 0.0)

    def test_yaw_towards_is_the_inverse_of_forward(self) -> None:
        for yaw in (-170.0, -90.0, 0.0, 37.0, 90.0, 179.0):
            fx, fy = forward(yaw)
            self.assertAlmostEqual(yaw_towards((0.0, 0.0), (fx, fy)), yaw, places=6)

    def test_facing_east_target(self) -> None:
        self.assertAlmostEqual(yaw_towards((0.0, 0.0), (5.0, 0.0)), -90.0)


class SolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.doc = load_document(EXAMPLE)

    def test_example_converges_without_warnings(self) -> None:
        layout = solve(self.doc, self.catalog)
        self.assertEqual([], layout.warnings)

    def test_relative_placement_respects_distance(self) -> None:
        layout = solve(self.doc, self.catalog)
        character = layout.require("character_1")
        car = layout.require("car_1")
        # 9m requested; collision resolution may nudge it, never below contact.
        self.assertGreaterEqual(distance(character, car), character.radius + car.radius)
        self.assertLessEqual(distance(character, car), 9.5)

    def test_mutual_facing_and_placement_resolves(self) -> None:
        """character faces car, car is placed ahead of character: a real loop."""
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["facing"] = "character_1"
        layout = solve(normalize(doc), self.catalog)
        self.assertEqual([], layout.warnings)
        character = layout.require("character_1")
        car = layout.require("car_1")
        self.assertAlmostEqual(
            character.yaw, yaw_towards(character.position, car.position), places=4
        )

    def test_nothing_overlaps(self) -> None:
        layout = solve(self.doc, self.catalog)
        placed = list(layout.objects.values())
        for i, a in enumerate(placed):
            for b in placed[i + 1:]:
                self.assertGreaterEqual(
                    distance(a, b), a.radius + b.radius - 1e-6, f"{a.id} overlaps {b.id}"
                )

    def test_coincident_objects_are_pushed_apart(self) -> None:
        doc = normalize(
            {
                "kentsugi_version": "0.1.0",
                "environment": {"preset": "void", "time_of_day": "day"},
                "objects": [
                    {"id": "a", "asset": "crate", "placement": {"type": "at", "position": [0, 0]}},
                    {
                        "id": "b",
                        "asset": "crate",
                        "placement": {"type": "relative", "ref": "a", "direction": "ahead", "distance": 0},
                    },
                ],
                "camera": {"type": "static"},
                "render": {"duration": 1},
            }
        )
        layout = solve(doc, self.catalog)
        a, b = layout.require("a"), layout.require("b")
        self.assertGreaterEqual(distance(a, b), a.radius + b.radius - 1e-6)

    def test_pinned_objects_do_not_move(self) -> None:
        """An explicit coordinate is a promise; collision resolution must not break it."""
        doc = normalize(
            {
                "kentsugi_version": "0.1.0",
                "environment": {"preset": "void", "time_of_day": "day"},
                "objects": [
                    {"id": "pinned", "asset": "crate", "placement": {"type": "at", "position": [3, 4]}},
                    {
                        "id": "pushed",
                        "asset": "crate",
                        "placement": {"type": "relative", "ref": "pinned", "direction": "north", "distance": 0.1},
                    },
                ],
                "camera": {"type": "static"},
                "render": {"duration": 1},
            }
        )
        layout = solve(doc, self.catalog)
        self.assertAlmostEqual(layout.require("pinned").position[0], 3.0)
        self.assertAlmostEqual(layout.require("pinned").position[1], 4.0)

    def test_overlapping_pinned_objects_are_reported(self) -> None:
        doc = normalize(
            {
                "kentsugi_version": "0.1.0",
                "environment": {"preset": "void", "time_of_day": "day"},
                "objects": [
                    {"id": "a", "asset": "crate", "placement": {"type": "at", "position": [0, 0]}},
                    {"id": "b", "asset": "crate", "placement": {"type": "at", "position": [0, 0]}},
                ],
                "camera": {"type": "static"},
                "render": {"duration": 1},
            }
        )
        layout = solve(doc, self.catalog)
        self.assertTrue(any("a/b" in warning for warning in layout.warnings))

    def test_static_assets_are_not_displaced(self) -> None:
        doc = normalize(
            {
                "kentsugi_version": "0.1.0",
                "environment": {"preset": "city", "time_of_day": "day"},
                "objects": [
                    {"id": "tower", "asset": "building_block", "placement": {"type": "at", "position": [0, 0]}},
                    {
                        "id": "box",
                        "asset": "crate",
                        "placement": {"type": "relative", "ref": "tower", "direction": "north", "distance": 1},
                    },
                ],
                "camera": {"type": "static"},
                "render": {"duration": 1},
            }
        )
        layout = solve(doc, self.catalog)
        tower = layout.require("tower")
        self.assertAlmostEqual(tower.position[0], 0.0)
        self.assertAlmostEqual(tower.position[1], 0.0)
        self.assertGreaterEqual(distance(tower, layout.require("box")), tower.radius + layout.require("box").radius - 1e-6)

    def test_world_directions_are_absolute(self) -> None:
        doc = normalize(
            {
                "kentsugi_version": "0.1.0",
                "environment": {"preset": "void", "time_of_day": "day"},
                "objects": [
                    {"id": "a", "asset": "crate", "placement": {"type": "at", "position": [0, 0]}, "facing": 180},
                    {
                        "id": "b",
                        "asset": "crate",
                        "placement": {"type": "relative", "ref": "a", "direction": "east", "distance": 10},
                    },
                ],
                "camera": {"type": "static"},
                "render": {"duration": 1},
            }
        )
        layout = solve(doc, self.catalog)
        b = layout.require("b")
        self.assertAlmostEqual(b.position[0], 10.0, places=4)
        self.assertAlmostEqual(b.position[1], 0.0, places=4)


if __name__ == "__main__":
    unittest.main()

