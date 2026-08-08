"""Trusted blueprint registry and deterministic semantic retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BLUEPRINT_PATH = Path(__file__).resolve().parent.parent / "assets" / "blueprints.json"


@dataclass(frozen=True)
class Blueprint:
    id: str
    family: str
    variant: str
    category: str
    asset_id: str
    tags: frozenset[str]
    capabilities: frozenset[str]
    license: str
    version: int


@dataclass(frozen=True)
class BlueprintMatch:
    blueprint: Blueprint
    score: float
    reasons: tuple[str, ...]


class BlueprintRegistry:
    def __init__(self, blueprints: list[Blueprint]) -> None:
        self._blueprints = tuple(blueprints)

    def __iter__(self):
        return iter(self._blueprints)

    def require(self, blueprint_id: str) -> Blueprint:
        found = next((item for item in self._blueprints if item.id == blueprint_id), None)
        if found is None:
            raise KeyError(f"unknown blueprint {blueprint_id!r}")
        return found

    def search(self, spec: dict[str, Any], approval_counts: dict[str, int] | None = None) -> list[BlueprintMatch]:
        approvals = approval_counts or {}
        requested_tags = {
            str(tag).casefold() for tag in spec.get("attributes", {}).get("style_tags", [])
        }
        requested_tags.update(str(color).casefold() for color in spec.get("attributes", {}).get("colors", []))
        matches: list[BlueprintMatch] = []
        for blueprint in self._blueprints:
            score = 0.0
            reasons: list[str] = []
            if blueprint.category == spec.get("category"):
                score += 20.0
                reasons.append("category")
            else:
                score -= 20.0
            if blueprint.family == spec.get("family"):
                score += 40.0
                reasons.append("family")
            if blueprint.variant == spec.get("variant"):
                score += 25.0
                reasons.append("variant")
            overlap = requested_tags & blueprint.tags
            if overlap:
                score += 3.0 * len(overlap)
                reasons.append("tags:" + ",".join(sorted(overlap)))
            approved = approvals.get(blueprint.id, 0)
            if approved:
                score += min(5.0, approved * 0.5)
                reasons.append(f"approved:{approved}")
            matches.append(BlueprintMatch(blueprint, score, tuple(reasons)))
        return sorted(matches, key=lambda match: (-match.score, match.blueprint.id))


def load_blueprints(path: str | Path | None = None) -> BlueprintRegistry:
    source = Path(path) if path is not None else DEFAULT_BLUEPRINT_PATH
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    blueprints = [
        Blueprint(
            id=entry["id"], family=entry["family"], variant=entry["variant"],
            category=entry["category"], asset_id=entry["asset_id"],
            tags=frozenset(str(tag).casefold() for tag in entry.get("tags", [])),
            capabilities=frozenset(entry.get("capabilities", [])),
            license=entry.get("license", "unknown"), version=int(entry.get("version", 1)),
        )
        for entry in raw.get("blueprints", [])
    ]
    ids = [blueprint.id for blueprint in blueprints]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate blueprint id in {source}")
    return BlueprintRegistry(blueprints)

