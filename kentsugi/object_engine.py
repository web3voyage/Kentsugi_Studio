"""Object-generation orchestration from intent through approval memory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import KENTSUGI_VERSION
from .blueprints import Blueprint, BlueprintMatch, BlueprintRegistry, load_blueprints
from .catalog import Catalog, load_catalog
from .document import normalize
from .knowledge import KnowledgeLibrary
from .object_spec import plan_object_spec, validate_object_spec
from .validate import errors, validate


@dataclass(frozen=True)
class EngineCheck:
    stage: str
    severity: str
    code: str
    message: str


@dataclass
class PreparedObject:
    prompt: str
    spec: dict[str, Any]
    blueprint: Blueprint
    candidates: list[BlueprintMatch]
    scene: dict[str, Any]
    checks: list[EngineCheck]
    attempts: int

    def check_dicts(self) -> list[dict[str, Any]]:
        return [asdict(check) for check in self.checks]


def _scene_for(prompt: str, spec: dict[str, Any], blueprint: Blueprint, catalog: Catalog) -> dict[str, Any]:
    asset = catalog.require(blueprint.asset_id)
    largest = max(asset.size)
    distance = max(0.65, largest * (4.0 if largest < 0.5 else 2.0))
    height = max(0.16, asset.height * 0.95)
    shot = "medium"
    object_spec: dict[str, Any] = {
        "id": "generated_object", "asset": asset.id,
        "placement": {"type": "at", "position": [0, 0]},
        "facing": 180,
    }
    colors = spec.get("attributes", {}).get("colors", [])
    if isinstance(colors, list) and colors:
        object_spec["appearance"] = {"color": colors[0]}
    return normalize(
        {
            "kentsugi_version": KENTSUGI_VERSION,
            "title": prompt[:200],
            "environment": {
                "preset": "studio", "time_of_day": "dusk",
                "weather": "clear", "ground_material": "concrete",
            },
            "objects": [object_spec],
            "actions": [],
            "camera": {
                "type": "orbit", "target": "generated_object", "position": "front",
                "distance": distance, "height": height, "shot": shot,
            },
            "render": {"duration": 2.0, "fps": 12, "resolution": [640, 360], "quality": "preview"},
        }
    )


def _candidate_checks(spec: dict[str, Any], blueprint: Blueprint, catalog: Catalog, scene: dict[str, Any]) -> list[EngineCheck]:
    checks: list[EngineCheck] = []
    if blueprint.category != spec["category"]:
        checks.append(EngineCheck("blueprint_search", "error", "category_mismatch", "blueprint category does not match ObjectSpec"))
    asset = catalog.get(blueprint.asset_id)
    if asset is None:
        checks.append(EngineCheck("blueprint_search", "error", "missing_asset", f"catalog asset {blueprint.asset_id!r} is missing"))
        return checks
    if any(value <= 0 for value in asset.size):
        checks.append(EngineCheck("geometry", "error", "invalid_bounds", "asset dimensions must be positive"))
    else:
        checks.append(EngineCheck("geometry", "pass", "valid_bounds", "asset dimensions are positive"))
    camera_distance = float(scene.get("camera", {}).get("distance", 0.0))
    if camera_distance < asset.height * 3.5:
        checks.append(EngineCheck("render", "error", "camera_too_close", "camera lacks safe framing margin"))
    else:
        checks.append(EngineCheck("render", "pass", "framing_margin", "camera has safe full-object framing margin"))
    source = asset.source
    if source.get("type") == "recipe_asset":
        recipe = Path(str(source.get("recipe", "")))
        if not recipe.is_absolute():
            recipe = Path(__file__).resolve().parent.parent / "assets" / recipe
        if recipe.is_file() and recipe.suffix.casefold() == ".json":
            checks.append(EngineCheck("geometry", "pass", "recipe_available", "parametric recipe is available"))
        else:
            checks.append(EngineCheck("geometry", "error", "missing_recipe", f"recipe is unavailable: {recipe}"))
    requirements = spec.get("requirements", {})
    if requirements.get("animation_ready") and not (
        {"rigged", "wheel_rig", "animation_ready"} & blueprint.capabilities
    ):
        checks.append(EngineCheck(
            "capability", "error", "animation_unavailable",
            "request requires animation-ready geometry but this blueprint has no compatible rig",
        ))
    if requirements.get("texture_required") and not (
        {"pbr_material", "uv_mapping"} & blueprint.capabilities
    ):
        checks.append(EngineCheck(
            "capability", "warning", "texture_work_required",
            "selected blueprint needs an additional texture-generation or licensed-artwork pass",
        ))
    scene_errors = errors(validate(scene, catalog))
    if scene_errors:
        checks.extend(EngineCheck("scene", "error", issue.code, issue.message) for issue in scene_errors)
    else:
        checks.append(EngineCheck("scene", "pass", "valid_scene", "compiled scene is renderable"))
    if spec.get("requirements", {}).get("exact_brand"):
        checks.append(EngineCheck(
            "rights", "warning", "brand_artwork_required",
            "exact branding requires approved artwork and usage rights",
        ))
    elif spec.get("attributes", {}).get("brand_reference"):
        checks.append(EngineCheck(
            "rights", "warning", "brand_inspired_only",
            "using structural proportions and original graphics until licensed brand artwork is supplied",
        ))
    return checks


def artifact_checks(path: str | Path | None) -> list[EngineCheck]:
    if path is None:
        return [EngineCheck("render", "warning", "not_rendered", "render was not requested")]
    artifact = Path(path)
    if not artifact.is_file() or artifact.stat().st_size == 0:
        return [EngineCheck("render", "error", "missing_artifact", "renderer produced no artifact")]
    header = artifact.read_bytes()[:12]
    valid = artifact.suffix.casefold() == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")
    valid = valid or (artifact.suffix.casefold() == ".mp4" and b"ftyp" in header)
    if not valid:
        return [EngineCheck("render", "error", "bad_artifact", "artifact signature does not match PNG or MP4")]
    return [EngineCheck("render", "pass", "valid_artifact", f"rendered artifact is {artifact.stat().st_size} bytes")]


class ObjectEngine:
    def __init__(
        self, *, catalog: Catalog | None = None, registry: BlueprintRegistry | None = None,
        knowledge: KnowledgeLibrary | None = None,
        planner: Callable[[str], dict[str, Any]] = plan_object_spec,
    ) -> None:
        self.catalog = catalog or load_catalog()
        self.registry = registry or load_blueprints()
        self.knowledge = knowledge or KnowledgeLibrary()
        self.planner = planner

    def prepare(self, prompt: str, *, max_attempts: int = 3) -> PreparedObject:
        spec = self.planner(prompt)
        issues = validate_object_spec(spec)
        if issues:
            raise ValueError("invalid ObjectSpec: " + "; ".join(str(issue) for issue in issues))
        candidates = self.registry.search(spec, self.knowledge.approval_counts())
        if not candidates:
            raise ValueError("blueprint registry is empty")
        last_checks: list[EngineCheck] = []
        for attempt, match in enumerate(candidates[:max(1, max_attempts)], start=1):
            if self.catalog.get(match.blueprint.asset_id) is None:
                last_checks = [EngineCheck(
                    "blueprint_search", "error", "missing_asset",
                    f"catalog asset {match.blueprint.asset_id!r} is missing",
                )]
                continue
            scene = _scene_for(prompt, spec, match.blueprint, self.catalog)
            last_checks = _candidate_checks(spec, match.blueprint, self.catalog, scene)
            if not any(check.severity == "error" for check in last_checks):
                return PreparedObject(prompt, spec, match.blueprint, candidates, scene, last_checks, attempt)
        detail = "; ".join(check.message for check in last_checks if check.severity == "error")
        raise ValueError(f"no blueprint passed checks after {min(len(candidates), max_attempts)} attempt(s): {detail}")

    def finalize(self, run: PreparedObject, *, artifact: str | Path | None = None, approved: bool = False) -> Path:
        render_checks = artifact_checks(artifact)
        all_checks = run.checks + render_checks
        if approved and any(check.severity == "error" for check in all_checks):
            raise ValueError("cannot approve a generation with failed checks")
        if approved and artifact is None:
            raise ValueError("cannot approve before a rendered artifact exists")
        blueprint = {
            "id": run.blueprint.id, "version": run.blueprint.version,
            "asset_id": run.blueprint.asset_id, "license": run.blueprint.license,
        }
        return self.knowledge.record(
            prompt=run.prompt, spec=run.spec, blueprint=blueprint, scene=run.scene,
            checks=[asdict(check) for check in all_checks],
            artifact=str(Path(artifact).resolve()) if artifact is not None else None,
            approved=approved,
        )
