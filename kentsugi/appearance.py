"""Safe colour parsing for scene and asset appearance data.

Scene documents may use a small, predictable vocabulary of colour names, a
hex colour, or normalised RGB/RGBA components.  Everything is resolved to an
RGBA tuple before a render plan reaches Blender, so the backend never has to
interpret user-facing colour language.
"""

from __future__ import annotations

import re
from typing import Any


NAMED_COLORS: dict[str, tuple[float, float, float, float]] = {
    "black": (0.0, 0.0, 0.0, 1.0),
    "white": (1.0, 1.0, 1.0, 1.0),
    "gray": (0.5, 0.5, 0.5, 1.0),
    "grey": (0.5, 0.5, 0.5, 1.0),
    "silver": (0.75, 0.75, 0.75, 1.0),
    "red": (1.0, 0.0, 0.0, 1.0),
    "dark_red": (0.55, 0.0, 0.0, 1.0),
    "crimson": (0.86, 0.08, 0.24, 1.0),
    "orange": (1.0, 0.45, 0.0, 1.0),
    "yellow": (1.0, 1.0, 0.0, 1.0),
    "gold": (1.0, 0.72, 0.0, 1.0),
    "green": (0.0, 0.5, 0.0, 1.0),
    "lime": (0.0, 1.0, 0.0, 1.0),
    "olive": (0.5, 0.5, 0.0, 1.0),
    "teal": (0.0, 0.5, 0.5, 1.0),
    "cyan": (0.0, 1.0, 1.0, 1.0),
    "blue": (0.0, 0.0, 1.0, 1.0),
    "navy": (0.0, 0.0, 0.5, 1.0),
    "purple": (0.5, 0.0, 0.5, 1.0),
    "magenta": (1.0, 0.0, 1.0, 1.0),
    "pink": (1.0, 0.41, 0.71, 1.0),
    "brown": (0.45, 0.24, 0.10, 1.0),
    "beige": (0.96, 0.91, 0.76, 1.0),
}

_HEX_COLOR = re.compile(r"^#(?P<digits>[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _normalise_name(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def resolve_color(value: Any) -> tuple[float, float, float, float]:
    """Resolve a named, hex, RGB, or RGBA colour to normalised RGBA."""
    if isinstance(value, str):
        name = _normalise_name(value)
        if name in NAMED_COLORS:
            return NAMED_COLORS[name]

        match = _HEX_COLOR.match(value.strip())
        if match:
            digits = match.group("digits")
            if len(digits) in (3, 4):
                digits = "".join(ch * 2 for ch in digits)
            if len(digits) == 6:
                digits += "ff"
            return tuple(int(digits[i:i + 2], 16) / 255.0 for i in range(0, 8, 2))  # type: ignore[return-value]

        names = ", ".join(sorted(NAMED_COLORS))
        raise ValueError(f"unknown colour {value!r}; use one of: {names}, or a hex colour")

    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ValueError("colour must be a name, hex string, or three/four normalised numbers")

    components: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError("colour components must be numbers")
        number = float(component)
        if not 0.0 <= number <= 1.0:
            raise ValueError("colour components must be between 0 and 1")
        components.append(number)
    if len(components) == 3:
        components.append(1.0)
    return tuple(components)  # type: ignore[return-value]


def color_list(value: Any) -> list[float]:
    """JSON-friendly form of :func:`resolve_color`."""
    return list(resolve_color(value))
