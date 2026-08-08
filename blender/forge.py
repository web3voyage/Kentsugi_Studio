"""Compile a declarative asset blueprint into a reusable Blender file.

This is the only generator entry point.  It accepts JSON data and a fixed,
small primitive vocabulary; it never evaluates generated Python or opens an
arbitrary path from the blueprint.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kentsugi.appearance import resolve_color

PRIMITIVES = {"box", "cylinder", "sphere", "torus"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(prog="kentsugi-forge")
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def material(name: str, spec: dict) -> object:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        color = resolve_color(spec.get("base_color", [0.6, 0.6, 0.6, 1.0]))
        bsdf.inputs["Base Color"].default_value = color
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = float(spec.get("metallic", 0.0))
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = float(spec.get("roughness", 0.5))
        if "Emission Color" in bsdf.inputs and "emission_color" in spec:
            bsdf.inputs["Emission Color"].default_value = resolve_color(spec["emission_color"])
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = float(spec.get("emission_strength", 0.0))
    return mat


def add_primitive(op: dict, index: int) -> object:
    primitive = op["primitive"]
    if primitive not in PRIMITIVES:
        raise ValueError(f"unsupported primitive {primitive!r}")
    location = tuple(float(v) for v in op.get("location", [0, 0, 0]))
    dimensions = tuple(float(v) for v in op.get("dimensions", [1, 1, 1]))
    if primitive == "box":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
        obj = bpy.context.object
        obj.dimensions = dimensions
    elif primitive == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=0.5, depth=1.0, location=location)
        obj = bpy.context.object
        obj.dimensions = dimensions
    elif primitive == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.5, location=location)
        obj = bpy.context.object
        obj.dimensions = dimensions
    else:
        # Start from a known torus size, then resize to the blueprint envelope.
        bpy.ops.mesh.primitive_torus_add(major_radius=0.4, minor_radius=0.1, major_segments=64, minor_segments=16, location=location)
        obj = bpy.context.object
        base = tuple(float(v) for v in obj.dimensions)
        obj.scale = tuple(dimensions[i] / base[i] for i in range(3))
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.name = f"part_{index:03d}_{primitive}"
    spec = op.get("material") or {}
    obj.data.materials.append(material(f"mat_{index:03d}", spec))
    bevel = obj.modifiers.new("Soft edges", "BEVEL")
    bevel.width = min(dimensions) * 0.035
    bevel.segments = 2
    return obj


def main() -> int:
    args = parse_args(sys.argv)
    blueprint_path = Path(args.blueprint).resolve()
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8-sig"))
    if blueprint.get("blueprint_version") != "0.1.0":
        raise ValueError("unsupported blueprint version")
    operations = blueprint.get("operations", [])
    if not isinstance(operations, list) or not operations:
        raise ValueError("blueprint operations must be a non-empty array")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    asset_collection = bpy.data.collections.new("KentsugiAsset")
    bpy.context.scene.collection.children.link(asset_collection)
    root = bpy.data.objects.new("Asset", None)
    asset_collection.objects.link(root)
    parts = [add_primitive(op, i) for i, op in enumerate(operations)]
    for part in parts:
        for collection in list(part.users_collection):
            collection.objects.unlink(part)
        asset_collection.objects.link(part)
        part.parent = root
    root["kentsugi_asset_id"] = blueprint["id"]
    root["kentsugi_blueprint_version"] = blueprint["blueprint_version"]
    root["kentsugi_size"] = list(blueprint["size"])
    root["kentsugi_pivot"] = blueprint.get("pivot", "base_center")
    root["kentsugi_radius"] = float(blueprint["radius"])
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    print(f"KENTSUGI_FORGE_OK {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Blender otherwise exits 0 after script errors.
        print(f"kentsugi forge error: {exc}", file=sys.stderr)
        raise SystemExit(1)
