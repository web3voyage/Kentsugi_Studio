"""Extract a declarative radial asset recipe from one Blender mesh.

Run with the source blend already open:

    blender --background source.blend --python blender/extract_recipe.py -- \
        --object SodaCan --output soda_can.recipe.json

The output is data, never executable code. Dense source textures stay packed in
the curated blend; the recipe records their channels and a fallback palette.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]
from mathutils import Vector  # type: ignore[import-not-found]


def parse_args(argv: list[str]) -> argparse.Namespace:
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(prog="kentsugi-extract-recipe")
    parser.add_argument("--object", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bins", type=int, default=36)
    parser.add_argument("--license", default="unknown")
    return parser.parse_args(argv)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def radial_profile(obj, bins: int) -> tuple[list[list[float]], list[float]]:
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    minimum = Vector((min(v.x for v in points), min(v.y for v in points), min(v.z for v in points)))
    maximum = Vector((max(v.x for v in points), max(v.y for v in points), max(v.z for v in points)))
    center_x = (minimum.x + maximum.x) * 0.5
    center_y = (minimum.y + maximum.y) * 0.5
    height = maximum.z - minimum.z
    if height <= 1e-9:
        raise ValueError(f"object {obj.name!r} has no measurable height")

    buckets: list[list[float]] = [[] for _ in range(bins)]
    for point in points:
        normalized = (point.z - minimum.z) / height
        bucket = min(bins - 1, max(0, int(normalized * bins)))
        buckets[bucket].append(math.hypot(point.x - center_x, point.y - center_y))

    observed: list[float | None] = [percentile(values, 0.96) if values else None for values in buckets]
    for i, radius in enumerate(observed):
        if radius is not None:
            continue
        before = next((observed[j] for j in range(i - 1, -1, -1) if observed[j] is not None), None)
        after = next((observed[j] for j in range(i + 1, bins) if observed[j] is not None), None)
        observed[i] = float(before if after is None else after if before is None else (before + after) * 0.5)

    profile = [[0.0, round(float(observed[0]), 6)]]
    profile.extend(
        [round((i + 0.5) * height / bins, 6), round(float(radius), 6)]
        for i, radius in enumerate(observed)
    )
    profile.append([round(height, 6), round(float(observed[-1]), 6)])
    bounds = [round(maximum.x - minimum.x, 6), round(maximum.y - minimum.y, 6), round(height, 6)]
    return profile, bounds


def texture_role(name: str) -> str:
    lowered = name.casefold()
    for role in ("basecolor", "roughness", "metallic", "normal", "height"):
        if role in lowered:
            return {"basecolor": "base_color"}.get(role, role)
    return "unclassified"


def sampled_palette(image, grid: int = 20) -> list[list[float]]:
    width, height = image.size
    if width <= 0 or height <= 0:
        return []
    histogram: dict[tuple[int, int, int], int] = {}
    pixels = image.pixels
    for y in range(grid):
        py = min(height - 1, round((y + 0.5) * height / grid))
        for x in range(grid):
            px = min(width - 1, round((x + 0.5) * width / grid))
            index = (py * width + px) * 4
            rgba = pixels[index:index + 4]
            if len(rgba) < 4 or rgba[3] < 0.25:
                continue
            key = tuple(min(7, max(0, round(float(channel) * 7))) for channel in rgba[:3])
            histogram[key] = histogram.get(key, 0) + 1
    ranked = sorted(histogram.items(), key=lambda item: item[1], reverse=True)[:6]
    return [[round(channel / 7, 3) for channel in key] for key, _ in ranked]


def uv_mapping_recipe(obj, bins: int = 24) -> dict | None:
    """Summarize a source mesh's cylindrical UV layout without copying its mesh."""
    layer = obj.data.uv_layers.active
    if layer is None:
        return None
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    minimum = Vector((min(v.x for v in points), min(v.y for v in points), min(v.z for v in points)))
    maximum = Vector((max(v.x for v in points), max(v.y for v in points), max(v.z for v in points)))
    center_x = (minimum.x + maximum.x) * 0.5
    center_y = (minimum.y + maximum.y) * 0.5
    height = maximum.z - minimum.z
    if height <= 1e-9:
        return None

    samples: list[tuple[float, float, float, float]] = []
    normal_matrix = obj.matrix_world.to_3x3()
    for polygon in obj.data.polygons:
        normal = (normal_matrix @ polygon.normal).normalized()
        if abs(normal.z) > 0.75:
            continue
        for loop_index in polygon.loop_indices:
            vertex = points[obj.data.loops[loop_index].vertex_index]
            radius = math.hypot(vertex.x - center_x, vertex.y - center_y)
            if radius < max(maximum.x - minimum.x, maximum.y - minimum.y) * 0.28:
                continue
            theta = (math.atan2(vertex.y - center_y, vertex.x - center_x) / (2.0 * math.pi)) % 1.0
            z = (vertex.z - minimum.z) / height
            uv = layer.data[loop_index].uv
            samples.append((theta, z, float(uv.x), float(uv.y)))
    if not samples:
        return None

    best_sign, best_offset, best_strength = 1, 0.0, -1.0
    for sign in (1, -1):
        angles = [2.0 * math.pi * ((u - sign * theta) % 1.0) for theta, _, u, _ in samples]
        x = sum(math.cos(angle) for angle in angles)
        y = sum(math.sin(angle) for angle in angles)
        strength = math.hypot(x, y) / len(angles)
        if strength > best_strength:
            best_sign = sign
            best_offset = (math.atan2(y, x) / (2.0 * math.pi)) % 1.0
            best_strength = strength

    buckets: list[list[float]] = [[] for _ in range(bins)]
    for _, z, _, v in samples:
        buckets[min(bins - 1, max(0, int(z * bins)))].append(v)
    v_profile = [
        [round((index + 0.5) / bins, 6), round(float(statistics.median(values)), 6)]
        for index, values in enumerate(buckets)
        if values
    ]
    return {
        "type": "cylindrical",
        "u_direction": best_sign,
        "u_offset": round(best_offset, 6),
        "fit_confidence": round(best_strength, 4),
        "v_profile": v_profile,
    }


def material_recipe(obj) -> dict:
    material = next((slot.material for slot in obj.material_slots if slot.material is not None), None)
    if material is None:
        return {"fallback": {"base_color": [0.6, 0.6, 0.6], "metallic": 0.7, "roughness": 0.3}}

    fallback = {"base_color": [0.6, 0.6, 0.6], "metallic": 0.7, "roughness": 0.3}
    tree = material.node_tree
    if tree is not None:
        bsdf = tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            color = bsdf.inputs["Base Color"].default_value
            fallback["base_color"] = [round(float(v), 4) for v in color[:3]]
            fallback["metallic"] = round(float(bsdf.inputs["Metallic"].default_value), 4)
            fallback["roughness"] = round(float(bsdf.inputs["Roughness"].default_value), 4)

    images = []
    palette: list[list[float]] = []
    if tree is not None:
        for node in tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            image = node.image
            role = texture_role(image.name)
            images.append(
                {
                    "role": role,
                    "image": image.name,
                    "size": [int(image.size[0]), int(image.size[1])],
                    "packed": bool(image.packed_file),
                    "color_space": image.colorspace_settings.name,
                }
            )
            if role == "base_color" and not palette:
                palette = sampled_palette(image)
    return {
        "source_material": material.name,
        "fallback": fallback,
        "palette": palette,
        "textures": sorted(images, key=lambda item: item["role"]),
    }


def main() -> int:
    args = parse_args(sys.argv)
    obj = bpy.data.objects.get(args.object)
    if obj is None or obj.type != "MESH":
        raise ValueError(f"{args.object!r} is not a mesh object")
    profile, bounds = radial_profile(obj, max(12, min(128, args.bins)))
    source_blend = str(Path(bpy.data.filepath).resolve()).replace("\\", "/")
    recipe = {
        "recipe_version": "1.0",
        "id": "sampled_soda_can",
        "kind": "product",
        "units": "metres",
        "license": args.license,
        "source_evidence": {"blend": source_blend, "object": obj.name},
        "bounds": bounds,
        "construction": {
            "type": "radial_lathe",
            "axis": "z",
            "segments": 64,
            "profile": profile,
            "details": [
                {"type": "lid", "z": round(bounds[2] * 0.985, 6), "radius": round(max(bounds[:2]) * 0.45, 6)},
                {"type": "pull_tab", "z": round(bounds[2] * 1.003, 6), "length": round(max(bounds[:2]) * 0.31, 6)},
            ],
        },
        "material": material_recipe(obj),
    }
    uv_mapping = uv_mapping_recipe(obj)
    if uv_mapping is not None:
        recipe["material"]["uv_mapping"] = uv_mapping
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    print(f"kentsugi: wrote recipe {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

