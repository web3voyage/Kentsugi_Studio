"""A small, dependency-free web workbench for Kentsugi scene documents.

The web app deliberately stays on the same side of the architecture boundary
as the CLI: it calls the validator, layout solver, timeline baker, and SVG
preview renderer directly, but never imports Blender.  That makes the editor
instant on machines where a real render would be slow or unavailable.

Run it with ``python -m kentsugi web`` and open the printed localhost URL.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .catalog import Catalog, load_catalog
from .document import load_document, new_document, normalize
from .layout import solve
from .preview import render_svg
from .timeline import bake
from .validate import errors, validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DOCUMENT = REPO_ROOT / "examples" / "rainy_city.json"
SCHEMA_DOCUMENT = REPO_ROOT / "schema" / "scene.schema.json"
MAX_BODY_BYTES = 1_000_000


def _issues(issues: list[Any]) -> list[dict[str, Any]]:
    return [asdict(issue) for issue in issues]


def _catalog_json(catalog: Catalog) -> list[dict[str, Any]]:
    return [
        {
            "id": asset.id,
            "name": asset.name,
            "kind": asset.kind,
            "size": list(asset.size),
            "radius": asset.radius,
            "clips": list(asset.clips),
            "source": asset.source,
            "color": list(asset.color),
            "static": asset.static,
        }
        for asset in sorted(catalog, key=lambda item: item.id)
    ]


def process_document(document: Any, catalog: Catalog) -> tuple[dict[str, Any], int]:
    """Validate and, when possible, bake a document for the browser client."""
    if not isinstance(document, dict):
        return {"issues": [{"severity": "error", "path": "", "code": "bad_type", "message": "document must be a JSON object"}]}, 422

    try:
        normalized = normalize(document)
    except (TypeError, ValueError) as exc:
        return {"issues": [{"severity": "error", "path": "", "code": "bad_json", "message": str(exc)}]}, 422

    found = validate(normalized, catalog)
    response: dict[str, Any] = {
        "document": normalized,
        "issues": _issues(found),
        "valid": not errors(found),
    }
    if errors(found):
        return response, 422

    layout = solve(normalized, catalog)
    plan = bake(normalized, catalog, layout)
    response.update({"plan": plan, "svg": render_svg(plan)})
    return response, 200


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "KentsugiWeb/0.1"
    catalog: Catalog
    example: dict[str, Any]
    schema: Any

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib hook name
        # Keep the terminal useful while still showing normal request activity.
        print(f"kentsugi web: {self.address_string()} - {format % args}")

    def _send(self, payload: Any, status: int = HTTPStatus.OK, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else (payload.encode("utf-8") if isinstance(payload, str) else _json_bytes(payload))
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(INDEX_HTML, content_type="text/html")
        elif path == "/api/catalog":
            self._send({"assets": _catalog_json(self.catalog)})
        elif path == "/api/example":
            self._send({"document": self.example})
        elif path == "/api/schema":
            self._send(self.schema)
        else:
            self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        path = urlsplit(self.path).path
        if path not in ("/api/validate", "/api/preview"):
            self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send({"error": "request body must be between 0 and 1,000,000 bytes"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send({"error": f"invalid JSON: {exc}"}, HTTPStatus.BAD_REQUEST)
            return

        document = payload.get("document") if isinstance(payload, dict) and "document" in payload else payload
        result, status = process_document(document, self.catalog)
        if path == "/api/validate":
            result.pop("plan", None)
            result.pop("svg", None)
        self._send(result, status)


def make_server(host: str = "127.0.0.1", port: int = 8765, catalog_path: str | Path | None = None) -> ThreadingHTTPServer:
    """Create a configured server without starting its blocking loop."""
    catalog = load_catalog(catalog_path)
    example = load_document(EXAMPLE_DOCUMENT) if EXAMPLE_DOCUMENT.is_file() else new_document("Kentsugi web workbench")
    schema = json.loads(SCHEMA_DOCUMENT.read_text(encoding="utf-8")) if SCHEMA_DOCUMENT.is_file() else {}

    class Handler(_Handler):
        pass

    Handler.catalog = catalog
    Handler.example = example
    Handler.schema = schema
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8765, catalog_path: str | Path | None = None, *, open_browser: bool = False) -> None:
    server = make_server(host, port, catalog_path)
    url = f"http://{host}:{server.server_port}/"
    print(f"Kentsugi web workbench: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.15, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kentsugi — Shot Workbench</title>
<style>
:root{color-scheme:dark;--bg:#0b0e11;--panel:#13181d;--panel2:#1a222a;--line:#2a3640;--text:#e7edf2;--muted:#8fa0ad;--accent:#61d6b7;--warn:#e5b45b;--bad:#ee766f}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% -10%,#1c3133 0,#0b0e11 45%);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
header{height:72px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid var(--line);background:#0b0e11cc;backdrop-filter:blur(12px)}
.brand{display:flex;gap:12px;align-items:center}.mark{width:30px;height:30px;border:2px solid var(--accent);border-radius:9px;transform:rotate(45deg);position:relative}.mark:after{content:"";position:absolute;inset:6px;border:1px solid var(--warn);border-radius:4px}.brand h1{font-size:19px;letter-spacing:.12em;margin:0}.brand small{color:var(--muted);display:block;font-size:11px;margin-top:-3px}
.pill{border:1px solid var(--line);border-radius:999px;padding:6px 11px;color:var(--muted);font-size:12px}.pill.ok{color:var(--accent);border-color:#2d7668}.pill.bad{color:var(--bad);border-color:#7a3f3b}
main{max-width:1500px;margin:0 auto;padding:24px 28px 40px;display:grid;grid-template-columns:minmax(380px,480px) 1fr;gap:22px}.panel{background:linear-gradient(145deg,#161d23,#101419);border:1px solid var(--line);border-radius:16px;box-shadow:0 14px 40px #0004;overflow:hidden}.panel-head{padding:18px 20px 14px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.panel-head h2{margin:0;font-size:15px}.panel-head p{margin:4px 0 0;color:var(--muted);font-size:12px}.body{padding:18px 20px}
textarea{width:100%;min-height:600px;resize:vertical;background:#0b0f13;border:1px solid var(--line);border-radius:10px;color:#d8e4ec;padding:15px;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;outline:none}textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px #61d6b71c}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.button{border:1px solid var(--line);border-radius:9px;padding:9px 12px;background:#1b252d;color:var(--text);cursor:pointer;font-weight:600}.button:hover{border-color:var(--accent);color:var(--accent)}.button.primary{background:var(--accent);color:#06201a;border-color:var(--accent)}.button.primary:hover{background:#8ce7d0;color:#06201a}.button:disabled{opacity:.5;cursor:wait}
#preview-stage{min-height:580px;border:1px solid var(--line);border-radius:10px;display:grid;place-items:center;background:#0b0f12;overflow:auto}#preview-stage svg{display:block;max-width:100%;height:auto}.empty{color:var(--muted);text-align:center;padding:60px 20px}.empty strong{display:block;color:var(--text);font-size:16px;margin-bottom:6px}
.meta{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}.stat{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px}.stat b{display:block;font-size:16px}.stat span{font-size:11px;color:var(--muted)}
.issues{margin-top:14px;display:grid;gap:7px}.issue{border-radius:8px;padding:9px 10px;border:1px solid #7a3f3b;background:#2a1616;color:#ffb7b0;font-size:12px}.issue.warning{border-color:#735c31;background:#2b2112;color:#f0cf8b}.issue code{color:inherit;opacity:.8}.catalog{display:flex;gap:6px;flex-wrap:wrap;margin-top:14px}.asset{border:1px solid var(--line);border-radius:999px;color:var(--muted);padding:4px 8px;font-size:11px}.asset b{color:var(--text)}
@media(max-width:1000px){main{grid-template-columns:1fr}textarea{min-height:360px}#preview-stage{min-height:420px}}@media(max-width:600px){header{padding:0 16px}main{padding:14px 16px 28px}.meta{grid-template-columns:repeat(2,1fr)}.pill{display:none}}
</style>
</head>
<body>
<header><div class="brand"><div class="mark"></div><div><h1>KENTSUGI</h1><small>prompt-driven shot workbench</small></div></div><div id="status" class="pill">loading example…</div></header>
<main>
<section class="panel"><div class="panel-head"><div><h2>Scene blueprint</h2><p>Describe relations; Kentsugi resolves coordinates and keyframes.</p></div><span id="hash" class="pill">—</span></div><div class="body"><div class="toolbar"><button class="button" id="load-example">Load example</button><button class="button" id="format">Format JSON</button><button class="button primary" id="preview">Validate &amp; preview</button></div><textarea id="editor" spellcheck="false" aria-label="Scene document JSON"></textarea><div id="issues" class="issues"></div></div></section>
<section class="panel"><div class="panel-head"><div><h2>Top-down preview</h2><p>Same baked plan as the CLI SVG preview — no Blender required.</p></div><span id="engine" class="pill">engine —</span></div><div class="body"><div id="preview-stage"><div class="empty"><strong>Your shot will appear here</strong>Edit the blueprint, then validate and preview.</div></div><div id="meta" class="meta"></div><div id="catalog" class="catalog"></div></div></section>
</main>
<script>
const $=id=>document.getElementById(id), editor=$('editor');
function setStatus(text,kind=''){const el=$('status');el.textContent=text;el.className='pill '+kind}
function showIssues(items){$('issues').innerHTML='';for(const issue of items||[]){const el=document.createElement('div');el.className='issue '+(issue.severity==='warning'?'warning':'');el.innerHTML=`<code>${issue.severity}</code> · ${issue.path||'document'} · ${issue.message} <code>[${issue.code}]</code>`;$('issues').appendChild(el)}}
function showPlan(result){const p=result.plan;if(!p)return;$('hash').textContent=p.source_hash;$('engine').textContent=`${p.render.engine} · ${p.render.quality}`;$('meta').innerHTML=`<div class="stat"><b>${p.objects.length}</b><span>objects</span></div><div class="stat"><b>${p.duration}s</b><span>duration</span></div><div class="stat"><b>${p.fps} fps</b><span>frame rate</span></div><div class="stat"><b>${p.camera.type}</b><span>camera</span></div>`;$('preview-stage').innerHTML=result.svg;setStatus('blueprint valid','ok')}
async function loadExample(){setStatus('loading…');const r=await fetch('/api/example');const data=await r.json();editor.value=JSON.stringify(data.document,null,2);await runPreview()}
async function runPreview(){let doc;try{doc=JSON.parse(editor.value)}catch(e){showIssues([{severity:'error',path:'',code:'bad_json',message:e.message}]);setStatus('invalid JSON','bad');return} $('preview').disabled=true;setStatus('baking…');try{const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({document:doc})});const data=await r.json();showIssues(data.issues);if(r.ok)showPlan(data);else{setStatus('fix validation errors','bad');$('hash').textContent='—';$('engine').textContent='engine —'}}catch(e){showIssues([{severity:'error',path:'',code:'network',message:e.message}]);setStatus('server unavailable','bad')}finally{$('preview').disabled=false}}
$('load-example').onclick=loadExample;$('preview').onclick=runPreview;$('format').onclick=()=>{try{editor.value=JSON.stringify(JSON.parse(editor.value),null,2)}catch(e){setStatus('invalid JSON','bad')}};
async function loadCatalog(){const r=await fetch('/api/catalog');const data=await r.json();$('catalog').innerHTML=data.assets.map(a=>`<span class="asset" title="${a.name}"><b>${a.id}</b> · ${a.kind}</span>`).join('')}
loadCatalog();loadExample();
</script>
</body></html>'''

