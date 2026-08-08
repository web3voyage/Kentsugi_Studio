from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from kentsugi.asset_blueprint import load_blueprint, validate_blueprint


ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = ROOT / "assets" / "blueprints" / "soda_can.json"


class BlueprintTests(unittest.TestCase):
    def test_soda_can_blueprint_is_valid(self) -> None:
        blueprint = load_blueprint(BLUEPRINT)
        self.assertEqual([], validate_blueprint(blueprint))

    def test_unknown_primitive_is_rejected(self) -> None:
        blueprint = load_blueprint(BLUEPRINT)
        blueprint["operations"][0]["primitive"] = "python"
        issues = validate_blueprint(blueprint)
        self.assertIn("bad_enum", {issue.code for issue in issues})

    def test_blueprint_validation_does_not_mutate(self) -> None:
        blueprint = load_blueprint(BLUEPRINT)
        original = json.dumps(blueprint, sort_keys=True)
        validate_blueprint(copy.deepcopy(blueprint))
        self.assertEqual(original, json.dumps(blueprint, sort_keys=True))

    def test_named_material_color_is_valid(self) -> None:
        blueprint = load_blueprint(BLUEPRINT)
        blueprint["operations"][0]["material"]["base_color"] = "crimson"
        self.assertEqual([], validate_blueprint(blueprint))

    def test_bad_material_color_is_rejected(self) -> None:
        blueprint = load_blueprint(BLUEPRINT)
        blueprint["operations"][0]["material"]["base_color"] = "invisible_plaid"
        self.assertIn("bad_color", {issue.code for issue in validate_blueprint(blueprint)})


if __name__ == "__main__":
    unittest.main()
