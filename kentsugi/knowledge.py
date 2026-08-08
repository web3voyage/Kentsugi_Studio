"""Immutable, versioned records of object generations and approvals."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / ".kentsugi" / "knowledge"


class KnowledgeLibrary:
    def __init__(self, root: str | Path | None = None, *, clock: Callable[[], datetime] | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_KNOWLEDGE_PATH
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return records
        for path in sorted(self.root.glob("*/*/v*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def approval_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records():
            if record.get("status") != "approved":
                continue
            blueprint_id = record.get("blueprint", {}).get("id")
            if isinstance(blueprint_id, str):
                counts[blueprint_id] = counts.get(blueprint_id, 0) + 1
        return counts

    def record(
        self, *, prompt: str, spec: dict[str, Any], blueprint: dict[str, Any],
        scene: dict[str, Any], checks: list[dict[str, Any]], artifact: str | None,
        approved: bool,
    ) -> Path:
        family = str(spec["family"])
        variant = str(spec["variant"])
        directory = self.root / family / variant
        directory.mkdir(parents=True, exist_ok=True)
        versions = sorted(directory.glob("v*.json"))
        version = len(versions) + 1
        identity = hashlib.sha256(
            json.dumps({"prompt": prompt, "spec": spec, "blueprint": blueprint}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        value = {
            "knowledge_version": "0.1.0",
            "id": identity,
            "version": version,
            "created_at": self.clock().isoformat(),
            "status": "approved" if approved else "pending_review" if artifact else "pending_render",
            "prompt": prompt,
            "object_spec": spec,
            "blueprint": blueprint,
            "scene": scene,
            "checks": checks,
            "artifact": artifact,
        }
        destination = directory / f"v{version:04d}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination

