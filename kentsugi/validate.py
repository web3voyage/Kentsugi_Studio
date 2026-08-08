"""Validation.

Two layers, both of which return the same structured issue type:

* **Shape** — types, enums, ranges, id syntax. Catches malformed JSON that
  parsed but means nothing.
* **Semantic** — cross-references, catalog lookups, timing. Catches documents
  that are well-formed but unrenderable.

Issues are structured rather than free-text because their real consumer is an
AI repair loop: on failure you feed this list back to the model and ask for a
corrected document. Prose error messages make that loop unreliable.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .appearance import resolve_color
from .catalog import CHARACTER_KINDS, Catalog

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

PRESETS = ("city", "interior_room", "open_field", "studio", "void")
TIMES_OF_DAY = ("dawn", "day", "dusk", "night")
WEATHERS = ("clear", "overcast", "rain", "fog")
GROUND_MATERIALS = ("asphalt", "concrete", "grass", "wood", "none")
DIRECTIONS = ("ahead", "behind", "left", "right", "north", "south", "east", "west")
ACTION_TYPES = ("walk_to", "idle", "look_at", "present")
CAMERA_TYPES = ("follow", "static", "orbit")
CAMERA_POSITIONS = ("behind", "front", "left", "right", "overhead")
SHOTS = ("wide", "medium", "close_up")
QUALITIES = ("preview", "final")

TARGETED_ACTIONS = ("walk_to", "look_at", "present")
TARGETED_CAMERAS = ("follow", "orbit")

TOP_LEVEL_FIELDS = frozenset(
    {"kentsugi_version", "title", "environment", "objects", "actions", "camera", "render"}
)
ENVIRONMENT_FIELDS = frozenset({"preset", "time_of_day", "weather", "ground_material"})
OBJECT_FIELDS = frozenset({"id", "asset", "placement", "facing", "scale", "appearance"})
AT_PLACEMENT_FIELDS = frozenset({"type", "position"})
RELATIVE_PLACEMENT_FIELDS = frozenset({"type", "ref", "direction", "distance"})
ACTION_FIELDS = frozenset({"actor", "type", "target", "stop_distance", "start", "duration"})
CAMERA_FIELDS = frozenset({"type", "target", "position", "distance", "height", "shot"})
RENDER_FIELDS = frozenset({"duration", "fps", "resolution", "quality"})


@dataclass(frozen=True)
class Issue:
    severity: str
    """'error' blocks rendering; 'warning' does not."""
    path: str
    """JSON-pointer-ish location, e.g. 'objects[2].placement.ref'."""
    code: str
    """Stable machine-readable code. Safe to branch on."""
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.path}: {self.message} [{self.code}]"


def _err(path: str, code: str, message: str) -> Issue:
    return Issue("error", path, code, message)


def _warn(path: str, code: str, message: str) -> Issue:
    return Issue("warning", path, code, message)


def _check_fields(value: dict[str, Any], allowed: frozenset[str], path: str, out: list[Issue]) -> None:
    """Mirror JSON Schema ``additionalProperties: false`` at runtime."""
    for key in value:
        if key not in allowed:
            location = f"{path}.{key}" if path else key
            out.append(_err(location, "unknown_field", f"field {key!r} is not allowed"))


def _check_enum(value: Any, allowed: tuple[str, ...], path: str, out: list[Issue]) -> bool:
    if value not in allowed:
        out.append(
            _err(path, "bad_enum", f"expected one of {list(allowed)}, got {value!r}")
        )
        return False
    return True


def _check_number(
    value: Any, path: str, out: list[Issue], *, lo: float | None = None,
    hi: float | None = None, exclusive_lo: bool = False,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        out.append(_err(path, "bad_type", f"expected a number, got {type(value).__name__}"))
        return False
    if lo is not None and (value <= lo if exclusive_lo else value < lo):
        rel = ">" if exclusive_lo else ">="
        out.append(_err(path, "out_of_range", f"must be {rel} {lo}, got {value}"))
        return False
    if hi is not None and value > hi:
        out.append(_err(path, "out_of_range", f"must be <= {hi}, got {value}"))
        return False
    return True


def _check_id(value: Any, path: str, out: list[Issue]) -> bool:
    if not isinstance(value, str):
        out.append(_err(path, "bad_type", f"expected a string id, got {type(value).__name__}"))
        return False
    if not ID_RE.match(value):
        out.append(
            _err(path, "bad_id", f"{value!r} is not lowercase snake_case (max 48 chars)")
        )
        return False
    return True


def validate(doc: dict[str, Any], catalog: Catalog) -> list[Issue]:
    """Return every issue found. An empty list means the document is renderable."""
    out: list[Issue] = []

    if not isinstance(doc, dict):
        return [_err("", "bad_type", "document must be a JSON object")]

    _check_fields(doc, TOP_LEVEL_FIELDS, "", out)

    title = doc.get("title")
    if title is not None:
        if not isinstance(title, str):
            out.append(_err("title", "bad_type", "title must be a string"))
        elif len(title) > 200:
            out.append(_err("title", "too_long", "title must be at most 200 characters"))

    version = doc.get("kentsugi_version")
    if version != "0.1.0":
        out.append(
            _err("kentsugi_version", "bad_version", f"expected '0.1.0', got {version!r}")
        )

    _validate_environment(doc.get("environment"), out)
    object_ids = _validate_objects(doc.get("objects"), catalog, out)
    _validate_actions(doc.get("actions"), doc, object_ids, catalog, out)
    _validate_camera(doc.get("camera"), object_ids, out)
    _validate_render(doc.get("render"), out)

    return out


def _validate_environment(env: Any, out: list[Issue]) -> None:
    if not isinstance(env, dict):
        out.append(_err("environment", "missing", "environment is required and must be an object"))
        return
    _check_fields(env, ENVIRONMENT_FIELDS, "environment", out)
    if "preset" not in env:
        out.append(_err("environment.preset", "missing", "preset is required"))
    else:
        _check_enum(env["preset"], PRESETS, "environment.preset", out)
    if "time_of_day" not in env:
        out.append(_err("environment.time_of_day", "missing", "time_of_day is required"))
    else:
        _check_enum(env["time_of_day"], TIMES_OF_DAY, "environment.time_of_day", out)
    if "weather" in env:
        _check_enum(env["weather"], WEATHERS, "environment.weather", out)
    if "ground_material" in env:
        _check_enum(env["ground_material"], GROUND_MATERIALS, "environment.ground_material", out)


def _validate_objects(objects: Any, catalog: Catalog, out: list[Issue]) -> set[str]:
    """Validate objects and return the set of ids that are usable as references."""
    if not isinstance(objects, list):
        out.append(_err("objects", "bad_type", "objects must be an array"))
        return set()
    if len(objects) > 64:
        out.append(_err("objects", "too_many_items", "objects must contain at most 64 items"))

    seen: set[str] = set()
    for i, obj in enumerate(objects):
        base = f"objects[{i}]"
        if not isinstance(obj, dict):
            out.append(_err(base, "bad_type", "each object must be a JSON object"))
            continue
        _check_fields(obj, OBJECT_FIELDS, base, out)

        if "id" not in obj:
            out.append(_err(f"{base}.id", "missing", "id is required"))
        elif _check_id(obj["id"], f"{base}.id", out):
            if obj["id"] in seen:
                out.append(_err(f"{base}.id", "duplicate_id", f"id {obj['id']!r} is used twice"))
            else:
                seen.add(obj["id"])

        if "asset" not in obj:
            out.append(_err(f"{base}.asset", "missing", "asset is required"))
        elif _check_id(obj["asset"], f"{base}.asset", out) and obj["asset"] not in catalog:
            out.append(
                _err(
                    f"{base}.asset",
                    "unknown_asset",
                    f"{obj['asset']!r} is not in the catalog; available: {catalog.ids()}",
                )
            )

        if "scale" in obj:
            _check_number(obj["scale"], f"{base}.scale", out, lo=0, hi=20, exclusive_lo=True)

        _validate_appearance(obj.get("appearance"), f"{base}.appearance", out)

        _validate_placement(obj.get("placement"), f"{base}.placement", out)

        facing = obj.get("facing")
        if facing is not None and not isinstance(facing, str):
            _check_number(facing, f"{base}.facing", out, lo=-360, hi=360)

    # Reference checks run in a second pass so that forward references resolve.
    for i, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        base = f"objects[{i}]"
        placement = obj.get("placement")
        if isinstance(placement, dict) and placement.get("type") == "relative":
            ref = placement.get("ref")
            if isinstance(ref, str) and ref not in seen:
                out.append(
                    _err(f"{base}.placement.ref", "unknown_reference", f"no object with id {ref!r}")
                )
            if isinstance(ref, str) and ref == obj.get("id"):
                out.append(
                    _err(f"{base}.placement.ref", "self_reference", "an object cannot be placed relative to itself")
                )
        facing = obj.get("facing")
        if isinstance(facing, str):
            if facing not in seen:
                out.append(_err(f"{base}.facing", "unknown_reference", f"no object with id {facing!r}"))
            elif facing == obj.get("id"):
                out.append(_err(f"{base}.facing", "self_reference", "an object cannot face itself"))

    _detect_placement_cycles(objects, out)
    return seen


def _validate_appearance(appearance: Any, path: str, out: list[Issue]) -> None:
    if appearance is None:
        return
    if not isinstance(appearance, dict):
        out.append(_err(path, "bad_type", "appearance must be an object"))
        return

    allowed = {"color", "metallic", "roughness", "emission_color", "emission_strength"}
    for field in appearance:
        if field not in allowed:
            out.append(_err(f"{path}.{field}", "unknown_field", f"unsupported appearance field {field!r}"))

    for field in ("color", "emission_color"):
        if field not in appearance:
            continue
        try:
            resolve_color(appearance[field])
        except ValueError as exc:
            out.append(_err(f"{path}.{field}", "bad_color", str(exc)))

    for field in ("metallic", "roughness"):
        if field in appearance:
            _check_number(appearance[field], f"{path}.{field}", out, lo=0, hi=1)
    if "emission_strength" in appearance:
        _check_number(appearance["emission_strength"], f"{path}.emission_strength", out, lo=0, hi=10_000)


def _detect_placement_cycles(objects: list[Any], out: list[Issue]) -> None:
    """Placement cycles are unresolvable; facing cycles are not (see layout.py)."""
    graph: dict[str, str] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        placement = obj.get("placement")
        if isinstance(placement, dict) and placement.get("type") == "relative":
            oid, ref = obj.get("id"), placement.get("ref")
            if isinstance(oid, str) and isinstance(ref, str):
                graph[oid] = ref

    for start in graph:
        seen_nodes = {start}
        node = graph.get(start)
        while node is not None and node in graph:
            if node in seen_nodes:
                out.append(
                    _err(
                        f"objects[{start}].placement.ref",
                        "placement_cycle",
                        f"placement chain starting at {start!r} loops back on itself",
                    )
                )
                break
            seen_nodes.add(node)
            node = graph.get(node)


def _validate_placement(placement: Any, path: str, out: list[Issue]) -> None:
    if not isinstance(placement, dict):
        out.append(_err(path, "missing", "placement is required and must be an object"))
        return

    kind = placement.get("type")
    if kind == "at":
        _check_fields(placement, AT_PLACEMENT_FIELDS, path, out)
        position = placement.get("position")
        if not isinstance(position, list) or len(position) != 2:
            out.append(_err(f"{path}.position", "bad_type", "position must be [x, y] in metres"))
        else:
            for j, v in enumerate(position):
                _check_number(v, f"{path}.position[{j}]", out, lo=-500, hi=500)
    elif kind == "relative":
        _check_fields(placement, RELATIVE_PLACEMENT_FIELDS, path, out)
        if "ref" not in placement:
            out.append(_err(f"{path}.ref", "missing", "ref is required for relative placement"))
        else:
            _check_id(placement["ref"], f"{path}.ref", out)
        _check_enum(placement.get("direction"), DIRECTIONS, f"{path}.direction", out)
        if "distance" not in placement:
            out.append(_err(f"{path}.distance", "missing", "distance is required"))
        else:
            _check_number(placement["distance"], f"{path}.distance", out, lo=0, hi=500)
    else:
        out.append(
            _err(f"{path}.type", "bad_enum", f"expected 'at' or 'relative', got {kind!r}")
        )


def _validate_actions(
    actions: Any, doc: dict[str, Any], object_ids: set[str], catalog: Catalog, out: list[Issue]
) -> None:
    if actions is None:
        return
    if not isinstance(actions, list):
        out.append(_err("actions", "bad_type", "actions must be an array"))
        return
    if len(actions) > 64:
        out.append(_err("actions", "too_many_items", "actions must contain at most 64 items"))

    render = doc.get("render")
    shot_duration = render.get("duration") if isinstance(render, dict) else None

    by_id = {}
    if isinstance(doc.get("objects"), list):
        by_id = {o.get("id"): o for o in doc["objects"] if isinstance(o, dict)}

    for i, action in enumerate(actions):
        base = f"actions[{i}]"
        if not isinstance(action, dict):
            out.append(_err(base, "bad_type", "each action must be a JSON object"))
            continue
        _check_fields(action, ACTION_FIELDS, base, out)

        actor = action.get("actor")
        if not _check_id(actor, f"{base}.actor", out):
            continue
        if actor not in object_ids:
            out.append(_err(f"{base}.actor", "unknown_reference", f"no object with id {actor!r}"))

        kind = action.get("type")
        if not _check_enum(kind, ACTION_TYPES, f"{base}.type", out):
            continue

        if kind in TARGETED_ACTIONS:
            target = action.get("target")
            if target is None:
                out.append(_err(f"{base}.target", "missing", f"{kind} requires a target"))
            elif _check_id(target, f"{base}.target", out):
                if target not in object_ids:
                    out.append(
                        _err(f"{base}.target", "unknown_reference", f"no object with id {target!r}")
                    )
                if target == actor:
                    out.append(
                        _err(f"{base}.target", "self_reference", f"{actor!r} cannot {kind} itself")
                    )

        if kind in {"walk_to", "present"}:
            # Only assets the catalog marks as characters can locomote. Without
            # this, "the building walks to the car" validates and then renders.
            actor_obj = by_id.get(actor)
            if isinstance(actor_obj, dict):
                asset = catalog.get(actor_obj.get("asset", ""))
                if asset is not None and asset.kind not in CHARACTER_KINDS:
                    out.append(
                        _err(
                            f"{base}.actor",
                            "not_a_character",
                            f"{actor!r} uses asset {asset.id!r} of kind {asset.kind!r}, which cannot {kind}",
                        )
                    )

        start = action.get("start", 0)
        duration = action.get("duration", 4)
        ok_start = _check_number(start, f"{base}.start", out, lo=0, hi=600)
        ok_duration = _check_number(duration, f"{base}.duration", out, lo=0, hi=600, exclusive_lo=True)

        if "stop_distance" in action:
            _check_number(action["stop_distance"], f"{base}.stop_distance", out, lo=0, hi=500)

        if ok_start and ok_duration and isinstance(shot_duration, (int, float)):
            if start + duration > shot_duration + 1e-9:
                out.append(
                    _warn(
                        f"{base}.duration",
                        "action_exceeds_shot",
                        f"action ends at {start + duration}s but the shot is {shot_duration}s; it will be cut off",
                    )
                )

    walks_by_actor: dict[str, list[tuple[float, float, int]]] = {}
    for i, action in enumerate(actions):
        if not isinstance(action, dict) or action.get("type") != "walk_to":
            continue
        actor = action.get("actor")
        start, duration = action.get("start", 0), action.get("duration", 4)
        if isinstance(actor, str) and isinstance(start, (int, float)) and isinstance(duration, (int, float)):
            walks_by_actor.setdefault(actor, []).append((float(start), float(start + duration), i))
    for actor, intervals in walks_by_actor.items():
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1] - 1e-9:
                out.append(
                    _err(
                        f"actions[{current[2]}].start",
                        "overlapping_action",
                        f"walk_to actions for {actor!r} overlap in time",
                    )
                )


def _validate_camera(camera: Any, object_ids: set[str], out: list[Issue]) -> None:
    if not isinstance(camera, dict):
        out.append(_err("camera", "missing", "camera is required and must be an object"))
        return
    _check_fields(camera, CAMERA_FIELDS, "camera", out)

    kind = camera.get("type")
    if not _check_enum(kind, CAMERA_TYPES, "camera.type", out):
        return

    target = camera.get("target")
    if kind in TARGETED_CAMERAS:
        if target is None:
            out.append(_err("camera.target", "missing", f"a {kind} camera requires a target"))
        elif _check_id(target, "camera.target", out) and target not in object_ids:
            out.append(_err("camera.target", "unknown_reference", f"no object with id {target!r}"))
    elif target is not None:
        if _check_id(target, "camera.target", out) and target not in object_ids:
            out.append(_err("camera.target", "unknown_reference", f"no object with id {target!r}"))

    if "position" in camera:
        _check_enum(camera["position"], CAMERA_POSITIONS, "camera.position", out)
    if "shot" in camera:
        _check_enum(camera["shot"], SHOTS, "camera.shot", out)
    if "distance" in camera:
        _check_number(camera["distance"], "camera.distance", out, lo=0, hi=500)
    if "height" in camera:
        _check_number(camera["height"], "camera.height", out, lo=0, hi=100)


def _validate_render(render: Any, out: list[Issue]) -> None:
    if not isinstance(render, dict):
        out.append(_err("render", "missing", "render is required and must be an object"))
        return
    _check_fields(render, RENDER_FIELDS, "render", out)

    if "duration" not in render:
        out.append(_err("render.duration", "missing", "duration is required"))
    else:
        _check_number(render["duration"], "render.duration", out, lo=0, hi=600, exclusive_lo=True)

    if "fps" in render:
        fps = render["fps"]
        if not isinstance(fps, int) or isinstance(fps, bool):
            out.append(_err("render.fps", "bad_type", f"fps must be an integer, got {fps!r}"))
        else:
            _check_number(fps, "render.fps", out, lo=1, hi=120)

    if "resolution" in render:
        resolution = render["resolution"]
        if not isinstance(resolution, list) or len(resolution) != 2:
            out.append(_err("render.resolution", "bad_type", "resolution must be [width, height]"))
        else:
            for j, v in enumerate(resolution):
                if not isinstance(v, int) or isinstance(v, bool):
                    out.append(
                        _err(f"render.resolution[{j}]", "bad_type", f"must be an integer, got {v!r}")
                    )
                else:
                    _check_number(v, f"render.resolution[{j}]", out, lo=16, hi=7680)

    if "quality" in render:
        _check_enum(render["quality"], QUALITIES, "render.quality", out)


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def warnings(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "warning"]


def to_repair_payload(issues: list[Issue]) -> str:
    """Serialise issues for feeding back to the model in a repair turn."""
    return json.dumps([asdict(i) for i in errors(issues)], indent=2)
