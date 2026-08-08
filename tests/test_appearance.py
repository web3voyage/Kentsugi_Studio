from __future__ import annotations

import copy
import unittest
from pathlib import Path

from kentsugi.appearance import resolve_color
from kentsugi.catalog import load_catalog
from kentsugi.document import load_document, normalize
from kentsugi.layout import solve
from kentsugi.preview import render_svg
from kentsugi.timeline import bake
from kentsugi.validate import validate


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "rainy_city.json"


class ColorParsingTests(unittest.TestCase):
    def test_named_color_is_resolved(self) -> None:
        self.assertEqual((0.0, 0.0, 0.5, 1.0), resolve_color("navy"))

    def test_names_accept_spaces_and_case(self) -> None:
        self.assertEqual(resolve_color("dark_red"), resolve_color("Dark Red"))

    def test_hex_color_is_resolved(self) -> None:
        color = resolve_color("#ff0080")
        self.assertEqual((1.0, 0.0, 1.0), (color[0], color[1], color[3]))
        self.assertAlmostEqual(128 / 255, color[2])

    def test_rgb_gets_an_opaque_alpha(self) -> None:
        self.assertEqual((0.1, 0.2, 0.3, 1.0), resolve_color([0.1, 0.2, 0.3]))

    def test_unknown_color_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_color("invisible_plaid")


class SceneAppearanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.doc = load_document(EXAMPLE)

    def test_named_object_color_validates_and_bakes(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][1]["appearance"] = {
            "color": "navy",
            "metallic": 0.8,
            "roughness": 0.2,
        }
        doc = normalize(doc)
        self.assertEqual([], validate(doc, self.catalog))
        plan = bake(doc, self.catalog, solve(doc, self.catalog))
        car = next(track for track in plan["objects"] if track["id"] == "car_1")
        self.assertEqual([0.0, 0.0, 0.5, 1.0], car["color"])
        self.assertEqual(0.8, car["appearance"]["metallic"])
        self.assertIn("#000080", render_svg(plan))

    def test_unknown_named_color_is_rejected(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][0]["appearance"] = {"color": "invisible_plaid"}
        codes = {issue.code for issue in validate(normalize(doc), self.catalog)}
        self.assertIn("bad_color", codes)

    def test_material_values_are_bounded(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["objects"][0]["appearance"] = {"roughness": 1.5}
        codes = {issue.code for issue in validate(normalize(doc), self.catalog)}
        self.assertIn("out_of_range", codes)

    def test_catalog_color_remains_the_default(self) -> None:
        plan = bake(self.doc, self.catalog, solve(self.doc, self.catalog))
        car = next(track for track in plan["objects"] if track["id"] == "car_1")
        self.assertEqual(list(self.catalog.require("red_car").color), car["color"])
        self.assertIsNone(car["appearance"])


if __name__ == "__main__":
    unittest.main()
