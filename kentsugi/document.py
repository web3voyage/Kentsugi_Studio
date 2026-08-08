"""Loading, normalising, and hashing scene documents.

A document is a plain ``dict``. That is a choice, not laziness: documents get
diffed, patched, versioned, and hashed, and dataclasses would put a
serialisation layer in the middle of every one of those operations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import KENTSUGI_VERSION

# Defaults live here rather than only in the JSON Schema because the schema is
# what we hand to the model, while this is what the rest of the code relies on.
# Keep the two in sync; tests/test_validate.py checks that they agree.
_OBJECT_DEFAULTS: dict[str, Any] = {"scale": 1.0}
_ACTION_DEFAULTS: dict[str, Any] = {"start": 0.0, "duration": 4.0, "stop_distance": 1.5}
_CAMERA_DEFAULTS: dict[str, Any] = {
    "position": "behind",
    "distance": 6.0,
    "height": 1.7,
    "shot": "medium",
}
_RENDER_DEFAULTS: dict[str, Any] = {
    "fps": 24,
    "resolution": [1280, 720],
    "quality": "preview",
}
_ENVIRONMENT_DEFAULTS: dict[str, Any] = {"weather": "clear", "ground_material": "concrete"}

FOCAL_LENGTHS_MM: dict[str, float] = {"wide": 24.0, "medium": 50.0, "close_up": 85.0}


def new_document(title: str = "Untitled shot") -> dict[str, Any]:
    """A minimal document that validates and renders (an empty lit ground plane)."""
    return {
        "kentsugi_version": KENTSUGI_VERSION,
        "title": title,
        "environment": {"preset": "void", "time_of_day": "day"},
        "objects": [],
        "actions": [],
        "camera": {"type": "static", "distance": 10.0, "height": 2.0},
        "render": {"duration": 2.0},
    }


def load_document(path: str | Path) -> dict[str, Any]:
    """Read and normalise a document. Raises on unreadable or non-JSON files."""
    # utf-8-sig, not utf-8: Windows editors and PowerShell routinely emit a BOM,
    # and json.loads rejects it outright. Files without a BOM are unaffected.
    text = Path(path).read_text(encoding="utf-8-sig")
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level of a scene document must be an object")
    return normalize(doc)


def save_document(doc: dict[str, Any], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults so downstream code never writes ``.get(k, fallback)``.

    Normalising does not validate. A malformed document survives this function
    unchanged in its malformed parts; the validator is what rejects it.
    """
    out = json.loads(json.dumps(doc))  # deep copy, and rejects non-JSON values early

    out.setdefault("kentsugi_version", KENTSUGI_VERSION)
    out.setdefault("objects", [])
    out.setdefault("actions", [])

    if isinstance(out.get("environment"), dict):
        for key, value in _ENVIRONMENT_DEFAULTS.items():
            out["environment"].setdefault(key, value)

    if isinstance(out.get("objects"), list):
        for obj in out["objects"]:
            if isinstance(obj, dict):
                for key, value in _OBJECT_DEFAULTS.items():
                    obj.setdefault(key, value)

    if isinstance(out.get("actions"), list):
        for action in out["actions"]:
            if isinstance(action, dict):
                for key, value in _ACTION_DEFAULTS.items():
                    action.setdefault(key, value)

    if isinstance(out.get("camera"), dict):
        for key, value in _CAMERA_DEFAULTS.items():
            out["camera"].setdefault(key, value)

    if isinstance(out.get("render"), dict):
        for key, value in _RENDER_DEFAULTS.items():
            out["render"].setdefault(key, value)

    return out


def content_hash(doc: dict[str, Any]) -> str:
    """Stable hash of a document, for render caching.

    Canonicalised with sorted keys so that key ordering — which the model
    varies between calls — does not invalidate a cached render.
    """
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
