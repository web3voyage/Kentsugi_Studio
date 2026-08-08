"""Object engine architecture tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kentsugi.blueprints import Blueprint, BlueprintRegistry, load_blueprints
from kentsugi.catalog import load_catalog
from kentsugi.knowledge import KnowledgeLibrary
from kentsugi.object_engine import ObjectEngine, artifact_checks
from kentsugi.object_spec import plan_object_spec, validate_object_spec


class ObjectSpecTests(unittest.TestCase):
    def test_energy_prompt_becomes_semantic_spec(self) -> None:
        spec = plan_object_spec("Create a black and green Monster energy drink can")
        self.assertEqual([], validate_object_spec(spec))
        self.assertEqual("can", spec["family"])
        self.assertEqual("tall_energy", spec["variant"])
        self.assertEqual(["black", "green"], spec["attributes"]["colors"])
        self.assertNotIn("path", str(spec).casefold())

    def test_runtime_contract_matches_schema_top_level(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parent.parent / "schema" / "object-spec.schema.json").read_text(encoding="utf-8")
        )
        spec = plan_object_spec("Create a red car")
        self.assertEqual(set(schema["required"]), set(spec))
        self.assertEqual(set(schema["properties"]), set(spec))


class BlueprintSearchTests(unittest.TestCase):
    def test_exact_family_and_variant_win(self) -> None:
        spec = plan_object_spec("Create a Monster energy can")
        matches = load_blueprints().search(spec)
        self.assertEqual("can_tall_energy_v1", matches[0].blueprint.id)
        self.assertIn("family", matches[0].reasons)
        self.assertIn("variant", matches[0].reasons)

    def test_approved_history_boosts_retrieval(self) -> None:
        spec = plan_object_spec("Create a generic object")
        matches = load_blueprints().search(spec, {"prop_generic_v1": 4})
        self.assertEqual("prop_generic_v1", matches[0].blueprint.id)
        self.assertIn("approved:4", matches[0].reasons)


class EngineLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.knowledge = KnowledgeLibrary(
            self.temporary.name,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_engine_compiles_checked_scene(self) -> None:
        run = ObjectEngine(knowledge=self.knowledge).prepare("Create a normal red soda can")
        self.assertEqual("can_standard_v1", run.blueprint.id)
        self.assertFalse(any(check.severity == "error" for check in run.checks))
        self.assertEqual("standard_soda_can", run.scene["objects"][0]["asset"])
        self.assertEqual(180, run.scene["objects"][0]["facing"])
        self.assertIn("framing_margin", {check.code for check in run.checks})

    def test_failed_blueprint_advances_to_next_candidate(self) -> None:
        bad = Blueprint(
            "bad_can", "can", "tall_energy", "product", "missing_asset",
            frozenset({"energy"}), frozenset(), "test", 1,
        )
        good = load_blueprints().require("can_tall_energy_v1")
        registry = BlueprintRegistry([bad, good])
        run = ObjectEngine(registry=registry, knowledge=self.knowledge).prepare(
            "Create a Monster energy can", max_attempts=2
        )
        self.assertEqual("can_tall_energy_v1", run.blueprint.id)
        self.assertEqual(2, run.attempts)

    def test_approved_render_enters_versioned_knowledge(self) -> None:
        run = ObjectEngine(knowledge=self.knowledge).prepare("Create a Monster energy can")
        artifact = Path(self.temporary.name) / "render.png"
        artifact.write_bytes(b"\x89PNG\r\n\x1a\nproof")
        record = ObjectEngine(knowledge=self.knowledge).finalize(run, artifact=artifact, approved=True)
        self.assertEqual("v0001.json", record.name)
        self.assertEqual({"can_tall_energy_v1": 1}, self.knowledge.approval_counts())

    def test_unrendered_result_cannot_be_approved(self) -> None:
        run = ObjectEngine(knowledge=self.knowledge).prepare("Create a soda can")
        with self.assertRaisesRegex(ValueError, "before a rendered artifact"):
            ObjectEngine(knowledge=self.knowledge).finalize(run, approved=True)

    def test_missing_required_rig_blocks_generation(self) -> None:
        with self.assertRaisesRegex(ValueError, "no blueprint passed checks"):
            ObjectEngine(knowledge=self.knowledge).prepare("Create an animated Bugatti driving")

    def test_artifact_check_rejects_wrong_signature(self) -> None:
        artifact = Path(self.temporary.name) / "fake.png"
        artifact.write_bytes(b"not an image")
        self.assertEqual("bad_artifact", artifact_checks(artifact)[0].code)


if __name__ == "__main__":
    unittest.main()

