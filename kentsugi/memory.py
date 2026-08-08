"""Local, auditable memory and procedural-skill store.

Memory is intentionally boring JSONL.  It survives process restarts, is easy
to inspect or delete, and never becomes an execution channel.  Skills contain
declarative blueprint fragments; callers still validate them before compiling.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .asset_blueprint import validate_blueprint

DEFAULT_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".kentsugi" / "memory"
EVENTS_FILE = "events.jsonl"
SKILLS_FILE = "skills.json"
MAX_EVENT_BYTES = 64_000
MAX_SKILLS = 256


def _tokens(value: Any) -> set[str]:
    return {token.lower() for token in re.findall(r"[a-z0-9_]{2,}", str(value))}


@dataclass
class MemoryEvent:
    kind: str
    task: str
    outcome: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProceduralSkill:
    id: str
    name: str
    description: str
    tags: list[str]
    blueprint: dict[str, Any]
    uses: int = 0
    successes: int = 0
    failures: int = 0
    source: str = "user"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def score(self) -> float:
        # A conservative smoothed success rate, with a small reuse bonus.
        return (self.successes + 1) / (self.successes + self.failures + 2) + min(self.uses, 20) * 0.002


class MemoryStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_MEMORY_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / EVENTS_FILE
        self.skills_path = self.root / SKILLS_FILE

    def record(self, event: MemoryEvent | dict[str, Any]) -> MemoryEvent:
        item = event if isinstance(event, MemoryEvent) else MemoryEvent(**event)
        raw = json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("memory event is too large")
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(raw + "\n")
        return item

    def events(self, *, limit: int = 50, kind: str | None = None) -> list[MemoryEvent]:
        if not self.events_path.is_file():
            return []
        found: list[MemoryEvent] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines()[-max(0, limit * 4):]:
            try:
                item = MemoryEvent(**json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if kind is None or item.kind == kind:
                found.append(item)
        return found[-limit:]

    def _load_skills(self) -> list[ProceduralSkill]:
        if not self.skills_path.is_file():
            return []
        try:
            raw = json.loads(self.skills_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        return [ProceduralSkill(**item) for item in raw if isinstance(item, dict) and "id" in item]

    def _save_skills(self, skills: Iterable[ProceduralSkill]) -> None:
        ordered = sorted(skills, key=lambda skill: skill.updated_at, reverse=True)[:MAX_SKILLS]
        self.skills_path.write_text(json.dumps([asdict(skill) for skill in ordered], indent=2) + "\n", encoding="utf-8")

    def upsert_skill(self, skill: ProceduralSkill | dict[str, Any], *, promote: bool = False) -> ProceduralSkill:
        item = skill if isinstance(skill, ProceduralSkill) else ProceduralSkill(**skill)
        issues = validate_blueprint(item.blueprint)
        if any(issue.severity == "error" for issue in issues):
            raise ValueError("skill blueprint is invalid: " + "; ".join(issue.code for issue in issues))
        if not item.tags:
            item.tags = sorted(_tokens(item.name + " " + item.description))[:12]
        existing = {candidate.id: candidate for candidate in self._load_skills()}
        if item.id in existing:
            previous = existing[item.id]
            item.uses = previous.uses
            item.successes = previous.successes
            item.failures = previous.failures
            item.created_at = previous.created_at
        item.source = "promoted" if promote else item.source
        item.updated_at = time.time()
        existing[item.id] = item
        self._save_skills(existing.values())
        return item

    def skills(self) -> list[ProceduralSkill]:
        return sorted(self._load_skills(), key=lambda skill: skill.score, reverse=True)

    def retrieve(self, query: str, *, limit: int = 5) -> list[ProceduralSkill]:
        wanted = _tokens(query)
        ranked: list[tuple[float, ProceduralSkill]] = []
        for skill in self._load_skills():
            haystack = _tokens(" ".join([skill.id, skill.name, skill.description, *skill.tags, skill.blueprint.get("kind", "")]))
            overlap = len(wanted & haystack)
            if overlap:
                ranked.append((overlap / max(1, len(wanted)) + skill.score * 0.1, skill))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [skill for _, skill in ranked[:limit]]

    def feedback(self, skill_id: str, success: bool, *, task: str = "", note: str = "") -> ProceduralSkill | None:
        skills = {skill.id: skill for skill in self._load_skills()}
        skill = skills.get(skill_id)
        if skill is None:
            return None
        skill.uses += 1
        if success:
            skill.successes += 1
            outcome = "success"
        else:
            skill.failures += 1
            outcome = "failure"
        skill.updated_at = time.time()
        self._save_skills(skills.values())
        self.record(MemoryEvent("skill_feedback", task, outcome, {"skill_id": skill_id, "note": note}))
        return skill

    def promote(self, skill_id: str) -> ProceduralSkill | None:
        skills = {skill.id: skill for skill in self._load_skills()}
        skill = skills.get(skill_id)
        if skill is None:
            return None
        skill.source = "promoted"
        skill.updated_at = time.time()
        self._save_skills(skills.values())
        self.record(MemoryEvent("skill_promotion", skill.name, "success", {"skill_id": skill_id}))
        return skill


def propose_skill(store: MemoryStore, *, skill_id: str, name: str, description: str, blueprint: dict[str, Any], tags: list[str] | None = None) -> ProceduralSkill:
    """Create a candidate skill after validation; promotion is explicit."""
    skill = ProceduralSkill(skill_id, name, description, tags or [], blueprint, source="candidate")
    return store.upsert_skill(skill, promote=False)
