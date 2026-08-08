"""A top-down SVG of a baked plan.

Blender is a heavy dependency and a slow feedback loop. This renders the same
plan as a floor diagram using nothing but the standard library, so layout and
camera-path bugs are visible in a browser in milliseconds rather than after a
render. It is a debugging view, not a product surface.
"""

from __future__ import annotations

import math
from typing import Any

MARGIN_PX = 48
DEFAULT_WIDTH_PX = 900
MIN_EXTENT_M = 8.0

_BG = "#111417"
_GRID = "#1e242a"
_GROUND = "#181d22"
_TEXT = "#c9d3dc"
_MUTED = "#6b7a88"
_CAMERA = "#f0a03c"
_PATH = "#4fb3ff"


def _scene_bounds(plan: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    for obj in plan["objects"]:
        radius = max(obj["size"][0], obj["size"][1]) / 2.0
        for key in obj["keys"]:
            xs.extend([key["position"][0] - radius, key["position"][0] + radius])
            ys.extend([key["position"][1] - radius, key["position"][1] + radius])

    for key in plan["camera"]["keys"]:
        xs.append(key["position"][0])
        ys.append(key["position"][1])

    if not xs or not ys:
        return (-MIN_EXTENT_M, -MIN_EXTENT_M, MIN_EXTENT_M, MIN_EXTENT_M)

    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    if max_x - min_x < MIN_EXTENT_M:
        mid = (min_x + max_x) / 2.0
        min_x, max_x = mid - MIN_EXTENT_M / 2, mid + MIN_EXTENT_M / 2
    if max_y - min_y < MIN_EXTENT_M:
        mid = (min_y + max_y) / 2.0
        min_y, max_y = mid - MIN_EXTENT_M / 2, mid + MIN_EXTENT_M / 2
    return (min_x, min_y, max_x, max_y)


def _hex(color: list[float]) -> str:
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in color[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(plan: dict[str, Any], width_px: int = DEFAULT_WIDTH_PX) -> str:
    min_x, min_y, max_x, max_y = _scene_bounds(plan)
    span_x = max_x - min_x
    span_y = max_y - min_y
    scale = (width_px - 2 * MARGIN_PX) / span_x
    height_px = int(span_y * scale + 2 * MARGIN_PX)

    def to_px(x: float, y: float) -> tuple[float, float]:
        # SVG y grows downward; world +Y is north, so it is flipped here.
        return (MARGIN_PX + (x - min_x) * scale, MARGIN_PX + (max_y - y) * scale)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" font-family="ui-monospace,Menlo,Consolas,monospace">',
        f'<rect width="100%" height="100%" fill="{_BG}"/>',
        f'<rect x="{MARGIN_PX}" y="{MARGIN_PX}" width="{width_px - 2 * MARGIN_PX}" '
        f'height="{height_px - 2 * MARGIN_PX}" fill="{_GROUND}"/>',
    ]

    # One-metre grid, labelled every five.
    start_x = math.floor(min_x)
    while start_x <= max_x:
        px, _ = to_px(start_x, 0)
        opacity = 0.9 if start_x % 5 == 0 else 0.35
        parts.append(
            f'<line x1="{px:.1f}" y1="{MARGIN_PX}" x2="{px:.1f}" y2="{height_px - MARGIN_PX}" '
            f'stroke="{_GRID}" stroke-width="1" opacity="{opacity}"/>'
        )
        start_x += 1
    start_y = math.floor(min_y)
    while start_y <= max_y:
        _, py = to_px(0, start_y)
        opacity = 0.9 if start_y % 5 == 0 else 0.35
        parts.append(
            f'<line x1="{MARGIN_PX}" y1="{py:.1f}" x2="{width_px - MARGIN_PX}" y2="{py:.1f}" '
            f'stroke="{_GRID}" stroke-width="1" opacity="{opacity}"/>'
        )
        start_y += 1

    # Camera path.
    cam_keys = plan["camera"]["keys"]
    if len(cam_keys) > 1:
        points = " ".join(
            "{:.1f},{:.1f}".format(*to_px(k["position"][0], k["position"][1])) for k in cam_keys
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{_CAMERA}" '
            f'stroke-width="1.5" stroke-dasharray="5 4" opacity="0.85"/>'
        )

    # Objects: footprint, heading tick, motion path.
    for obj in plan["objects"]:
        keys = obj["keys"]
        color = _hex(obj["color"])
        radius_m = max(obj["size"][0], obj["size"][1]) / 2.0

        if len(keys) > 1:
            path = " ".join(
                "{:.1f},{:.1f}".format(*to_px(k["position"][0], k["position"][1])) for k in keys
            )
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{_PATH}" '
                f'stroke-width="2" opacity="0.7"/>'
            )

        for index, key in enumerate(keys):
            is_last = index == len(keys) - 1
            cx, cy = to_px(key["position"][0], key["position"][1])
            r_px = max(3.0, radius_m * scale)
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r_px:.1f}" fill="{color}" '
                f'fill-opacity="{0.85 if is_last else 0.25}" stroke="{color}" stroke-width="1.5"/>'
            )
            if is_last:
                yaw = math.radians(key["yaw"])
                fx, fy = -math.sin(yaw), math.cos(yaw)
                tx, ty = to_px(
                    key["position"][0] + fx * (radius_m + 0.9),
                    key["position"][1] + fy * (radius_m + 0.9),
                )
                parts.append(
                    f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" '
                    f'stroke="{_TEXT}" stroke-width="2"/>'
                )
                parts.append(
                    f'<text x="{cx:.1f}" y="{cy - r_px - 7:.1f}" fill="{_TEXT}" font-size="12" '
                    f'text-anchor="middle">{_esc(obj["id"])}</text>'
                )

    # Camera start marker.
    if cam_keys:
        cx, cy = to_px(cam_keys[0]["position"][0], cam_keys[0]["position"][1])
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{_CAMERA}"/>'
            f'<text x="{cx:.1f}" y="{cy - 11:.1f}" fill="{_CAMERA}" font-size="12" '
            f'text-anchor="middle">camera</text>'
        )

    env = plan["environment"]
    subtitle = (
        f'{env["preset"]} · {env["time_of_day"]} · {env.get("weather", "clear")} · '
        f'{plan["duration"]}s @ {plan["fps"]}fps · {plan["render"]["engine"]}'
    )
    parts.append(
        f'<text x="{MARGIN_PX}" y="{MARGIN_PX - 22:.0f}" fill="{_TEXT}" font-size="15">'
        f'{_esc(plan["title"])}</text>'
        f'<text x="{MARGIN_PX}" y="{MARGIN_PX - 7:.0f}" fill="{_MUTED}" font-size="11">'
        f'{_esc(subtitle)}</text>'
        f'<text x="{width_px - MARGIN_PX}" y="{height_px - 18:.0f}" fill="{_MUTED}" font-size="11" '
        f'text-anchor="end">top-down · 1m grid · +Y north</text>'
    )

    parts.append("</svg>")
    return "\n".join(parts)
