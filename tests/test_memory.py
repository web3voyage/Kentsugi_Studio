from __future__ import annotations

import copy
import tempfile
import unittest

from kentsugi.asset_blueprint import load_blueprint
from kentsugi.memory import MemoryEvent, MemoryStore, propose_skill


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.blueprint = load_blueprint("assets/blueprints/soda_can.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_events_persist_and_reload(self) -> None:
        self.store.record(MemoryEvent("forge", "red soda can", "success", {"asset": "soda_can"}))
        restored = MemoryStore(self.tmp.name)
        events = restored.events()
        self.assertEqual(1, len(events))
        self.assertEqual("success", events[0].outcome)

    def test_skill_is_retrievable_and_feedback_changes_score(self) -> None:
        skill = propose_skill(self.store, skill_id="soda_can_v1", name="Soda can", description="procedural cylindrical drink can", blueprint=copy.deepcopy(self.blueprint), tags=["can", "drink"])
        self.assertEqual("candidate", skill.source)
        self.assertEqual(["soda_can_v1"], [item.id for item in self.store.retrieve("make a drink can")])
        before = skill.score
        updated = self.store.feedback(skill.id, True, task="make a drink can")
        self.assertIsNotNone(updated)
        self.assertGreater(updated.score, before)

    def test_invalid_skill_never_enters_store(self) -> None:
        bad = copy.deepcopy(self.blueprint)
        bad["operations"][0]["primitive"] = "execute_python"
        with self.assertRaises(ValueError):
            propose_skill(self.store, skill_id="bad", name="Bad", description="unsafe", blueprint=bad)
        self.assertEqual([], self.store.skills())

    def test_promotion_is_explicit(self) -> None:
        skill = propose_skill(self.store, skill_id="soda_can_v1", name="Soda can", description="can", blueprint=self.blueprint)
        self.assertEqual("candidate", skill.source)
        promoted = self.store.promote(skill.id)
        self.assertIsNotNone(promoted)
        self.assertEqual("promoted", promoted.source)


if __name__ == "__main__":
    unittest.main()
