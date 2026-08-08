"""Semantic object requests shared by deterministic and external AI planners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .appearance import NAMED_COLORS

OBJECT_SPEC_VERSION = "0.1.0"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
CATEGORIES = frozenset({"character", "product", "prop", "vehicle"})
COLORS = tuple(sorted(NAMED_COLORS))


@dataclass(frozen=True)
class ObjectSpecIssue:
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


def plan_object_spec(prompt: str) -> dict[str, Any]:
    """Deterministic first planner; an AI adapter can later emit the same contract."""
    text = " ".join(prompt.strip().split())
    if not text:
        raise ValueError("prompt must not be empty")
    if len(text) > 2_000:
        raise ValueError("prompt must be at most 2,000 characters")
    lowered = text.casefold()

    if any(word in lowered for word in ("monster", "energy drink", "energy can")):
        category, family, variant = "product", "can", "tall_energy"
    elif any(word in lowered for word in ("soda", "soft drink", "drink can", "can drink")):
        category, family, variant = "product", "can", "standard_soda"
    elif any(word in lowered for word in ("perfume", "fragrance", "bottle")):
        category, family, variant = "product", "bottle", "luxury"
    elif any(word in lowered for word in ("car", "bugatti", "vehicle", "automobile")):
        category, family = "vehicle", "car"
        variant = "hypercar" if any(word in lowered for word in ("bugatti", "hypercar", "supercar")) else "generic"
    elif any(word in lowered for word in ("boy", "girl", "person", "human", "character")):
        category, family, variant = "character", "human", "stylized"
    else:
        category, family, variant = "prop", "generic", "generic"

    colors = [
        color
        for color in COLORS
        if re.search(rf"\b{re.escape(color).replace('_', r'[\s_-]+')}\b", lowered)
    ]
    brand = next((name for name in ("bugatti", "monster") if name in lowered), None)
    style_tags = sorted({
        word for word in (
            "advertisement", "animated", "cinematic", "energy", "hypercar", "luxury",
            "realistic", "slim", "sports", "stylized", "tall",
        ) if word in lowered
    })
    return {
        "object_spec_version": OBJECT_SPEC_VERSION,
        "name": text[:120],
        "category": category,
        "family": family,
        "variant": variant,
        "attributes": {
            "colors": colors,
            "style_tags": style_tags,
            **({"brand_reference": brand} if brand else {}),
        },
        "requirements": {
            "animation_ready": any(word in lowered for word in ("animate", "animated", "driving", "walking")),
            "texture_required": bool(brand or any(word in lowered for word in ("label", "logo", "texture"))),
            "exact_brand": any(word in lowered for word in ("exact", "official logo", "identical")),
        },
    }


def validate_object_spec(spec: Any) -> list[ObjectSpecIssue]:
    issues: list[ObjectSpecIssue] = []
    if not isinstance(spec, dict):
        return [ObjectSpecIssue("", "bad_type", "ObjectSpec must be an object")]
    allowed = {"object_spec_version", "name", "category", "family", "variant", "attributes", "requirements"}
    required = allowed
    for key in spec:
        if key not in allowed:
            issues.append(ObjectSpecIssue(key, "unknown_field", "field is not allowed"))
    for key in required:
        if key not in spec:
            issues.append(ObjectSpecIssue(key, "missing", "field is required"))
    if spec.get("object_spec_version") != OBJECT_SPEC_VERSION:
        issues.append(ObjectSpecIssue("object_spec_version", "bad_version", f"expected {OBJECT_SPEC_VERSION!r}"))
    if not isinstance(spec.get("name"), str) or not spec.get("name", "").strip():
        issues.append(ObjectSpecIssue("name", "bad_type", "must be a non-empty string"))
    elif len(spec["name"]) > 120:
        issues.append(ObjectSpecIssue("name", "too_long", "must be at most 120 characters"))
    if spec.get("category") not in CATEGORIES:
        issues.append(ObjectSpecIssue("category", "bad_enum", f"expected one of {sorted(CATEGORIES)}"))
    for field in ("family", "variant"):
        if not isinstance(spec.get(field), str) or not ID_RE.fullmatch(spec.get(field, "")):
            issues.append(ObjectSpecIssue(field, "bad_id", "must be lowercase snake_case"))
    attributes = spec.get("attributes")
    if not isinstance(attributes, dict):
        issues.append(ObjectSpecIssue("attributes", "bad_type", "must be an object"))
    else:
        allowed_attributes = {"colors", "style_tags", "finish", "brand_reference"}
        for key in attributes:
            if key not in allowed_attributes:
                issues.append(ObjectSpecIssue(f"attributes.{key}", "unknown_field", "field is not allowed"))
        for field, limit in (("colors", 6), ("style_tags", 12)):
            value = attributes.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                issues.append(ObjectSpecIssue(f"attributes.{field}", "bad_type", "must be an array of strings"))
            elif len(value) > limit:
                issues.append(ObjectSpecIssue(f"attributes.{field}", "too_many", f"must contain at most {limit} values"))
        for field in ("finish", "brand_reference"):
            if field in attributes and not isinstance(attributes[field], str):
                issues.append(ObjectSpecIssue(f"attributes.{field}", "bad_type", "must be a string"))
    requirements = spec.get("requirements")
    if not isinstance(requirements, dict):
        issues.append(ObjectSpecIssue("requirements", "bad_type", "must be an object"))
    else:
        allowed_requirements = {"animation_ready", "texture_required", "exact_brand"}
        for key in requirements:
            if key not in allowed_requirements:
                issues.append(ObjectSpecIssue(f"requirements.{key}", "unknown_field", "field is not allowed"))
        for field in ("animation_ready", "texture_required"):
            if not isinstance(requirements.get(field), bool):
                issues.append(ObjectSpecIssue(f"requirements.{field}", "bad_type", "must be boolean"))
        if "exact_brand" in requirements and not isinstance(requirements["exact_brand"], bool):
            issues.append(ObjectSpecIssue("requirements.exact_brand", "bad_type", "must be boolean"))
    return issues
