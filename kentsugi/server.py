"""Dependency-free HTTP API for planning, previewing, and rendering jobs."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .catalog import load_catalog
from .document import save_document
from .layout import solve
from .planner import configured_planner
from .preview import render_svg
from .timeline import bake
from .validate import errors, validate

JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")


@dataclass
class Job:
    id: str
    prompt: str
    status: str
    document: dict[str, Any]
    plan: dict[str, Any]
    directory: Path
    error: str | None = None
    video: Path | None = None
    requested_engine: str = "auto"
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "status": self.status,
            "error": self.error,
            "document": self.document,
            "plan": self.plan,
            "preview_url": f"/api/jobs/{self.id}/preview.svg",
            "video_url": f"/api/jobs/{self.id}/video.mp4" if self.video else None,
            "requested_engine": self.requested_engine,
        }


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.catalog = load_catalog()
        self.planner_name, self.planner = configured_planner()

    def create(
        self,
        prompt: str,
        *,
        render: bool = False,
        engine: str = "auto",
        quality: str | None = None,
    ) -> Job:
        if engine not in {"auto", "cycles", "eevee"}:
            raise ValueError("engine must be auto, cycles, or eevee")
        if quality not in {None, "preview", "final"}:
            raise ValueError("quality must be preview or final")
        document = self.planner(prompt)
        if quality is not None:
            document["render"]["quality"] = quality
        issues = validate(document, self.catalog)
        hard = errors(issues)
        if hard:
            raise ValueError("planner produced an invalid document: " + "; ".join(str(issue) for issue in hard))
        layout = solve(document, self.catalog)
        plan = bake(document, self.catalog, layout)

        job_id = uuid.uuid4().hex[:12]
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=False)
        save_document(document, directory / "scene.json")
        (directory / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        (directory / "preview.svg").write_text(render_svg(plan), encoding="utf-8")
        job = Job(
            job_id, prompt, "queued" if render else "preview_ready",
            document, plan, directory, requested_engine=engine,
        )
        with self._lock:
            self._jobs[job_id] = job
        if render:
            threading.Thread(target=self._render, args=(job, engine), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    @staticmethod
    def _render(job: Job, engine: str) -> None:
        with job.lock:
            job.status = "rendering"
        output = job.directory / "video.mp4"
        command = [
            sys.executable,
            "-m",
            "kentsugi",
            "render",
            str(job.directory / "scene.json"),
            "-o",
            str(output),
            "--engine",
            engine,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        with job.lock:
            if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                job.status = "complete"
                job.video = output
            else:
                job.status = "failed"
                detail = (result.stderr or result.stdout or f"render exited {result.returncode}").strip()
                job.error = detail[-4_000:]


class KentsugiHandler(BaseHTTPRequestHandler):
    store: JobStore

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({
                "status": "ok",
                "planner": self.store.planner_name,
                "assets": len(self.store.catalog),
                "capabilities": ["planning", "validation", "svg_preview", "cycles_render", "object_engine"],
            })
            return
        if path == "/api/catalog":
            self._json({
                "assets": [
                    {
                        "id": asset.id,
                        "name": asset.name,
                        "kind": asset.kind,
                        "size": list(asset.size),
                        "color": list(asset.color),
                        "source_type": asset.source.get("type", "primitive"),
                    }
                    for asset in self.store.catalog
                ]
            })
            return
        if path == "/api/jobs":
            with self.store._lock:
                jobs = list(self.store._jobs.values())
            self._json({"jobs": [job.public() for job in reversed(jobs[-20:])]})
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})(?:/(preview\.svg|video\.mp4))?", path)
        if not match:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        job = self.store.get(match.group(1))
        if job is None:
            self._json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            return
        asset = match.group(2)
        if asset is None:
            self._json(job.public())
        elif asset == "preview.svg":
            self._file(job.directory / asset, "image/svg+xml")
        elif job.video is not None:
            self._file(job.video, "video/mp4")
        else:
            self._json({"error": "video is not ready"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/jobs":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 20_000:
                raise ValueError("request body must be between 1 and 20,000 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise ValueError("prompt must be a string")
            render = payload.get("render", False)
            if not isinstance(render, bool):
                raise ValueError("render must be a boolean")
            engine = payload.get("engine", "auto")
            if not isinstance(engine, str):
                raise ValueError("engine must be a string")
            quality = payload.get("quality")
            if quality is not None and not isinstance(quality, str):
                raise ValueError("quality must be a string")
            job = self.store.create(prompt, render=render, engine=engine, quality=quality)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json(job.public(), HTTPStatus.ACCEPTED if render else HTTPStatus.CREATED)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, format: str, *args: object) -> None:
        print(f"api: {format % args}")


def serve(host: str = "127.0.0.1", port: int = 8765, root: Path | None = None) -> None:
    store = JobStore(root or Path(".kentsugi") / "jobs")
    handler = type("BoundKentsugiHandler", (KentsugiHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Kentsugi API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
