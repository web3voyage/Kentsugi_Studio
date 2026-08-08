"""Timeline baking: a resolved scene plus actions become explicit keyframes.

This runs in plain CPython, not in Blender. That is the point — all the motion
logic is testable without launching a renderer, and the Blender applier becomes
a dumb consumer that sets keys it is handed. Swapping in Godot or Unity later
means reimplementing only the applier, not any of this.
"""

from __future__ import annotations

import math
from typing import Any

from .appearance import color_list
from .catalog import Catalog
from .document import FOCAL_LENGTHS_MM, content_hash
from .layout import Layout, Placed, forward, right, yaw_towards

ENGINE_BY_QUALITY = {"preview": "EEVEE", "final": "CYCLES"}
SAMPLES_BY_QUALITY = {"preview": 16, "final": 128}

EYE_HEIGHT_FRACTION = 0.6
"""Cameras aim at this fraction of the target's height rather than its base,
so a follow camera frames a character's torso instead of their feet."""

MIN_ORBIT_RADIUS = 0.5


def bake(doc: dict[str, Any], catalog: Catalog, layout: Layout) -> dict[str, Any]:
    """Produce an engine-agnostic render plan."""
    render = doc["render"]
    fps = int(render.get("fps", 24))
    duration = float(render["duration"])
    frame_start = 1
    frame_count = max(1, round(duration * fps))
    frame_end = frame_start + frame_count - 1
    quality = render.get("quality", "preview")

    object_tracks = _bake_objects(doc, layout, fps, frame_start, frame_end)
    camera_track = _bake_camera(doc, layout, object_tracks, fps, frame_start, frame_end)

    return {
        "kentsugi_version": doc.get("kentsugi_version", "0.1.0"),
        "source_hash": content_hash(doc),
        "title": doc.get("title", "Untitled shot"),
        "fps": fps,
        "duration": duration,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "environment": doc["environment"],
        "render": {
            "resolution": list(render.get("resolution", [1280, 720])),
            "quality": quality,
            "engine": ENGINE_BY_QUALITY.get(quality, "EEVEE"),
            "samples": SAMPLES_BY_QUALITY.get(quality, 16),
        },
        "objects": object_tracks,
        "camera": camera_track,
        "warnings": list(layout.warnings),
    }


def _time_to_frame(t: float, fps: int, frame_start: int) -> int:
    return frame_start + int(round(t * fps))


def _bake_objects(
    doc: dict[str, Any], layout: Layout, fps: int, frame_start: int, frame_end: int
) -> list[dict[str, Any]]:
    tracks: dict[str, dict[str, Any]] = {}

    specs_by_id = {
        spec.get("id"): spec
        for spec in doc.get("objects", [])
        if isinstance(spec, dict) and isinstance(spec.get("id"), str)
    }

    for placed in layout.objects.values():
        asset = placed.asset
        spec = specs_by_id.get(placed.id, {})
        appearance_spec = spec.get("appearance") if isinstance(spec, dict) else None
        appearance = _bake_appearance(appearance_spec, asset.color)
        tracks[placed.id] = {
            "id": placed.id,
            "asset": asset.id,
            "source": asset.source,
            "size": [v * placed.scale for v in asset.size],
            "color": appearance["color"],
            "appearance": appearance if isinstance(appearance_spec, dict) else None,
            "emissive": asset.emissive,
            "scale": placed.scale,
            "static": asset.static,
            "keys": [
                {
                    "frame": frame_start,
                    "position": list(placed.origin()),
                    "yaw": placed.yaw,
                }
            ],
        }

    actions = sorted(
        (a for a in doc.get("actions", []) if isinstance(a, dict)),
        key=lambda a: float(a.get("start", 0.0)),
    )
    for action in actions:
        actor = layout.get(action["actor"])
        if actor is None:
            continue

        start_t = float(action.get("start", 0.0))
        duration = float(action.get("duration", 4.0))
        track = tracks[actor.id]
        start_frame = max(frame_start, _time_to_frame(start_t, fps, frame_start))
        end_frame = min(frame_end, _time_to_frame(start_t + duration, fps, frame_start))
        if end_frame <= start_frame:
            continue

        start_pos, start_yaw = sample_track(track["keys"], start_frame)
        kind = action.get("type")
        target = layout.get(action.get("target", ""))
        target_track = tracks.get(target.id) if target is not None else None

        if kind == "idle":
            track["keys"].extend(
                [
                    {"frame": start_frame, "position": list(start_pos), "yaw": start_yaw},
                    {"frame": end_frame, "position": list(start_pos), "yaw": start_yaw},
                ]
            )
            track.setdefault("motions", []).append(
                {
                    "type": "idle",
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "clip": "idle" if "idle" in actor.asset.clips else None,
                }
            )
            continue

        if target is None or target_track is None:
            continue
        target_pos, _ = sample_track(target_track["keys"], start_frame)
        heading = yaw_towards((start_pos[0], start_pos[1]), (target_pos[0], target_pos[1]))

        if kind == "look_at":
            track["keys"].extend(
                [
                    {"frame": start_frame, "position": list(start_pos), "yaw": heading},
                    {"frame": end_frame, "position": list(start_pos), "yaw": heading},
                ]
            )
            continue

        if kind == "present":
            dx = target_pos[0] - start_pos[0]
            dy = target_pos[1] - start_pos[1]
            rx, ry = right(start_yaw)
            side = "right" if dx * rx + dy * ry >= 0 else "left"
            track["keys"].extend(
                [
                    {"frame": start_frame, "position": list(start_pos), "yaw": start_yaw},
                    {"frame": end_frame, "position": list(start_pos), "yaw": start_yaw},
                ]
            )
            track.setdefault("motions", []).append(
                {
                    "type": "present",
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "side": side,
                    "clip": "present" if "present" in actor.asset.clips else None,
                }
            )
            continue

        if kind != "walk_to":
            continue

        stop_distance = float(action.get("stop_distance", 1.5))
        dx = target_pos[0] - start_pos[0]
        dy = target_pos[1] - start_pos[1]
        gap = math.hypot(dx, dy)

        # Stop short of the target, and never walk backwards through it.
        travel = max(0.0, gap - max(stop_distance, target.radius + actor.radius))
        if gap < 1e-9 or travel < 1e-6:
            continue

        effective_duration = (end_frame - start_frame) / fps
        effective_travel = travel * min(1.0, effective_duration / duration)
        ux, uy = dx / gap, dy / gap
        end_pos = (start_pos[0] + ux * effective_travel, start_pos[1] + uy * effective_travel, 0.0)

        track["keys"].extend(
            [
                {"frame": start_frame, "position": list(start_pos), "yaw": heading},
                {"frame": end_frame, "position": list(end_pos), "yaw": heading},
            ]
        )
        motion = {
            "type": "walk",
            "start_frame": start_frame,
            "end_frame": end_frame,
            "speed": travel / duration,
            "clip": "walk" if "walk" in actor.asset.clips else None,
        }
        track.setdefault("motions", []).append(motion)

    for track in tracks.values():
        track["keys"] = _dedupe_keys(track["keys"])
        if len(track.get("motions", [])) == 1:
            track["motion"] = track["motions"][0]

    return list(tracks.values())


def _bake_appearance(appearance: Any, catalog_color: tuple[float, ...]) -> dict[str, Any]:
    spec = appearance if isinstance(appearance, dict) else {}
    baked: dict[str, Any] = {"color": color_list(spec.get("color", catalog_color))}
    for field in ("metallic", "roughness", "emission_strength"):
        if field in spec:
            baked[field] = float(spec[field])
    if "emission_color" in spec:
        baked["emission_color"] = color_list(spec["emission_color"])
    return baked


def _dedupe_keys(keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(keys, key=lambda k: k["frame"]):
        if out and out[-1]["frame"] == key["frame"]:
            out[-1] = key
        else:
            out.append(key)
    return out


def sample_track(keys: list[dict[str, Any]], frame: int) -> tuple[tuple[float, float, float], float]:
    """Linearly interpolate a baked track at an arbitrary frame."""
    if not keys:
        return ((0.0, 0.0, 0.0), 0.0)
    if frame <= keys[0]["frame"]:
        k = keys[0]
        return (tuple(k["position"]), k["yaw"])  # type: ignore[return-value]
    if frame >= keys[-1]["frame"]:
        k = keys[-1]
        return (tuple(k["position"]), k["yaw"])  # type: ignore[return-value]

    for a, b in zip(keys, keys[1:]):
        if a["frame"] <= frame <= b["frame"]:
            span = b["frame"] - a["frame"]
            t = 0.0 if span == 0 else (frame - a["frame"]) / span
            pos = tuple(a["position"][i] + (b["position"][i] - a["position"][i]) * t for i in range(3))
            yaw = a["yaw"] + (b["yaw"] - a["yaw"]) * t
            return (pos, yaw)  # type: ignore[return-value]

    k = keys[-1]
    return (tuple(k["position"]), k["yaw"])  # type: ignore[return-value]


def _camera_offset(position: str, target: Placed, distance: float) -> tuple[float, float]:
    """Planar offset from the target to the camera, in the target's local frame."""
    if position == "behind":
        fx, fy = forward(target.yaw)
        return (-fx * distance, -fy * distance)
    if position == "front":
        fx, fy = forward(target.yaw)
        return (fx * distance, fy * distance)
    if position == "right":
        rx, ry = right(target.yaw)
        return (rx * distance, ry * distance)
    if position == "left":
        rx, ry = right(target.yaw)
        return (-rx * distance, -ry * distance)
    return (0.0, 0.0)  # overhead


def _bake_camera(
    doc: dict[str, Any],
    layout: Layout,
    object_tracks: list[dict[str, Any]],
    fps: int,
    frame_start: int,
    frame_end: int,
) -> dict[str, Any]:
    camera = doc["camera"]
    kind = camera["type"]
    distance = float(camera.get("distance", 6.0))
    height = float(camera.get("height", 1.7))
    position = camera.get("position", "behind")
    focal = FOCAL_LENGTHS_MM.get(camera.get("shot", "medium"), 50.0)

    tracks_by_id = {t["id"]: t for t in object_tracks}
    target_id = camera.get("target")
    target = layout.get(target_id) if isinstance(target_id, str) else None

    keys: list[dict[str, Any]] = []

    if target is None:
        # No target: a fixed camera looking at the world origin.
        eye = (distance, -distance, max(height, 1.0))
        keys.append({"frame": frame_start, "position": list(eye), "look_at": [0.0, 0.0, 0.0]})
        keys.append({"frame": frame_end, "position": list(eye), "look_at": [0.0, 0.0, 0.0]})
        return {"type": kind, "focal_mm": focal, "keys": keys}

    target_track = tracks_by_id.get(target.id, {"keys": []})
    aim_height = target.height * EYE_HEIGHT_FRACTION

    if kind == "static":
        # Anchored where the target starts, then held while the target moves.
        start_pos, start_yaw = sample_track(target_track["keys"], frame_start)
        anchor = Placed(target.id, target.asset, (start_pos[0], start_pos[1]), start_yaw, target.scale)
        ox, oy = _camera_offset(position, anchor, distance)
        eye = (start_pos[0] + ox, start_pos[1] + oy, height if position != "overhead" else height + distance)
        for frame in (frame_start, frame_end):
            pos, _ = sample_track(target_track["keys"], frame)
            keys.append(
                {
                    "frame": frame,
                    "position": list(eye),
                    "look_at": [pos[0], pos[1], pos[2] + aim_height],
                }
            )
        return {"type": kind, "focal_mm": focal, "keys": keys}

    # follow and orbit are sampled every frame: both depend on where the target
    # is at that instant, so there is no cheaper keyframe set.
    total_frames = max(1, frame_end - frame_start)
    for frame in range(frame_start, frame_end + 1):
        pos, yaw = sample_track(target_track["keys"], frame)
        look_at = [pos[0], pos[1], pos[2] + aim_height]

        if kind == "orbit":
            t = (frame - frame_start) / total_frames
            angle = 2.0 * math.pi * t
            radius = max(MIN_ORBIT_RADIUS, distance)
            eye = (
                pos[0] + math.sin(angle) * radius,
                pos[1] - math.cos(angle) * radius,
                pos[2] + height,
            )
        else:  # follow
            anchor = Placed(target.id, target.asset, (pos[0], pos[1]), yaw, target.scale)
            ox, oy = _camera_offset(position, anchor, distance)
            if position == "overhead":
                eye = (pos[0], pos[1], pos[2] + height + distance)
            else:
                eye = (pos[0] + ox, pos[1] + oy, pos[2] + height)

        keys.append({"frame": frame, "position": list(eye), "look_at": look_at})

    return {"type": kind, "focal_mm": focal, "keys": keys}
