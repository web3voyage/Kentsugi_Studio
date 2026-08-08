"""The asset catalog.

Resolving "a red car" to something renderable is the part of this system most
likely to produce garbage, so every asset declares the metadata the solver
needs: real-world size, pivot convention, collision radius, animation clips.

Asset ids are catalog keys, never paths. That is the security boundary — a
model that emits ``../../etc/passwd`` as an asset id gets a lookup miss, not a
file read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "assets" / "catalog.json"

CHARACTER_KINDS = frozenset({"character"})


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    kind: str
    source: dict[str, Any]
    size: tuple[float, float, float]
    """Bounding size in metres, (x, y, z), before per-object scale."""
    pivot: str
    """Only 'base_center' is supported in v0.1: origin at the bottom centre."""
    radius: float
    """Planar collision radius in metres."""
    color: tuple[float, float, float, float]
    clips: tuple[str, ...] = ()
    walk_speed: float = 1.4
    static: bool = False
    """Static objects are never displaced by collision resolution."""
    emissive: dict[str, Any] | None = None
    license: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def height(self) -> float:
        return self.size[2]


class Catalog:
    def __init__(self, assets: dict[str, Asset]) -> None:
        self._assets = assets

    def __contains__(self, asset_id: object) -> bool:
        return asset_id in self._assets

    def __len__(self) -> int:
        return len(self._assets)

    def __iter__(self):
        return iter(self._assets.values())

    def get(self, asset_id: str) -> Asset | None:
        return self._assets.get(asset_id)

    def require(self, asset_id: str) -> Asset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise KeyError(f"unknown asset {asset_id!r}")
        return asset

    def ids(self) -> list[str]:
        return sorted(self._assets)


def load_catalog(path: str | Path | None = None) -> Catalog:
    path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    assets: dict[str, Asset] = {}
    for entry in raw.get("assets", []):
        known = {
            "id", "name", "kind", "source", "size", "pivot", "radius", "color",
            "clips", "walk_speed", "static", "emissive", "license",
        }
        asset = Asset(
            id=entry["id"],
            name=entry.get("name", entry["id"]),
            kind=entry.get("kind", "prop"),
            source=entry.get("source", {"type": "primitive", "shape": "box"}),
            size=tuple(float(v) for v in entry["size"]),  # type: ignore[arg-type]
            pivot=entry.get("pivot", "base_center"),
            radius=float(entry.get("radius", 0.5)),
            color=tuple(float(v) for v in entry.get("color", [0.6, 0.6, 0.6, 1.0])),  # type: ignore[arg-type]
            clips=tuple(entry.get("clips", ())),
            walk_speed=float(entry.get("walk_speed", 1.4)),
            static=bool(entry.get("static", False)),
            emissive=entry.get("emissive"),
            license=entry.get("license", "unknown"),
            extra={k: v for k, v in entry.items() if k not in known},
        )
        if asset.pivot != "base_center":
            raise ValueError(
                f"asset {asset.id!r}: pivot {asset.pivot!r} is not supported in v0.1"
            )
        if asset.id in assets:
            raise ValueError(f"duplicate asset id {asset.id!r} in {path}")
        assets[asset.id] = asset

    return Catalog(assets)
