"""Validator tests. Run with: python -m unittest discover -s tests -t ."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from kentsugi.catalog import load_catalog
from kentsugi.document import load_document, new_document, normalize
from kentsugi.validate import errors, validate, warnings

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "rainy_city.json"


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.doc = load_document(EXAMPLE)

    def codes(self, doc) -> set[str]:
        return {i.code for i in validate(normalize(doc), self.catalog)}

    def test_example_is_valid(self) -> None:
        self.assertEqual([], validate(self.doc, self.catalog))

    def test_new_document_is_valid(self) -> None:
        self.assertEqual([], validate(normalize(new_document()), self.catalog))

    def test_unknown_asset_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][0]["asset"] = "spaceship"
        self.assertIn("unknown_asset", self.codes(doc))

    def test_path_traversal_asset_id_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][0]["asset"] = "../../etc/passwd"
        self.assertIn("bad_id", self.codes(doc))

    def test_duplicate_object_ids_are_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["id"] = doc["objects"][0]["id"]
        self.assertIn("duplicate_id", self.codes(doc))

    def test_dangling_placement_reference_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["placement"]["ref"] = "nobody"
        self.assertIn("unknown_reference", self.codes(doc))

    def test_placement_cycle_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][0]["placement"] = {
            "type": "relative", "ref": "car_1", "direction": "ahead", "distance": 3,
        }
        self.assertIn("placement_cycle", self.codes(doc))

    def test_facing_cycle_is_allowed(self) -> None:
        """A facing loop is a natural description; the solver relaxes it."""
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["facing"] = "character_1"
        self.assertEqual([], validate(normalize(doc), self.catalog))

    def test_non_character_cannot_walk(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["actions"][0]["actor"] = "building_1"
        self.assertIn("not_a_character", self.codes(doc))

    def test_non_character_cannot_present_a_product(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["actions"] = [
            {"actor": "building_1", "type": "present", "target": "car_1", "start": 0, "duration": 2}
        ]
        self.assertIn("not_a_character", self.codes(doc))

    def test_action_target_cannot_be_the_actor(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["actions"][0]["target"] = "character_1"
        self.assertIn("self_reference", self.codes(doc))

    def test_action_past_end_of_shot_warns_but_does_not_block(self) -> None:
        doc = normalize(copy.deepcopy(self.doc))
        doc["actions"][0]["duration"] = 60
        issues = validate(doc, self.catalog)
        self.assertEqual([], errors(issues))
        self.assertIn("action_exceeds_shot", {i.code for i in warnings(issues)})

    def test_bad_enum_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["environment"]["time_of_day"] = "midnightish"
        self.assertIn("bad_enum", self.codes(doc))

    def test_zero_duration_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["render"]["duration"] = 0
        self.assertIn("out_of_range", self.codes(doc))

    def test_follow_camera_requires_a_target(self) -> None:
        doc = copy.deepcopy(self.doc)
        del doc["camera"]["target"]
        self.assertIn("missing", self.codes(doc))

    def test_missing_required_sections_are_reported_together(self) -> None:
        codes = self.codes({"kentsugi_version": "0.1.0"})
        self.assertIn("missing", codes)

    def test_unknown_fields_are_rejected_at_every_level(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["surprise"] = True
        doc["objects"][0]["surprise"] = True
        issues = validate(normalize(doc), self.catalog)
        paths = {i.path for i in issues if i.code == "unknown_field"}
        self.assertIn("surprise", paths)
        self.assertIn("objects[0].surprise", paths)

    def test_schema_collection_limits_are_enforced(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"] = [copy.deepcopy(doc["objects"][0]) for _ in range(65)]
        self.assertIn("too_many_items", self.codes(doc))

    def test_overlapping_walks_for_one_actor_are_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        second = copy.deepcopy(doc["actions"][0])
        second["start"] = 2
        doc["actions"].append(second)
        self.assertIn("overlapping_action", self.codes(doc))


class SchemaAgreementTests(unittest.TestCase):
    """The JSON Schema is what the model sees; the validator is what we enforce.
    If they disagree the model gets told one thing and judged by another."""

    def setUp(self) -> None:
        self.schema = json.loads(
            (REPO_ROOT / "schema" / "scene.schema.json").read_text(encoding="utf-8")
        )

    def test_enums_match_the_validator(self) -> None:
        from kentsugi import validate as V

        defs = self.schema["$defs"]
        env = self.schema["properties"]["environment"]["properties"]

        self.assertEqual(list(V.PRESETS), env["preset"]["enum"])
        self.assertEqual(list(V.TIMES_OF_DAY), env["time_of_day"]["enum"])
        self.assertEqual(list(V.WEATHERS), env["weather"]["enum"])
        self.assertEqual(list(V.GROUND_MATERIALS), env["ground_material"]["enum"])
        self.assertEqual(list(V.ACTION_TYPES), defs["action"]["properties"]["type"]["enum"])
        self.assertEqual(list(V.CAMERA_TYPES), defs["camera"]["properties"]["type"]["enum"])
        self.assertEqual(list(V.CAMERA_POSITIONS), defs["camera"]["properties"]["position"]["enum"])
        self.assertEqual(list(V.SHOTS), defs["camera"]["properties"]["shot"]["enum"])
        self.assertEqual(list(V.QUALITIES), defs["render"]["properties"]["quality"]["enum"])

        relative = defs["placement"]["oneOf"][1]["properties"]["direction"]["enum"]
        self.assertEqual(list(V.DIRECTIONS), relative)

    def test_defaults_match_the_validator(self) -> None:
        from kentsugi import document as D

        defs = self.schema["$defs"]
        self.assertEqual(D._ACTION_DEFAULTS["duration"], defs["action"]["properties"]["duration"]["default"])
        self.assertEqual(D._ACTION_DEFAULTS["start"], defs["action"]["properties"]["start"]["default"])
        self.assertEqual(
            D._ACTION_DEFAULTS["stop_distance"], defs["action"]["properties"]["stop_distance"]["default"]
        )
        self.assertEqual(D._CAMERA_DEFAULTS["distance"], defs["camera"]["properties"]["distance"]["default"])
        self.assertEqual(D._CAMERA_DEFAULTS["height"], defs["camera"]["properties"]["height"]["default"])
        self.assertEqual(D._CAMERA_DEFAULTS["shot"], defs["camera"]["properties"]["shot"]["default"])
        self.assertEqual(D._RENDER_DEFAULTS["fps"], defs["render"]["properties"]["fps"]["default"])
        self.assertEqual(D._RENDER_DEFAULTS["quality"], defs["render"]["properties"]["quality"]["default"])


if __name__ == "__main__":
    unittest.main()

