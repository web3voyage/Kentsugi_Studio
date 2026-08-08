"""Validation for declarative, safe asset-construction blueprints.

Blueprints are data, not Blender Python.  The Blender Asset Forge consumes the
small operation vocabulary below and refuses unknown operations or paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .appearance import resolve_color
from .validate import ID_RE, Issue

BLUEPRINT_VERSION = "0.1.0"
PRIMITIVES = ("box", "cylinder", "sphere", "torus")
MATERIAL_FIELDS = ("base_color", "roughness", "metallic", "emission_color", "emission_strength")


def load_blueprint(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("asset blueprint top level must be an object")
    return data


def validate_blueprint(blueprint: Any) -> list[Issue]:
    out: list[Issue] = []
    if not isinstance(blueprint, dict):
        return [Issue("error", "", "bad_type", "blueprint must be an object")]
    if blueprint.get("blueprint_version") != BLUEPRINT_VERSION:
        out.append(Issue("error", "blueprint_version", "bad_version", f"expected {BLUEPRINT_VERSION!r}"))
    for field in ("id", "name", "kind", "size", "radius", "operations"):
        if field not in blueprint:
            out.append(Issue("error", field, "missing", f"{field} is required"))
    asset_id = blueprint.get("id")
    if not isinstance(asset_id, str) or not ID_RE.match(asset_id):
        out.append(Issue("error", "id", "bad_id", "id must be lowercase snake_case (max 48 chars)"))
    size = blueprint.get("size")
    if not isinstance(size, list) or len(size) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 for v in size):
        out.append(Issue("error", "size", "bad_type", "size must be three positive numbers"))
    radius = blueprint.get("radius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
        out.append(Issue("error", "radius", "bad_type", "radius must be a positive number"))
    operations = blueprint.get("operations")
    if not isinstance(operations, list) or not operations:
        out.append(Issue("error", "operations", "bad_type", "operations must be a non-empty array"))
        return out
    for i, op in enumerate(operations):
        path = f"operations[{i}]"
        if not isinstance(op, dict):
            out.append(Issue("error", path, "bad_type", "operation must be an object"))
            continue
        if op.get("primitive") not in PRIMITIVES:
            out.append(Issue("error", f"{path}.primitive", "bad_enum", f"expected one of {list(PRIMITIVES)}"))
        for field in ("location", "scale"):
            if field in op:
                value = op[field]
                if not isinstance(value, list) or len(value) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
                    out.append(Issue("error", f"{path}.{field}", "bad_type", f"{field} must be three numbers"))
        if "dimensions" in op:
            value = op["dimensions"]
            if not isinstance(value, list) or len(value) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 for v in value):
                out.append(Issue("error", f"{path}.dimensions", "bad_type", "dimensions must be three positive numbers"))
        if "material" in op:
            material = op["material"]
            if not isinstance(material, dict):
                out.append(Issue("error", f"{path}.material", "bad_type", "material must be an object"))
            else:
                for field in material:
                    if field not in MATERIAL_FIELDS:
                        out.append(Issue("error", f"{path}.material.{field}", "unknown_field", f"unsupported material field {field!r}"))
                for field in ("base_color", "emission_color"):
                    if field not in material:
                        continue
                    try:
                        resolve_color(material[field])
                    except ValueError as exc:
                        out.append(Issue("error", f"{path}.material.{field}", "bad_color", str(exc)))
                for field in ("metallic", "roughness"):
                    if field in material:
                        value = material[field]
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                            out.append(Issue("error", f"{path}.material.{field}", "out_of_range", f"{field} must be between 0 and 1"))
                if "emission_strength" in material:
                    value = material["emission_strength"]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10_000:
                        out.append(Issue("error", f"{path}.material.emission_strength", "out_of_range", "emission_strength must be between 0 and 10000"))
    return out


def save_blueprint(blueprint: dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(blueprint, indent=2) + "\n", encoding="utf-8")
