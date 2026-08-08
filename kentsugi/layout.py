"""The layout solver: relations in, coordinates out.

Language models are unreliable at absolute 3D placement — ask for coordinates
and you get interpenetrating geometry and objects floating off the ground. So
the document says "9 metres ahead of the character" and this module computes
where that actually is, then guarantees the invariants the model cannot:
everything sits on the ground, and nothing overlaps.

World convention (Blender's): +X east, +Y north, +Z up. Yaw is in degrees
about +Z, measured so that yaw 0 faces +Y (north) and positive yaw turns
counter-clockwise seen from above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .catalog import Asset, Catalog

MAX_RELAXATION_PASSES = 8
"""Facing and placement are mutually dependent (A faces B, B is placed ahead of
A). Rather than reject that as a cycle -- it is a natural way to describe a
scene -- positions are relaxed to a fixed point."""

CONVERGENCE_EPSILON = 1e-6
COLLISION_PASSES = 24


def forward(yaw_deg: float) -> tuple[float, float]:
    """Unit vector an object faces at the given yaw. yaw 0 -> +Y."""
    r = math.radians(yaw_deg)
    return (-math.sin(r), math.cos(r))


def right(yaw_deg: float) -> tuple[float, float]:
    """Unit vector to an object's right. yaw 0 -> +X."""
    r = math.radians(yaw_deg)
    return (math.cos(r), math.sin(r))


def yaw_towards(origin: tuple[float, float], target: tuple[float, float]) -> float:
    """Yaw in degrees that points `origin` at `target`. Returns 0 if coincident."""
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return 0.0
    return math.degrees(math.atan2(-dx, dy))


WORLD_DIRECTIONS: dict[str, tuple[float, float]] = {
    "north": (0.0, 1.0),
    "south": (0.0, -1.0),
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
}


@dataclass
class Placed:
    id: str
    asset: Asset
    position: tuple[float, float]
    """Ground-plane (x, y). Height comes from the pivot: base_center sits at z=0."""
    yaw: float
    scale: float = 1.0
    pinned: bool = False
    """Explicitly positioned or static objects are never moved by collision resolution."""

    @property
    def radius(self) -> float:
        return self.asset.radius * self.scale

    @property
    def height(self) -> float:
        return self.asset.height * self.scale

    def origin(self) -> tuple[float, float, float]:
        return (self.position[0], self.position[1], 0.0)


@dataclass
class Layout:
    objects: dict[str, Placed] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def get(self, object_id: str) -> Placed | None:
        return self.objects.get(object_id)

    def require(self, object_id: str) -> Placed:
        return self.objects[object_id]


def solve(doc: dict[str, Any], catalog: Catalog) -> Layout:
    """Resolve every object's position and yaw.

    Assumes the document already validated. Unknown assets or references raise
    KeyError rather than being papered over -- a solver that silently invents a
    position for a missing reference produces scenes nobody can debug.
    """
    layout = Layout()
    specs = [o for o in doc.get("objects", []) if isinstance(o, dict)]

    for spec in specs:
        asset = catalog.require(spec["asset"])
        placement = spec["placement"]
        pinned = placement.get("type") == "at" or asset.static
        layout.objects[spec["id"]] = Placed(
            id=spec["id"],
            asset=asset,
            position=(0.0, 0.0),
            yaw=0.0,
            scale=float(spec.get("scale", 1.0)),
            pinned=pinned,
        )

    order = _placement_order(specs)

    # Relax positions and facings together until they stop moving.
    converged = False
    for _ in range(MAX_RELAXATION_PASSES):
        before = {oid: p.position for oid, p in layout.objects.items()}
        _apply_placements(specs, order, layout)
        _apply_facings(specs, layout)
        delta = max(
            (_distance(before[oid], p.position) for oid, p in layout.objects.items()),
            default=0.0,
        )
        if delta < CONVERGENCE_EPSILON:
            converged = True
            break

    if not converged:
        layout.warnings.append(
            f"layout did not converge after {MAX_RELAXATION_PASSES} passes; "
            "positions may be approximate (usually caused by a facing/placement loop)"
        )

    _resolve_collisions(layout)
    return layout


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _placement_order(specs: list[dict[str, Any]]) -> list[str]:
    """Topological order over placement references, so refs resolve before dependants."""
    by_id = {s["id"]: s for s in specs}
    order: list[str] = []
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(node_id: str) -> None:
        mark = state.get(node_id)
        if mark == 1 or mark == 0:
            # Done, or a cycle the validator already reported -- stop either way.
            return
        state[node_id] = 0
        spec = by_id.get(node_id)
        if spec is not None:
            placement = spec.get("placement", {})
            if placement.get("type") == "relative":
                ref = placement.get("ref")
                if isinstance(ref, str) and ref in by_id:
                    visit(ref)
        state[node_id] = 1
        order.append(node_id)

    for spec in specs:
        visit(spec["id"])
    return order


def _apply_placements(specs: list[dict[str, Any]], order: list[str], layout: Layout) -> None:
    by_id = {s["id"]: s for s in specs}
    for object_id in order:
        spec = by_id.get(object_id)
        if spec is None:
            continue
        placed = layout.objects[object_id]
        placement = spec["placement"]

        if placement["type"] == "at":
            x, y = placement["position"]
            placed.position = (float(x), float(y))
            continue

        ref = layout.objects.get(placement["ref"])
        if ref is None:
            continue

        direction = placement["direction"]
        distance = float(placement["distance"])

        if direction in WORLD_DIRECTIONS:
            ux, uy = WORLD_DIRECTIONS[direction]
        elif direction == "ahead":
            ux, uy = forward(ref.yaw)
        elif direction == "behind":
            fx, fy = forward(ref.yaw)
            ux, uy = -fx, -fy
        elif direction == "right":
            ux, uy = right(ref.yaw)
        elif direction == "left":
            rx, ry = right(ref.yaw)
            ux, uy = -rx, -ry
        else:  # unreachable for validated documents
            ux, uy = 0.0, 1.0

        placed.position = (ref.position[0] + ux * distance, ref.position[1] + uy * distance)


def _apply_facings(specs: list[dict[str, Any]], layout: Layout) -> None:
    for spec in specs:
        facing = spec.get("facing")
        if facing is None:
            continue
        placed = layout.objects[spec["id"]]
        if isinstance(facing, str):
            target = layout.objects.get(facing)
            if target is not None:
                placed.yaw = yaw_towards(placed.position, target.position)
        else:
            placed.yaw = float(facing)


def _resolve_collisions(layout: Layout) -> None:
    """Push overlapping objects apart.

    Pinned objects (explicit coordinates, or assets flagged static) hold their
    ground and push others away. This is the invariant a language model reliably
    fails to maintain, so it is enforced here rather than requested in a prompt.
    """
    everything = list(layout.objects.values())

    for _ in range(COLLISION_PASSES):
        moved = False
        for i, a in enumerate(everything):
            for b in everything[i + 1:]:
                if a.pinned and b.pinned:
                    continue
                dx = b.position[0] - a.position[0]
                dy = b.position[1] - a.position[1]
                dist = math.hypot(dx, dy)
                min_dist = a.radius + b.radius
                if dist >= min_dist:
                    continue

                if dist < 1e-9:
                    # Exactly coincident: pick an arbitrary but deterministic axis.
                    dx, dy, dist = 1.0, 0.0, 1.0
                overlap = min_dist - dist
                nx, ny = dx / dist, dy / dist

                if a.pinned:
                    b.position = (b.position[0] + nx * overlap, b.position[1] + ny * overlap)
                elif b.pinned:
                    a.position = (a.position[0] - nx * overlap, a.position[1] - ny * overlap)
                else:
                    half = overlap / 2.0
                    a.position = (a.position[0] - nx * half, a.position[1] - ny * half)
                    b.position = (b.position[0] + nx * half, b.position[1] + ny * half)
                moved = True
        if not moved:
            break

    residual = [
        f"{a.id}/{b.id}"
        for i, a in enumerate(everything)
        for b in everything[i + 1:]
        if _distance(a.position, b.position) < a.radius + b.radius - 1e-6
    ]
    if residual:
        layout.warnings.append(
            "objects still overlap after collision resolution: " + ", ".join(residual)
        )
