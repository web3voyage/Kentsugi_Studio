"""Timeline baking tests: motion and camera keys, without launching Blender."""

from __future__ import annotations

import copy
import math
import unittest
from pathlib import Path

from kentsugi.catalog import load_catalog
from kentsugi.document import content_hash, load_document, normalize
from kentsugi.layout import solve
from kentsugi.preview import render_svg
from kentsugi.timeline import bake, sample_track

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "rainy_city.json"


class BakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.doc = load_document(EXAMPLE)
        self.plan = bake(self.doc, self.catalog, solve(self.doc, self.catalog))

    def track(self, object_id: str) -> dict:
        return next(t for t in self.plan["objects"] if t["id"] == object_id)

    def test_frame_range_matches_duration(self) -> None:
        self.assertEqual(1, self.plan["frame_start"])
        self.assertEqual(6 * 24, self.plan["frame_end"])
        self.assertEqual(6 * 24, self.plan["frame_end"] - self.plan["frame_start"] + 1)

    def test_static_objects_get_a_single_key(self) -> None:
        self.assertEqual(1, len(self.track("car_1")["keys"]))

    def test_walking_actor_is_animated(self) -> None:
        keys = self.track("character_1")["keys"]
        self.assertGreater(len(keys), 1)
        self.assertNotEqual(keys[0]["position"][:2], keys[-1]["position"][:2])

    def test_character_catalog_uses_articulated_hero_source(self) -> None:
        asset = self.catalog.require("human_character")
        self.assertEqual("procedural_character", asset.source["type"])
        self.assertIn("walk", asset.clips)

    def test_idle_action_bakes_an_animation_clip(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["actions"] = [{"actor": "character_1", "type": "idle", "start": 0, "duration": 2}]
        plan = bake(normalize(doc), self.catalog, solve(normalize(doc), self.catalog))
        motion = next(t for t in plan["objects"] if t["id"] == "character_1")["motion"]
        self.assertEqual("idle", motion["type"])
        self.assertEqual("idle", motion["clip"])

    def test_present_action_selects_the_target_side(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["placement"] = {"type": "at", "position": [3, 0]}
        doc["objects"][0]["facing"] = 0
        doc["actions"] = [
            {"actor": "character_1", "type": "present", "target": "car_1", "start": 0, "duration": 2}
        ]
        plan = bake(normalize(doc), self.catalog, solve(normalize(doc), self.catalog))
        motion = next(t for t in plan["objects"] if t["id"] == "character_1")["motion"]
        self.assertEqual("present", motion["type"])
        self.assertEqual("right", motion["side"])
        self.assertEqual("present", motion["clip"])

    def test_sequential_walks_compose_instead_of_replacing_each_other(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["render"]["duration"] = 10
        doc["objects"].append(
            {"id": "crate_2", "asset": "crate", "placement": {"type": "at", "position": [10, 0]}}
        )
        doc["actions"] = [
            {"actor": "character_1", "type": "walk_to", "target": "car_1", "start": 0, "duration": 3},
            {"actor": "character_1", "type": "walk_to", "target": "crate_2", "start": 4, "duration": 3},
        ]
        plan = bake(normalize(doc), self.catalog, solve(normalize(doc), self.catalog))
        keys = next(t for t in plan["objects"] if t["id"] == "character_1")["keys"]
        frames = {key["frame"] for key in keys}
        self.assertIn(1 + 3 * 24, frames)
        self.assertIn(1 + 4 * 24, frames)
        self.assertIn(1 + 7 * 24, frames)
        first_end = next(key for key in keys if key["frame"] == 1 + 3 * 24)
        self.assertNotEqual(first_end["position"][:2], keys[0]["position"][:2])
        self.assertNotEqual(keys[-1]["position"][:2], first_end["position"][:2])

    def test_look_at_bakes_a_visible_rotation(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["actions"] = [
            {"actor": "character_1", "type": "look_at", "target": "streetlight_1", "start": 1, "duration": 2}
        ]
        plan = bake(normalize(doc), self.catalog, solve(normalize(doc), self.catalog))
        keys = next(t for t in plan["objects"] if t["id"] == "character_1")["keys"]
        self.assertGreater(len(keys), 1)
        self.assertNotEqual(keys[0]["yaw"], keys[-1]["yaw"])

    def test_actor_stops_short_of_the_target(self) -> None:
        character = self.track("character_1")["keys"][-1]["position"]
        car = self.track("car_1")["keys"][-1]["position"]
        gap = math.hypot(car[0] - character[0], car[1] - character[1])
        self.assertGreater(gap, 2.0, "the actor walked into the car")

    def test_actor_stays_on_the_ground(self) -> None:
        for track in self.plan["objects"]:
            for key in track["keys"]:
                self.assertAlmostEqual(key["position"][2], 0.0)

    def test_follow_camera_is_sampled_every_frame(self) -> None:
        keys = self.plan["camera"]["keys"]
        self.assertEqual(self.plan["frame_end"] - self.plan["frame_start"] + 1, len(keys))

    def test_follow_camera_keeps_its_distance(self) -> None:
        target_keys = self.track("character_1")["keys"]
        for key in self.plan["camera"]["keys"]:
            position, _ = sample_track(target_keys, key["frame"])
            planar = math.hypot(key["position"][0] - position[0], key["position"][1] - position[1])
            self.assertAlmostEqual(planar, 6.0, places=3)

    def test_camera_never_goes_underground(self) -> None:
        for key in self.plan["camera"]["keys"]:
            self.assertGreater(key["position"][2], 0.0)

    def test_focal_length_follows_shot_type(self) -> None:
        self.assertEqual(50.0, self.plan["camera"]["focal_mm"])
        doc = copy.deepcopy(self.doc)
        doc["camera"]["shot"] = "close_up"
        plan = bake(doc, self.catalog, solve(doc, self.catalog))
        self.assertEqual(85.0, plan["camera"]["focal_mm"])

    def test_quality_selects_the_engine(self) -> None:
        self.assertEqual("EEVEE", self.plan["render"]["engine"])
        doc = copy.deepcopy(self.doc)
        doc["render"]["quality"] = "final"
        plan = bake(doc, self.catalog, solve(doc, self.catalog))
        self.assertEqual("CYCLES", plan["render"]["engine"])

    def test_orbit_camera_circles_the_target(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["camera"] = {"type": "orbit", "target": "car_1", "distance": 8, "height": 2, "shot": "wide"}
        doc["actions"] = []
        plan = bake(normalize(doc), self.catalog, solve(normalize(doc), self.catalog))
        car = next(t for t in plan["objects"] if t["id"] == "car_1")["keys"][0]["position"]
        for key in plan["camera"]["keys"]:
            planar = math.hypot(key["position"][0] - car[0], key["position"][1] - car[1])
            self.assertAlmostEqual(planar, 8.0, places=3)

    def test_hash_is_stable_under_key_reordering(self) -> None:
        reordered = dict(reversed(list(self.doc.items())))
        self.assertEqual(content_hash(self.doc), content_hash(reordered))

    def test_sample_track_clamps_outside_the_range(self) -> None:
        keys = self.track("character_1")["keys"]
        before, _ = sample_track(keys, -100)
        after, _ = sample_track(keys, 10_000)
        self.assertEqual(tuple(keys[0]["position"]), before)
        self.assertEqual(tuple(keys[-1]["position"]), after)


class PreviewTests(unittest.TestCase):
    def test_svg_is_well_formed_and_mentions_every_object(self) -> None:
        catalog = load_catalog()
        doc = load_document(EXAMPLE)
        plan = bake(doc, catalog, solve(doc, catalog))
        svg = render_svg(plan)

        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        for track in plan["objects"]:
            self.assertIn(track["id"], svg)

    def test_svg_escapes_titles(self) -> None:
        catalog = load_catalog()
        doc = load_document(EXAMPLE)
        doc["title"] = '<script>alert("x")</script>'
        plan = bake(doc, catalog, solve(doc, catalog))
        svg = render_svg(plan)
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)


if __name__ == "__main__":
    unittest.main()

