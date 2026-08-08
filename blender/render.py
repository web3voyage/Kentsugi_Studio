"""Blender applier — the only file in this project that imports bpy.

Run headless:

    blender --background --factory-startup --python blender/render.py -- \
        --plan .kentsugi/plan.json --output out.mp4

It consumes a baked plan and does no scene reasoning of its own: positions,
yaws, and camera keys are already resolved. Everything here is translation.
That keeps the interesting logic in testable CPython and makes a second
backend (Godot, Unity) a matter of reimplementing this file alone.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import bpy  # type: ignore[import-not-found]
from mathutils import Vector  # type: ignore[import-not-found]

VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".avi", ".webm")

GROUND_MATERIALS = {
    "asphalt": {"color": (0.045, 0.047, 0.05, 1.0), "roughness": 0.72},
    "concrete": {"color": (0.20, 0.20, 0.19, 1.0), "roughness": 0.85},
    "grass": {"color": (0.055, 0.13, 0.045, 1.0), "roughness": 0.92},
    "wood": {"color": (0.16, 0.10, 0.055, 1.0), "roughness": 0.55},
    "none": None,
}

# (sun energy, sun colour, world colour, world strength, sun elevation degrees)
TIME_OF_DAY = {
    "dawn": (2.5, (1.0, 0.78, 0.62), (0.28, 0.26, 0.34), 0.55, 12.0),
    "day": (5.0, (1.0, 0.97, 0.92), (0.35, 0.43, 0.58), 1.00, 55.0),
    "dusk": (2.0, (1.0, 0.62, 0.42), (0.20, 0.15, 0.22), 0.40, 8.0),
    "night": (0.12, (0.55, 0.65, 1.0), (0.014, 0.018, 0.032), 0.09, 35.0),
}

WEATHER_ADJUST = {
    "clear": {"sun": 1.0, "world": 1.0, "ground_roughness": 1.0, "fog": 0.0},
    "overcast": {"sun": 0.35, "world": 1.25, "ground_roughness": 1.0, "fog": 0.0},
    "rain": {"sun": 0.30, "world": 0.85, "ground_roughness": 0.25, "fog": 0.004},
    "fog": {"sun": 0.45, "world": 1.1, "ground_roughness": 1.0, "fog": 0.02},
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="kentsugi-blender")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--save-blend")
    parser.add_argument("--device", default="CPU", choices=("CPU", "GPU"))
    parser.add_argument("--still-frame", type=int)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--resolution-scale", type=int, default=100)
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def enable_nodes(datablock) -> None:
    """Turn on node-based shading across Blender versions.

    ``use_nodes`` is deprecated in 5.x (nodes are always on) but still required
    in 4.x, where a fresh datablock has no node tree. Setting it only when the
    tree is missing keeps both working without emitting deprecation warnings.
    """
    if getattr(datablock, "node_tree", None) is None:
        datablock.use_nodes = True


def action_fcurves(action) -> list:
    """Every F-curve in an action, across the 4.4 slotted-action rewrite.

    Blender 4.4 replaced ``Action.fcurves`` with layers → strips → channelbags.
    The old attribute is gone in 5.x, so both shapes have to be handled.
    """
    if action is None:
        return []

    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)

    curves: list = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for bag in getattr(strip, "channelbags", []):
                curves.extend(getattr(bag, "fcurves", []))
    return curves


def set_linear_interpolation(obj) -> None:
    """Constant speed between keys.

    Blender's default bezier easing makes a walk accelerate and decelerate,
    which reads as the character sliding on ice.
    """
    animation = getattr(obj, "animation_data", None)
    if animation is None:
        return
    for curve in action_fcurves(getattr(animation, "action", None)):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def make_material(
    name: str,
    color,
    roughness: float = 0.5,
    metallic: float = 0.0,
    emission_color=None,
    emission_strength: float = 0.0,
):
    material = bpy.data.materials.new(name)
    enable_nodes(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        rgba = tuple(color[:4])
        if len(rgba) == 3:
            rgba = (*rgba, 1.0)
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic
        if emission_color is not None:
            for socket in ("Emission Color", "Emission"):
                if socket in bsdf.inputs:
                    bsdf.inputs[socket].default_value = tuple(emission_color)
                    break
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = emission_strength
    return material


def make_emissive_material(name: str, color, power: float):
    material = bpy.data.materials.new(name)
    enable_nodes(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
        # Socket name differs across versions; try both before giving up.
        for socket in ("Emission Color", "Emission"):
            if socket in bsdf.inputs:
                bsdf.inputs[socket].default_value = (color[0], color[1], color[2], 1.0)
                break
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = max(1.0, power / 100.0)
    return material


def setup_world(environment: dict) -> None:
    time_of_day = environment.get("time_of_day", "day")
    weather = environment.get("weather", "clear")
    sun_energy, sun_color, world_color, world_strength, elevation = TIME_OF_DAY.get(
        time_of_day, TIME_OF_DAY["day"]
    )
    adjust = WEATHER_ADJUST.get(weather, WEATHER_ADJUST["clear"])
    studio = environment.get("preset") == "studio"
    if studio:
        sun_energy *= 0.08
        world_color = (0.018, 0.022, 0.035)
        world_strength = 0.24

    world = bpy.data.worlds.new("KentsugiWorld")
    enable_nodes(world)
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (*world_color, 1.0)
        background.inputs["Strength"].default_value = world_strength * adjust["world"]
    bpy.context.scene.world = world

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = sun_energy * adjust["sun"]
    sun_data.color = sun_color
    sun_data.angle = math.radians(2.0 if weather == "clear" else 25.0)
    sun = bpy.data.objects.new("Sun", sun_data)
    bpy.context.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(90.0 - elevation), 0.0, math.radians(-40.0))


def add_ground(environment: dict, extent: float) -> None:
    spec = GROUND_MATERIALS.get(environment.get("ground_material", "concrete"))
    if spec is None:
        return
    bpy.ops.mesh.primitive_plane_add(size=max(40.0, extent * 3.0), location=(0.0, 0.0, 0.0))
    ground = bpy.context.active_object
    ground.name = "Ground"
    adjust = WEATHER_ADJUST.get(environment.get("weather", "clear"), WEATHER_ADJUST["clear"])
    roughness = spec["roughness"] * adjust["ground_roughness"]
    color = spec["color"]
    if environment.get("preset") == "studio":
        color = (0.035, 0.042, 0.065, 1.0)
        roughness = 0.22
    ground.data.materials.append(make_material("Ground", color, roughness=roughness, metallic=0.08))


def add_area_light(name: str, location, target, energy: float, color, size: float) -> None:
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.color = color
    data.shape = "DISK"
    data.size = size
    light = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(light)
    light.location = location
    direction = Vector(target) - light.location
    light.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def add_studio_set() -> None:
    """A compact premium-ad stage with a dark cove and three-point lighting."""
    wall_material = make_material("StudioCove", (0.018, 0.024, 0.045), roughness=0.32)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, -3.1, 3.0))
    wall = bpy.context.active_object
    wall.name = "StudioCove"
    wall.scale = (7.0, 0.12, 3.0)
    wall.data.materials.append(wall_material)

    accent = make_emissive_material("StudioAccent", (0.09, 0.32, 0.72), 900.0)
    for x in (-3.1, 3.1):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, -2.94, 2.4))
        panel = bpy.context.active_object
        panel.scale = (0.055, 0.035, 1.55)
        panel.data.materials.append(accent)

    add_area_light("Key", (3.8, 3.5, 5.2), (0.0, 0.0, 1.0), 1050.0, (1.0, 0.72, 0.48), 3.2)
    add_area_light("Fill", (-4.0, 2.0, 3.2), (0.0, 0.0, 1.0), 720.0, (0.36, 0.55, 1.0), 3.8)
    add_area_light("Rim", (0.0, -2.3, 4.8), (0.0, 0.0, 1.2), 1250.0, (0.24, 0.46, 1.0), 2.4)


def build_shape(spec: dict, size, material) -> list:
    """Create the meshes for one object, positioned with the base at z=0."""
    shape = spec.get("shape", "box")
    width, depth, height = size
    parts = []

    if shape == "capsule":
        radius = max(0.05, width / 2.0)
        body = max(0.05, height - 2.0 * radius)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=body, location=(0, 0, radius + body / 2))
        parts.append(bpy.context.active_object)
        for z in (radius, radius + body):
            bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=(0, 0, z))
            parts.append(bpy.context.active_object)
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(radius=max(0.02, width / 2.0), depth=height, location=(0, 0, height / 2))
        parts.append(bpy.context.active_object)
    else:  # box
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, height / 2))
        cube = bpy.context.active_object
        cube.scale = (width, depth, height)
        parts.append(cube)

    for part in parts:
        part.data.materials.append(material)
        for polygon in part.data.polygons:
            polygon.use_smooth = shape != "box"
    return parts


def add_part(name: str, shape: str, parent, location, scale, material):
    """Create one local-space character part under an object rig."""
    if shape == "cube":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=1.0, depth=2.0, location=(0.0, 0.0, 0.0))
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(location=(0.0, 0.0, 0.0))
    part = bpy.context.active_object
    part.name = name
    part.parent = parent
    part.location = location
    part.scale = scale
    part.data.materials.append(material)
    for polygon in part.data.polygons:
        polygon.use_smooth = shape != "cube"
    return part


def add_pivot(name: str, parent, location):
    pivot = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(pivot)
    pivot.parent = parent
    pivot.location = location
    return pivot


def build_procedural_character(track: dict, root) -> dict:
    """Build the first production hero as a lightweight articulated object rig.

    The rig is deliberately made from named parts rather than one capsule. It
    renders without external files today and supplies the same root/motion
    contract that imported characters will use later.
    """
    source = track.get("source", {})
    palette = source.get("palette", {})
    appearance = track.get("appearance") or {}
    if isinstance(appearance, dict) and "color" in appearance:
        palette = {**palette, "shirt": appearance["color"]}
    height = float(track["size"][2])
    unit = height / 1.8

    def color(name: str, fallback):
        value = palette.get(name, fallback)
        return tuple(float(v) for v in value[:3])

    materials = {
        "skin": make_material(f"skin_{track['id']}", color("skin", (0.48, 0.20, 0.10)), 0.62),
        "shirt": make_material(f"shirt_{track['id']}", color("shirt", (0.04, 0.22, 0.48)), 0.48),
        "trousers": make_material(f"trousers_{track['id']}", color("trousers", (0.055, 0.065, 0.09)), 0.7),
        "hair": make_material(f"hair_{track['id']}", color("hair", (0.018, 0.009, 0.006)), 0.9),
        "shoes": make_material(f"shoes_{track['id']}", color("shoes", (0.025, 0.02, 0.018)), 0.38),
        "eyes": make_material(f"eyes_{track['id']}", (0.018, 0.012, 0.01), 0.25),
        "white": make_material(f"eye_white_{track['id']}", (0.95, 0.92, 0.86), 0.32),
    }

    rig = add_pivot(f"rig_{track['id']}", root, (0.0, 0.0, 0.0))
    add_part(f"{track['id']}_torso", "sphere", rig, (0.0, 0.0, 1.08 * unit),
             (0.29 * unit, 0.20 * unit, 0.38 * unit), materials["shirt"])
    add_part(f"{track['id']}_pelvis", "sphere", rig, (0.0, 0.0, 0.78 * unit),
             (0.255 * unit, 0.18 * unit, 0.19 * unit), materials["trousers"])
    add_part(f"{track['id']}_neck", "cylinder", rig, (0.0, 0.0, 1.36 * unit),
             (0.075 * unit, 0.075 * unit, 0.07 * unit), materials["skin"])
    add_part(f"{track['id']}_head", "sphere", rig, (0.0, 0.0, 1.57 * unit),
             (0.235 * unit, 0.215 * unit, 0.255 * unit), materials["skin"])
    add_part(f"{track['id']}_hair", "sphere", rig, (0.0, -0.005 * unit, 1.71 * unit),
             (0.24 * unit, 0.22 * unit, 0.16 * unit), materials["hair"])
    for side in (-1.0, 1.0):
        tag = "l" if side < 0 else "r"
        add_part(f"{track['id']}_ear_{tag}", "sphere", rig,
                 (side * 0.235 * unit, 0.0, 1.57 * unit),
                 (0.052 * unit, 0.035 * unit, 0.075 * unit), materials["skin"])
        add_part(f"{track['id']}_eye_white_{tag}", "sphere", rig,
                 (side * 0.082 * unit, 0.198 * unit, 1.61 * unit),
                 (0.055 * unit, 0.026 * unit, 0.061 * unit), materials["white"])
        add_part(f"{track['id']}_eye_{tag}", "sphere", rig,
                 (side * 0.082 * unit, 0.221 * unit, 1.605 * unit),
                 (0.022 * unit, 0.014 * unit, 0.027 * unit), materials["eyes"])

    add_part(f"{track['id']}_nose", "sphere", rig, (0.0, 0.224 * unit, 1.55 * unit),
             (0.037 * unit, 0.045 * unit, 0.045 * unit), materials["skin"])
    add_part(f"{track['id']}_mouth", "cube", rig, (0.0, 0.219 * unit, 1.485 * unit),
             (0.055 * unit, 0.012 * unit, 0.009 * unit), materials["eyes"])

    controls = {"rig": rig}
    for side in (-1.0, 1.0):
        tag = "l" if side < 0 else "r"
        shoulder = add_pivot(f"shoulder_{tag}_{track['id']}", rig,
                             (side * 0.31 * unit, 0.0, 1.26 * unit))
        add_part(f"{track['id']}_arm_{tag}", "cylinder", shoulder,
                 (0.0, 0.0, -0.22 * unit),
                 (0.072 * unit, 0.072 * unit, 0.22 * unit), materials["shirt"])
        add_part(f"{track['id']}_hand_{tag}", "sphere", shoulder,
                 (0.0, 0.0, -0.46 * unit),
                 (0.082 * unit, 0.072 * unit, 0.09 * unit), materials["skin"])
        controls[f"arm_{tag}"] = shoulder

        hip = add_pivot(f"hip_{tag}_{track['id']}", rig,
                        (side * 0.13 * unit, 0.0, 0.74 * unit))
        add_part(f"{track['id']}_leg_{tag}", "cylinder", hip,
                 (0.0, 0.0, -0.29 * unit),
                 (0.085 * unit, 0.085 * unit, 0.29 * unit), materials["trousers"])
        add_part(f"{track['id']}_shoe_{tag}", "sphere", hip,
                 (0.0, 0.065 * unit, -0.61 * unit),
                 (0.105 * unit, 0.16 * unit, 0.075 * unit), materials["shoes"])
        controls[f"leg_{tag}"] = hip
    return controls


def make_luxury_material(name: str, color, *, metallic: float, roughness: float, transmission: float = 0.0):
    material = make_material(name, color, roughness=roughness, metallic=metallic)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        for socket in ("Transmission Weight", "Transmission"):
            if socket in bsdf.inputs:
                bsdf.inputs[socket].default_value = transmission
                break
        if "Coat Weight" in bsdf.inputs:
            bsdf.inputs["Coat Weight"].default_value = 0.35
        if "Coat Roughness" in bsdf.inputs:
            bsdf.inputs["Coat Roughness"].default_value = 0.12
    return material


def build_procedural_product(track: dict, root) -> None:
    source = track.get("source", {})
    appearance = track.get("appearance") or {}
    height = float(track["size"][2])
    unit = height / 1.45
    glass_color = tuple(appearance.get("color", source.get("glass_color", (0.055, 0.16, 0.22))))
    accent_color = tuple(source.get("accent_color", (0.82, 0.55, 0.16)))
    glass = make_luxury_material(
        f"glass_{track['id']}", glass_color,
        metallic=float(appearance.get("metallic", 0.18)),
        roughness=float(appearance.get("roughness", 0.12)),
                                 transmission=0.18)
    gold = make_luxury_material(f"gold_{track['id']}", accent_color, metallic=0.88, roughness=0.16)
    dark = make_luxury_material(f"pedestal_{track['id']}", (0.015, 0.02, 0.035), metallic=0.28,
                                roughness=0.2)

    add_part(f"{track['id']}_pedestal", "cylinder", root, (0.0, 0.0, 0.09 * unit),
             (0.38 * unit, 0.38 * unit, 0.09 * unit), dark)
    add_part(f"{track['id']}_bottle", "cylinder", root, (0.0, 0.0, 0.68 * unit),
             (0.27 * unit, 0.27 * unit, 0.50 * unit), glass)
    add_part(f"{track['id']}_shoulder", "sphere", root, (0.0, 0.0, 1.14 * unit),
             (0.27 * unit, 0.27 * unit, 0.18 * unit), glass)
    add_part(f"{track['id']}_neck", "cylinder", root, (0.0, 0.0, 1.27 * unit),
             (0.105 * unit, 0.105 * unit, 0.13 * unit), gold)
    add_part(f"{track['id']}_cap", "cylinder", root, (0.0, 0.0, 1.40 * unit),
             (0.145 * unit, 0.145 * unit, 0.07 * unit), gold)
    add_part(f"{track['id']}_label", "cube", root, (0.0, 0.276 * unit, 0.72 * unit),
             (0.22 * unit, 0.018 * unit, 0.18 * unit), gold)

    bpy.ops.object.text_add(location=(0.0, 0.0, 0.0))
    text = bpy.context.active_object
    text.name = f"{track['id']}_brand"
    text.parent = root
    text.location = (0.0, 0.298 * unit, 0.72 * unit)
    text.rotation_euler = (math.radians(-90.0), 0.0, 0.0)
    # The camera views the product along -Y. Flipping both in-plane axes keeps
    # the front face toward +Y while making the lettering read left-to-right.
    text.scale = (-0.14 * unit, -0.14 * unit, 0.14 * unit)
    text.data.body = str(source.get("brand", "AURA"))[:12]
    text.data.align_x = "CENTER"
    text.data.align_y = "CENTER"
    text.data.extrude = 0.006
    text.data.bevel_depth = 0.002
    text.data.materials.append(dark)


def trusted_asset_path(raw: str) -> Path:
    relative = Path(raw).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    asset_root = (repo_root / "assets").resolve()
    if relative.is_absolute():
        resolved = relative.resolve()
    elif str(relative).replace("\\", "/").startswith("assets/"):
        resolved = (repo_root / relative).resolve()
    else:
        resolved = (asset_root / relative).resolve()
    try:
        resolved.relative_to(asset_root)
    except ValueError as exc:
        raise ValueError(f"asset path escapes the trusted asset root: {raw!r}") from exc
    return resolved


def apply_material_override(objects, track: dict) -> None:
    appearance = track.get("appearance") or {}
    if not isinstance(appearance, dict) or not appearance:
        return
    material = make_material(
        f"override_{track['id']}",
        appearance.get("color", track["color"]),
        roughness=float(appearance.get("roughness", 0.45)),
        metallic=float(appearance.get("metallic", 0.0)),
        emission_color=appearance.get("emission_color"),
        emission_strength=float(appearance.get("emission_strength", 0.0)),
    )
    for obj in objects:
        data = getattr(obj, "data", None)
        materials = getattr(data, "materials", None)
        if materials is None:
            continue
        if hasattr(data, "copy"):
            obj.data = data.copy()
            materials = obj.data.materials
        materials.clear()
        materials.append(material)


def build_blend_asset(track: dict, root) -> None:
    """Append one curated object from a .blend asset recipe and normalize it.

    Paths come from the operator-controlled catalog, never model output. The
    recipe names the object explicitly so cameras, lights, and unrelated scene
    data in an asset pack cannot leak into the render.
    """
    source = track.get("source", {})
    asset_path = trusted_asset_path(str(source.get("path", "")))
    if asset_path.suffix.casefold() != ".blend" or not asset_path.is_file():
        raise ValueError(f"blend asset is missing or not a .blend file: {asset_path}")

    object_name = source.get("object")
    collection_name = source.get("collection")
    if not isinstance(object_name, str) and isinstance(collection_name, str):
        with bpy.data.libraries.load(str(asset_path), link=False) as (available, requested):
            if collection_name not in available.collections:
                raise ValueError(f"collection {collection_name!r} is not present in {asset_path.name}")
            requested.collections = [collection_name]
        collection = requested.collections[0]
        apply_material_override(collection.all_objects, track)
        instance = bpy.data.objects.new(f"asset_{track['id']}", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        instance.parent = root
        bpy.context.collection.objects.link(instance)
        return
    if not isinstance(object_name, str) or not object_name:
        raise ValueError("blend asset recipe requires an object name")
    with bpy.data.libraries.load(str(asset_path), link=False) as (available, requested):
        if object_name not in available.objects:
            raise ValueError(f"object {object_name!r} is not present in {asset_path.name}")
        requested.objects = [object_name]

    imported = [obj for obj in requested.objects if obj is not None]
    if not imported:
        raise ValueError(f"could not append object {object_name!r} from {asset_path.name}")
    for obj in imported:
        bpy.context.collection.objects.link(obj)
    apply_material_override(imported, track)
    bpy.context.view_layer.update()

    bounds = [obj.matrix_world @ Vector(corner) for obj in imported for corner in obj.bound_box]
    minimum = Vector((min(v.x for v in bounds), min(v.y for v in bounds), min(v.z for v in bounds)))
    maximum = Vector((max(v.x for v in bounds), max(v.y for v in bounds), max(v.z for v in bounds)))
    raw_height = maximum.z - minimum.z
    if raw_height <= 1e-9:
        raise ValueError(f"blend asset {object_name!r} has no measurable height")

    group = add_pivot(f"asset_{track['id']}", root, (0.0, 0.0, 0.0))
    matrices = {obj: obj.matrix_world.copy() for obj in imported}
    for obj in imported:
        obj.parent = group
        obj.matrix_world = matrices[obj]
    factor = float(track["size"][2]) / raw_height
    center = (minimum + maximum) * 0.5
    group.scale = (factor, factor, factor)
    group.location = (-center.x * factor, -center.y * factor, -minimum.z * factor)


def recipe_material(recipe: dict, track: dict):
    """Load the sampled PBR material, with a palette-based offline fallback."""
    material_spec = recipe.get("material", {})
    parameters = track.get("source", {}).get("parameters", {})
    material_override = parameters.get("material", {}) if isinstance(parameters, dict) else {}
    if not isinstance(material_override, dict):
        material_override = {}
    appearance = track.get("appearance") or {}
    if isinstance(appearance, dict):
        material_override = {**material_override}
        if "color" in appearance:
            material_override["base_color"] = appearance["color"]
        for key in ("metallic", "roughness"):
            if key in appearance:
                material_override[key] = appearance[key]
    evidence = recipe.get("source_evidence", {})
    source_blend = Path(str(evidence.get("blend", ""))).expanduser()
    material_name = material_spec.get("source_material")
    use_source = not material_override and parameters.get("use_source_material", True)
    if use_source and source_blend.is_file() and isinstance(material_name, str):
        with bpy.data.libraries.load(str(source_blend), link=False) as (available, requested):
            if material_name in available.materials:
                requested.materials = [material_name]
        loaded = next((material for material in requested.materials if material is not None), None)
        if loaded is not None:
            return loaded

    fallback = material_spec.get("fallback", {})
    palette = material_spec.get("palette", [])
    colorful = [color for color in palette if isinstance(color, list) and len(color) >= 3]
    sampled_color = max(
        colorful,
        key=lambda c: max(c[:3]) - min(c[:3]),
        default=fallback.get("base_color", [0.65, 0.08, 0.06]),
    )
    base_color = material_override.get("base_color", sampled_color)
    return make_luxury_material(
        f"recipe_{track['id']}",
        tuple(float(v) for v in base_color[:3]),
        metallic=float(material_override.get("metallic", fallback.get("metallic", 0.65))),
        roughness=float(material_override.get("roughness", fallback.get("roughness", 0.28))),
    )


def sampled_curve(points: list, position: float, default: float = 1.0) -> float:
    clean = sorted(
        (float(point[0]), float(point[1]))
        for point in points
        if isinstance(point, list) and len(point) == 2
    )
    if not clean:
        return default
    if position <= clean[0][0]:
        return clean[0][1]
    for left, right in zip(clean, clean[1:]):
        if position <= right[0]:
            amount = (position - left[0]) / max(1e-9, right[0] - left[0])
            return left[1] + (right[1] - left[1]) * amount
    return clean[-1][1]


def modified_profile(profile: list, shape: dict) -> list:
    """Apply bounded, catalog-authored radius changes to a reusable profile."""
    valid = [point for point in profile if isinstance(point, list) and len(point) == 2]
    if not valid:
        return profile
    z_min = min(float(point[0]) for point in valid)
    z_max = max(float(point[0]) for point in valid)
    height = max(1e-9, z_max - z_min)
    radius_curve = shape.get("radius_curve", []) if isinstance(shape, dict) else []
    return [
        [
            float(point[0]),
            float(point[1]) * max(0.5, min(1.5, sampled_curve(radius_curve, (float(point[0]) - z_min) / height))),
        ]
        for point in valid
    ]


def build_lathe(name: str, profile: list, segments: int, parent, material, uv_mapping: dict | None = None):
    """Revolve a measured [z, radius] profile into a UV-mapped mesh."""
    clean = [
        (float(point[0]), max(0.0001, float(point[1])))
        for point in profile
        if isinstance(point, list) and len(point) == 2
    ]
    if len(clean) < 2:
        raise ValueError("radial recipe requires at least two profile points")
    segments = max(12, min(256, int(segments)))
    vertices = [
        (radius * math.cos(2.0 * math.pi * j / segments),
         radius * math.sin(2.0 * math.pi * j / segments), z)
        for z, radius in clean
        for j in range(segments)
    ]
    bottom_center = len(vertices)
    top_center = bottom_center + 1
    vertices.extend([(0.0, 0.0, clean[0][0]), (0.0, 0.0, clean[-1][0])])
    faces = []
    for i in range(len(clean) - 1):
        for j in range(segments):
            nxt = (j + 1) % segments
            faces.append((i * segments + j, i * segments + nxt, (i + 1) * segments + nxt, (i + 1) * segments + j))
    for j in range(segments):
        nxt = (j + 1) % segments
        faces.append((bottom_center, nxt, j))
        faces.append((top_center, (len(clean) - 1) * segments + j, (len(clean) - 1) * segments + nxt))

    mesh = bpy.data.meshes.new(f"mesh_{name}")
    mesh.from_pydata(vertices, [], faces)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent

    z_min, z_max = clean[0][0], clean[-1][0]
    height = max(1e-9, z_max - z_min)
    uv = mesh.uv_layers.new(name="UVMap")
    mapping = uv_mapping if isinstance(uv_mapping, dict) else {}
    u_direction = -1.0 if int(mapping.get("u_direction", 1)) < 0 else 1.0
    u_offset = float(mapping.get("u_offset", 0.0))
    v_profile = [
        (float(point[0]), float(point[1]))
        for point in mapping.get("v_profile", [])
        if isinstance(point, list) and len(point) == 2
    ]

    def mapped_v(normalized_z: float) -> float:
        if not v_profile:
            return normalized_z
        if normalized_z <= v_profile[0][0]:
            return v_profile[0][1]
        for left, right in zip(v_profile, v_profile[1:]):
            if normalized_z <= right[0]:
                span = max(1e-9, right[0] - left[0])
                amount = (normalized_z - left[0]) / span
                return left[1] + (right[1] - left[1]) * amount
        return v_profile[-1][1]

    for polygon in mesh.polygons:
        ring_vertices = [mesh.loops[index].vertex_index for index in polygon.loop_indices]
        raw_us = {
            vertex: (u_direction * ((vertex % segments) / segments) + u_offset) % 1.0
            for vertex in ring_vertices if vertex < bottom_center
        }
        seam = bool(raw_us) and max(raw_us.values()) - min(raw_us.values()) > 0.5
        for loop_index in polygon.loop_indices:
            vertex = mesh.loops[loop_index].vertex_index
            if vertex >= bottom_center:
                uv.data[loop_index].uv = (0.5, 0.5)
                continue
            ring, j = divmod(vertex, segments)
            u = raw_us[vertex]
            if seam and u < 0.5:
                u += 1.0
            normalized_z = (clean[ring][0] - z_min) / height
            v = mapped_v(normalized_z)
            uv.data[loop_index].uv = (u, v)
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    return obj


def build_recipe_asset(track: dict, root) -> None:
    source = track.get("source", {})
    parameters = source.get("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}
    recipe_path = trusted_asset_path(str(source.get("recipe", "")))
    if recipe_path.suffix.casefold() != ".json" or not recipe_path.is_file():
        raise ValueError(f"asset recipe is missing or not JSON: {recipe_path}")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8-sig"))
    construction = recipe.get("construction", {})
    if construction.get("type") != "radial_lathe":
        raise ValueError(f"unsupported recipe construction {construction.get('type')!r}")

    bounds = recipe.get("bounds", [])
    if not isinstance(bounds, list) or len(bounds) != 3 or float(bounds[2]) <= 0:
        raise ValueError("asset recipe requires positive three-dimensional bounds")
    material = recipe_material(recipe, track)
    group = add_pivot(f"recipe_{track['id']}", root, (0.0, 0.0, 0.0))
    profile = modified_profile(construction.get("profile", []), parameters.get("shape", {}))
    build_lathe(
        f"{track['id']}_body",
        profile,
        int(parameters.get("segments", construction.get("segments", 64))),
        group,
        material,
        recipe.get("material", {}).get("uv_mapping"),
    )

    detail_parameters = parameters.get("details", {})
    if not isinstance(detail_parameters, dict):
        detail_parameters = {}
    metal = make_luxury_material(f"recipe_metal_{track['id']}", (0.42, 0.46, 0.5), metallic=0.92, roughness=0.2)
    for detail in construction.get("details", []):
        if not isinstance(detail, dict):
            continue
        if detail.get("type") == "lid":
            radius = float(detail.get("radius", max(float(bounds[0]), float(bounds[1])) * 0.45))
            radius *= max(0.5, min(1.5, float(detail_parameters.get("lid_radius_scale", 1.0))))
            z = float(detail.get("z", float(bounds[2]) * 0.985))
            add_part(f"{track['id']}_lid", "cylinder", group, (0.0, 0.0, z),
                     (radius, radius, float(bounds[2]) * 0.006), metal)
        elif detail.get("type") == "pull_tab":
            length = float(detail.get("length", max(float(bounds[0]), float(bounds[1])) * 0.31))
            length *= max(0.5, min(1.5, float(detail_parameters.get("pull_tab_scale", 1.0))))
            z = float(detail.get("z", float(bounds[2]) * 1.003))
            bpy.ops.mesh.primitive_torus_add(
                major_radius=length * 0.28,
                minor_radius=length * 0.075,
                major_segments=24,
                minor_segments=8,
                location=(0.0, -length * 0.08, z),
            )
            tab = bpy.context.active_object
            tab.name = f"{track['id']}_pull_tab"
            tab.scale.x = 1.6
            tab.data.materials.append(metal)
            tab.parent = group

    decoration = parameters.get("decoration", {})
    if isinstance(decoration, dict) and decoration.get("front_marks"):
        accent = decoration.get("accent_color", [0.18, 0.95, 0.12])
        accent_material = make_luxury_material(
            f"recipe_accent_{track['id']}", tuple(float(v) for v in accent[:3]), metallic=0.25, roughness=0.3
        )
        for index, x in enumerate((-0.105, 0.0, 0.105)):
            mark = add_part(
                f"{track['id']}_front_mark_{index}", "cube", group,
                (x, 0.497, 0.55 + (0.025 if index == 1 else 0.0)),
                (0.032, 0.008, 0.19), accent_material,
            )
            mark.rotation_euler[1] = math.radians((-9.0, 5.0, -7.0)[index])

    group.scale = (
        float(track["size"][0]) / float(bounds[0]),
        float(track["size"][1]) / float(bounds[1]),
        float(track["size"][2]) / float(bounds[2]),
    )


def animate_procedural_character(controls: dict, track: dict) -> None:
    """Apply compact idle and walk clips to the articulated hero."""
    rig = controls["rig"]

    def key_pose(frame: int, swing: float, bob: float) -> None:
        controls["leg_l"].rotation_euler[0] = math.radians(swing)
        controls["leg_r"].rotation_euler[0] = math.radians(-swing)
        controls["arm_l"].rotation_euler[0] = math.radians(-swing * 0.8)
        controls["arm_r"].rotation_euler[0] = math.radians(swing * 0.8)
        rig.location.z = bob
        for control in (controls["leg_l"], controls["leg_r"], controls["arm_l"], controls["arm_r"]):
            control.keyframe_insert("rotation_euler", frame=frame)
        rig.keyframe_insert("location", frame=frame)

    for motion in track.get("motions", []):
        start = int(motion["start_frame"])
        end = int(motion["end_frame"])
        if motion.get("type") == "walk":
            half_cycle = max(3, int(round(6.0 / max(float(motion.get("speed", 1.0)), 0.5))))
            frame = start
            direction = 1.0
            while frame < end:
                key_pose(frame, 28.0 * direction, 0.018 if direction > 0 else 0.0)
                direction *= -1.0
                frame += half_cycle
            key_pose(end, 0.0, 0.0)
        elif motion.get("type") == "idle":
            middle = start + max(1, (end - start) // 2)
            key_pose(start, 0.0, 0.0)
            key_pose(middle, 0.0, 0.012)
            key_pose(end, 0.0, 0.0)
        elif motion.get("type") == "present":
            side = "r" if motion.get("side") == "right" else "l"
            arm = controls[f"arm_{side}"]
            sign = -1.0 if side == "r" else 1.0
            lift = start + max(1, (end - start) // 4)
            lower = end - max(1, (end - start) // 4)
            for frame, amount in ((start, 0.0), (lift, 1.0), (lower, 1.0), (end, 0.0)):
                arm.rotation_euler = (math.radians(18.0 * amount), math.radians(68.0 * sign * amount), 0.0)
                arm.keyframe_insert("rotation_euler", frame=frame)
                rig.location.z = 0.012 * amount
                rig.keyframe_insert("location", frame=frame)


def add_object(track: dict) -> None:
    """One scene object: an Empty root carrying the keyframes, with mesh children."""
    root = bpy.data.objects.new(f"obj_{track['id']}", None)
    root.empty_display_size = 0.25
    bpy.context.collection.objects.link(root)

    source = track.get("source", {})
    if source.get("type") == "procedural_character":
        controls = build_procedural_character(track, root)
        animate_procedural_character(controls, track)
    elif source.get("type") == "procedural_product":
        build_procedural_product(track, root)
    elif source.get("type") in {"blend_asset", "blend"}:
        build_blend_asset(track, root)
    elif source.get("type") == "recipe_asset":
        build_recipe_asset(track, root)
    else:
        appearance = track.get("appearance") or {}
        material = make_material(
            f"mat_{track['id']}",
            track["color"],
            roughness=float(appearance.get("roughness", 0.35 if track["asset"].endswith("car") else 0.7)),
            metallic=float(appearance.get("metallic", 0.7 if track["asset"].endswith("car") else 0.0)),
            emission_color=appearance.get("emission_color"),
            emission_strength=float(appearance.get("emission_strength", 0.0)),
        )
        for part in build_shape(source, track["size"], material):
            part.parent = root
            part.name = f"{track['id']}_{part.name}"

    emissive = track.get("emissive")
    if emissive:
        color = emissive.get("color", (1.0, 1.0, 1.0))
        z = float(emissive.get("height", track["size"][2] * 0.9))
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.22, location=(0, 0, z))
        bulb = bpy.context.active_object
        bulb.data.materials.append(
            make_emissive_material(f"emit_{track['id']}", color, float(emissive.get("power", 500.0)))
        )
        bulb.parent = root

        light_data = bpy.data.lights.new(f"light_{track['id']}", type="POINT")
        light_data.energy = float(emissive.get("power", 500.0))
        light_data.color = color
        light_data.shadow_soft_size = 0.5
        light = bpy.data.objects.new(f"light_{track['id']}", light_data)
        bpy.context.collection.objects.link(light)
        light.location = (0.0, 0.0, z)
        light.parent = root

    for key in track["keys"]:
        root.location = Vector(key["position"])
        root.rotation_euler = (0.0, 0.0, math.radians(key["yaw"]))
        root.keyframe_insert("location", frame=key["frame"])
        root.keyframe_insert("rotation_euler", frame=key["frame"])

    # Constant speed between keys: the default bezier easing makes a walk cycle
    # visibly accelerate and decelerate, which reads as sliding.
    set_linear_interpolation(root)


def add_camera(camera_plan: dict, frame_start: int) -> None:
    data = bpy.data.cameras.new("Camera")
    data.lens = float(camera_plan.get("focal_mm", 50.0))
    camera = bpy.data.objects.new("Camera", data)
    bpy.context.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    focus = bpy.data.objects.new("CameraFocus", None)
    bpy.context.collection.objects.link(focus)
    data.dof.use_dof = True
    data.dof.focus_object = focus
    data.dof.aperture_fstop = 3.2

    for key in camera_plan["keys"]:
        location = Vector(key["position"])
        target = Vector(key["look_at"])
        camera.location = location
        direction = target - location
        if direction.length > 1e-6:
            camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        camera.keyframe_insert("location", frame=key["frame"])
        camera.keyframe_insert("rotation_euler", frame=key["frame"])
        focus.location = target
        focus.keyframe_insert("location", frame=key["frame"])

    set_linear_interpolation(camera)


def add_rain(extent: float, frame_end: int) -> None:
    """Particle rain over the shot area, instanced from a thin streak mesh."""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, -50.0))
    drop = bpy.context.active_object
    drop.name = "RainDrop"
    drop.scale = (0.012, 0.012, 0.16)
    drop.data.materials.append(make_material("RainDrop", (0.62, 0.70, 0.80), roughness=0.08))
    drop.hide_render = False

    size = max(30.0, extent * 2.2)
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0.0, 0.0, 14.0))
    emitter = bpy.context.active_object
    emitter.name = "RainEmitter"
    emitter.hide_render = True

    modifier = emitter.modifiers.new("Rain", type="PARTICLE_SYSTEM")
    settings = modifier.particle_system.settings
    settings.count = 9000
    settings.frame_start = -60  # pre-roll so rain is falling at frame 1
    settings.frame_end = frame_end
    settings.lifetime = 90
    settings.emit_from = "FACE"
    settings.physics_type = "NEWTON"
    settings.normal_factor = -9.0
    settings.effector_weights.gravity = 1.0
    settings.render_type = "OBJECT"
    settings.instance_object = drop
    settings.particle_size = 1.0
    settings.use_rotations = True
    settings.render_step = 3


def try_engine(render, identifier: str) -> bool:
    """Assign an engine, reporting whether it took.

    ``engine`` is a dynamic enum and it under-reports in background mode: a
    headless Blender 5.2 build enumerates only ``BLENDER_EEVEE`` yet accepts
    ``"CYCLES"`` on assignment. So probe by assigning and catching, never by
    checking membership in ``enum_items``.
    """
    try:
        render.engine = identifier
    except TypeError:
        return False
    return render.engine == identifier


def configure_cycles(scene, samples: int, device: str) -> None:
    cycles = scene.cycles
    cycles.samples = samples
    try:
        cycles.device = device
    except TypeError:
        print(f"kentsugi: Cycles device {device!r} unavailable; using CPU")
        cycles.device = "CPU"
    try:
        cycles.use_denoising = True
    except Exception as exc:  # noqa: BLE001 - denoiser availability varies by build
        print(f"kentsugi: denoising unavailable ({exc})")


def select_engine(render, wanted: str, samples: int, device: str) -> None:
    """Set the render engine, tolerating naming changes and missing add-ons."""
    scene = bpy.context.scene

    if wanted == "CYCLES":
        if "cycles" not in bpy.context.preferences.addons:
            try:
                bpy.ops.preferences.addon_enable(module="cycles")
            except Exception as exc:  # noqa: BLE001 - many failure modes, all non-fatal
                print(f"kentsugi: could not enable Cycles ({exc})")
        if try_engine(render, "CYCLES"):
            configure_cycles(scene, samples, device)
            return
        print("kentsugi: Cycles unavailable in this build; rendering with EEVEE instead")

    # The EEVEE identifier was BLENDER_EEVEE_NEXT for the 4.2-4.5 line and
    # reverted to BLENDER_EEVEE in 5.x. Pick whichever this build accepts.
    for identifier in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if try_engine(render, identifier):
            break
    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "taa_render_samples"):
        eevee.taa_render_samples = samples


def configure_render(
    plan: dict, output_dir: Path, device: str, *, still: bool = False,
    samples_override: int | None = None, resolution_scale: int = 100,
) -> None:
    scene = bpy.context.scene
    render = scene.render
    settings = plan["render"]

    render.resolution_x, render.resolution_y = settings["resolution"]
    render.resolution_percentage = max(1, min(100, resolution_scale))
    render.fps = plan["fps"]
    scene.frame_start = plan["frame_start"]
    scene.frame_end = plan["frame_end"]

    samples = samples_override if samples_override is not None else settings.get("samples", 16)
    select_engine(render, settings.get("engine", "EEVEE"), max(1, samples), device)
    print(f"kentsugi: engine {render.engine}")
    if hasattr(render, "use_motion_blur"):
        render.use_motion_blur = True
    view = scene.view_settings
    try:
        view.view_transform = "AgX"
        view.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    view.exposure = 0.35

    image_settings = render.image_settings
    # Blender 5.x hides video formats behind media_type; on 4.x setting
    # file_format directly is enough and media_type does not exist.
    has_media_type = "media_type" in type(image_settings).bl_rna.properties
    if still:
        if has_media_type:
            image_settings.media_type = "IMAGE"
        image_settings.file_format = "PNG"
        render.filepath = str(output_dir / "kentsugi.png")
    else:
        if has_media_type:
            image_settings.media_type = "VIDEO"
        image_settings.file_format = "FFMPEG"
        render.ffmpeg.format = "MPEG4"
        render.ffmpeg.codec = "H264"
        render.ffmpeg.constant_rate_factor = "HIGH" if settings["quality"] == "final" else "MEDIUM"
        render.ffmpeg.ffmpeg_preset = "GOOD"
        render.filepath = str(output_dir / "kentsugi_")


def scene_extent(plan: dict) -> float:
    values = [0.0]
    for track in plan["objects"]:
        for key in track["keys"]:
            values.append(abs(key["position"][0]))
            values.append(abs(key["position"][1]))
    for key in plan["camera"]["keys"]:
        values.append(abs(key["position"][0]))
        values.append(abs(key["position"][1]))
    return max(values)


def collect_output(output_dir: Path, destination: Path) -> Path | None:
    """Blender decorates video filenames with the frame range; find and move it."""
    candidates = [p for p in output_dir.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES]
    if not candidates:
        return None
    produced = max(candidates, key=lambda p: p.stat().st_size)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(produced), str(destination))
    return destination


def main() -> int:
    args = parse_args(sys.argv)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    clear_scene()
    extent = scene_extent(plan)

    setup_world(plan["environment"])
    add_ground(plan["environment"], extent)
    if plan["environment"].get("preset") == "studio":
        add_studio_set()

    for track in plan["objects"]:
        add_object(track)

    add_camera(plan["camera"], plan["frame_start"])

    if plan["environment"].get("weather") == "rain":
        add_rain(extent, plan["frame_end"])

    with tempfile.TemporaryDirectory(prefix="kentsugi-render-") as tmp:
        tmp_dir = Path(tmp)
        still = args.still_frame is not None
        configure_render(
            plan,
            tmp_dir,
            args.device,
            still=still,
            samples_override=args.samples,
            resolution_scale=args.resolution_scale,
        )

        if args.save_blend:
            Path(args.save_blend).parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(Path(args.save_blend).resolve()))

        if still:
            frame = max(plan["frame_start"], min(plan["frame_end"], args.still_frame))
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            bpy.ops.render.render(write_still=True)
            source = tmp_dir / "kentsugi.png"
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            produced = Path(shutil.move(str(source), str(destination))) if source.is_file() else None
        else:
            bpy.ops.render.render(animation=True)
            produced = collect_output(tmp_dir, Path(args.output))
        if produced is None:
            print("kentsugi: render produced no video file", file=sys.stderr)
            return 1
        print(f"kentsugi: wrote {produced}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
