"""Prompt planners.

The production planner will be an external model adapter.  The deterministic
planner keeps the whole application runnable without credentials and supplies
stable fixtures for the API and UI.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import re
from pathlib import Path
from typing import Any

from . import KENTSUGI_VERSION
from .appearance import NAMED_COLORS
from .catalog import Catalog, load_catalog
from .document import normalize
from .validate import errors, to_repair_payload, validate

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "scene.schema.json"
DEFAULT_AGENTROUTER_URL = "https://co.agentrouter.org"
DEFAULT_AGENTROUTER_MODEL = "claude-opus-4-8"
DEFAULT_AGENTROUTER_RESPONSES_URL = "https://agentrouter.org/v1"
DEFAULT_AGENTROUTER_RESPONSES_MODEL = "gpt-5.6-sol"
SUPPORTED_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def prompt_colors(text: str) -> list[str]:
    """Recognise the safe scene colour vocabulary in ordinary prompt text."""
    found: list[str] = []
    for name in NAMED_COLORS:
        words = re.escape(name).replace("_", r"[\s_-]+")
        if re.search(rf"\b{words}\b", text, flags=re.IGNORECASE):
            canonical = "gray" if name == "grey" else name
            if canonical not in found:
                found.append(canonical)
    return found


def demo_plan(prompt: str) -> dict[str, Any]:
    """Translate a small, documented prompt vocabulary into a scene document."""
    text = " ".join(prompt.strip().split())
    if not text:
        raise ValueError("prompt must not be empty")
    if len(text) > 2_000:
        raise ValueError("prompt must be at most 2,000 characters")

    lowered = text.casefold()
    colors = prompt_colors(lowered)
    appearance = {"color": colors[0]} if colors else None
    environment = {
        "preset": "city" if any(word in lowered for word in ("city", "street", "urban")) else "void",
        "time_of_day": "night" if "night" in lowered else "dusk" if "sunset" in lowered else "day",
        "weather": "rain" if "rain" in lowered else "clear",
        "ground_material": "asphalt" if any(word in lowered for word in ("city", "street")) else "concrete",
    }

    objects: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    wants_character = any(word in lowered for word in ("boy", "girl", "person", "character", "human"))
    wants_car = any(word in lowered for word in ("car", "vehicle", "automotive"))
    wants_can = any(word in lowered for word in ("can", "soda", "soft drink", "energy drink", "monster"))
    wants_energy_can = any(word in lowered for word in ("energy", "monster"))
    wants_product = wants_can or any(
        word in lowered for word in ("ad", "advert", "product", "perfume", "bottle", "drink")
    )

    if wants_product:
        environment.update({"preset": "studio", "time_of_day": "dusk", "ground_material": "concrete"})

    if wants_product or not (wants_character or wants_car):
        product = {
                "id": "product",
                "asset": (
                    "tall_energy_can" if wants_energy_can
                    else "standard_soda_can" if wants_can
                    else "aura_bottle" if wants_product
                    else "crate"
                ),
                "placement": {"type": "at", "position": [0, 0]},
                "scale": 1.0 if wants_product else 1.4,
            }
        if appearance:
            product["appearance"] = appearance
        objects.append(product)

    if wants_character:
        character = {
                "id": "character",
                "asset": "human_character",
                "placement": {"type": "at", "position": [-3, 0]},
                "facing": "car" if wants_car else 0,
            }
        if appearance and not wants_car:
            character["appearance"] = appearance
        objects.append(character)

    if wants_car:
        placement = (
            {"type": "relative", "ref": "character", "direction": "ahead", "distance": 7}
            if wants_character
            else {"type": "at", "position": [0, 0]}
        )
        car = {"id": "car", "asset": "red_car", "placement": placement, "facing": 90}
        if appearance:
            car["appearance"] = appearance
        objects.append(car)

    camera_target = "product" if wants_product or not (wants_character or wants_car) else "character" if wants_character else "car"
    camera_type = "orbit" if wants_product or "orbit" in lowered else "follow"

    if wants_character and wants_car and any(word in lowered for word in ("walk", "approach", "toward")):
        actions.append(
            {"actor": "character", "type": "walk_to", "target": "car", "start": 0, "duration": 2, "stop_distance": 2.5}
        )

    return normalize(
        {
            "kentsugi_version": KENTSUGI_VERSION,
            "title": text[:200],
            "environment": environment,
            "objects": objects,
            "actions": actions,
            "camera": {
                "type": camera_type,
                "target": camera_target,
                "position": "front" if wants_product else "behind",
                "distance": 0.6 if wants_can else 6,
                "height": 0.18 if wants_can else 2.2,
                "shot": "close_up" if wants_can else "medium",
            },
            "render": {"duration": 2, "fps": 12, "resolution": [640, 360], "quality": "preview"},
        }
    )


def _json_from_text(text: str) -> dict[str, Any]:
    """Extract one scene object from a model response without executing it."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response did not contain a JSON object") from None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"model returned invalid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


class AgentRouterPlanner:
    """Anthropic Messages-compatible scene planner for AgentRouter."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_AGENTROUTER_URL,
        model: str = DEFAULT_AGENTROUTER_MODEL,
        timeout: float = 90.0,
        catalog: Catalog | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("AgentRouter token must not be empty")
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("AgentRouter base URL must use HTTPS")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.catalog = catalog or load_catalog()

    @classmethod
    def from_env(cls) -> "AgentRouterPlanner":
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        return cls(
            token,
            base_url=os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_AGENTROUTER_URL),
            model=os.environ.get("ANTHROPIC_MODEL", DEFAULT_AGENTROUTER_MODEL),
        )

    def plan(self, prompt: str) -> dict[str, Any]:
        text = " ".join(prompt.strip().split())
        if not text:
            raise ValueError("prompt must not be empty")
        if len(text) > 2_000:
            raise ValueError("prompt must be at most 2,000 characters")

        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        system = (
            "You are the Kentsugi scene compiler. Return exactly one JSON object and no prose. "
            "Never emit code, file paths, or asset names outside the supplied catalog. Prefer relative "
            "placements; use one explicit 'at' placement only when an anchor is required. The document "
            "must conform exactly to the supplied JSON Schema."
        )
        user = (
            f"USER PROMPT:\n{text}\n\n"
            f"AVAILABLE ASSET IDS:\n{json.dumps(self.catalog.ids())}\n\n"
            f"SCENE JSON SCHEMA:\n{schema}"
        )
        response_text = self._complete([{"role": "user", "content": user}], system)
        raw = _json_from_text(response_text)
        document = normalize(raw)
        issues = validate(document, self.catalog)
        hard = errors(issues)
        if not hard:
            return document

        repair = (
            "The previous JSON failed validation. Return the complete corrected JSON object only.\n\n"
            f"ERRORS:\n{to_repair_payload(issues)}"
        )
        repaired_text = self._complete(
            [
                {"role": "user", "content": user},
                {"role": "assistant", "content": json.dumps(raw)},
                {"role": "user", "content": repair},
            ],
            system,
        )
        repaired = normalize(_json_from_text(repaired_text))
        remaining = errors(validate(repaired, self.catalog))
        if remaining:
            raise ValueError("model could not repair the scene: " + "; ".join(str(issue) for issue in remaining))
        return repaired

    def _complete(self, messages: list[dict[str, str]], system: str) -> str:
        body = json.dumps(
            {"model": self.model, "max_tokens": 8_192, "system": system, "messages": messages}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self.token,
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1_000]
            raise ValueError(f"AgentRouter returned HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError(f"AgentRouter request failed: {exc.reason if hasattr(exc, 'reason') else exc}") from None

        blocks = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(blocks, list):
            raise ValueError("AgentRouter response did not contain message content")
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            raise ValueError("AgentRouter response contained no text")
        return text


class AgentRouterResponsesPlanner(AgentRouterPlanner):
    """OpenAI Responses-compatible scene planner for AgentRouter."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_AGENTROUTER_RESPONSES_URL,
        model: str = DEFAULT_AGENTROUTER_RESPONSES_MODEL,
        reasoning_effort: str = "medium",
        timeout: float = 90.0,
        catalog: Catalog | None = None,
    ) -> None:
        super().__init__(token, base_url=base_url, model=model, timeout=timeout, catalog=catalog)
        if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            allowed = ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
            raise ValueError(f"AgentRouter reasoning effort must be one of: {allowed}")
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_env(cls) -> "AgentRouterResponsesPlanner":
        return cls(
            os.environ.get("AGENTROUTER_API_KEY", ""),
            base_url=os.environ.get(
                "AGENTROUTER_RESPONSES_BASE_URL", DEFAULT_AGENTROUTER_RESPONSES_URL
            ),
            model=os.environ.get("AGENTROUTER_RESPONSES_MODEL", DEFAULT_AGENTROUTER_RESPONSES_MODEL),
            reasoning_effort=os.environ.get("AGENTROUTER_REASONING_EFFORT", "medium").casefold(),
        )

    def _complete(self, messages: list[dict[str, str]], system: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "instructions": system,
                "input": messages,
                "reasoning": {"effort": self.reasoning_effort},
                "max_output_tokens": 8_192,
                "text": {"format": {"type": "json_object"}, "verbosity": "low"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1_000]
            raise ValueError(f"AgentRouter returned HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError(f"AgentRouter request failed: {exc.reason if hasattr(exc, 'reason') else exc}") from None

        text = payload.get("output_text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text:
            output = payload.get("output") if isinstance(payload, dict) else None
            if isinstance(output, list):
                text = "".join(
                    content.get("text", "")
                    for item in output
                    if isinstance(item, dict) and item.get("type") == "message"
                    for content in item.get("content", [])
                    if isinstance(content, dict) and content.get("type") == "output_text"
                )
        if not isinstance(text, str) or not text:
            raise ValueError("AgentRouter response contained no output text")
        return text


def configured_planner() -> tuple[str, Any]:
    """Choose the live planner when configured, otherwise retain a runnable demo."""
    choice = os.environ.get("KENTSUGI_PLANNER", "auto").casefold()
    choices = {"auto", "demo", "agentrouter", "agentrouter-responses"}
    if choice not in choices:
        raise ValueError(
            "KENTSUGI_PLANNER must be 'auto', 'demo', 'agentrouter', or 'agentrouter-responses'"
        )
    if choice == "demo":
        return "demo", demo_plan
    if choice == "agentrouter-responses" or (choice == "auto" and os.environ.get("AGENTROUTER_API_KEY")):
        planner = AgentRouterResponsesPlanner.from_env()
        return "agentrouter-responses", planner.plan
    if choice == "auto" and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "demo", demo_plan
    planner = AgentRouterPlanner.from_env()
    return "agentrouter", planner.plan
