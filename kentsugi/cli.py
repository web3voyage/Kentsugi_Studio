"""Command line interface.

    python -m kentsugi validate examples/rainy_city.json
    python -m kentsugi plan     examples/rainy_city.json
    python -m kentsugi preview  examples/rainy_city.json --open
    python -m kentsugi render   examples/rainy_city.json -o out.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from .catalog import load_catalog
from .asset_blueprint import load_blueprint, validate_blueprint
from .blueprints import load_blueprints
from .memory import MemoryEvent, MemoryStore, propose_skill
from .document import load_document, save_document
from .engine import resolve_engine
from .knowledge import KnowledgeLibrary
from .layout import solve
from .object_engine import ObjectEngine
from .preview import render_svg
from .server import serve as serve_api
from .timeline import bake
from .validate import errors, to_repair_payload, validate, warnings

REPO_ROOT = Path(__file__).resolve().parent.parent
BLENDER_APPLIER = REPO_ROOT / "blender" / "render.py"
BUILD_DIR = REPO_ROOT / ".kentsugi"

# Checked in order after $KENTSUGI_BLENDER and $PATH.
BLENDER_SEARCH_GLOBS = (
    r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
    r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe",
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/snap/bin/blender",
)


def find_blender(explicit: str | None = None) -> Path | None:
    """Locate a Blender executable, or return None with no side effects."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("KENTSUGI_BLENDER")
    if env:
        candidates.append(env)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path

    on_path = shutil.which("blender")
    if on_path:
        return Path(on_path)

    for pattern in BLENDER_SEARCH_GLOBS:
        if any(ch in pattern for ch in "*?"):
            anchor = Path(pattern).anchor
            relative = pattern[len(anchor):] if anchor else pattern
            try:
                matches = sorted(Path(anchor or ".").glob(relative), reverse=True)
            except (OSError, ValueError):
                continue
            for match in matches:
                if match.is_file():
                    return match
        else:
            path = Path(pattern)
            if path.is_file():
                return path
    return None


def _load(doc_path: str, catalog_path: str | None) -> tuple[dict[str, Any], Any]:
    return load_document(doc_path), load_catalog(catalog_path)


def _report(issues: list[Any], quiet: bool = False) -> int:
    """Print issues; return a process exit code."""
    hard, soft = errors(issues), warnings(issues)
    if not quiet:
        for issue in hard + soft:
            print(f"  {issue}", file=sys.stderr if issue.severity == "error" else sys.stdout)
    if hard:
        print(f"\n{len(hard)} error(s), {len(soft)} warning(s)", file=sys.stderr)
        return 1
    if soft and not quiet:
        print(f"\nok, with {len(soft)} warning(s)")
    return 0


def _build_plan(args: argparse.Namespace) -> dict[str, Any] | None:
    doc, catalog = _load(args.document, args.catalog)
    issues = validate(doc, catalog)
    if errors(issues):
        print("validation failed:", file=sys.stderr)
        _report(issues)
        if getattr(args, "repair_json", False):
            print(to_repair_payload(issues))
        return None
    for issue in warnings(issues):
        print(f"  {issue}")

    if getattr(args, "quality", None):
        doc["render"]["quality"] = args.quality

    layout = solve(doc, catalog)
    plan = bake(doc, catalog, layout)

    # EEVEE needs a working GPU context, which a headless box may not have.
    # Overriding here rather than in the baker keeps the plan hash a property
    # of the document, not of the machine that happens to render it. `auto` is
    # resolved in cmd_render, where Blender is known and can be probed.
    engine = getattr(args, "engine", None)
    if engine and engine != "auto":
        plan["render"]["engine"] = {"eevee": "EEVEE", "cycles": "CYCLES"}[engine]

    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    return plan


def cmd_validate(args: argparse.Namespace) -> int:
    doc, catalog = _load(args.document, args.catalog)
    issues = validate(doc, catalog)
    if not issues:
        print(f"{args.document}: valid")
        return 0
    print(f"{args.document}:")
    code = _report(issues)
    if code and args.repair_json:
        print(to_repair_payload(issues))
    return code


def cmd_catalog(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    print(f"{len(catalog)} asset(s):\n")
    for asset in sorted(catalog, key=lambda a: a.id):
        size = " x ".join(f"{v:g}" for v in asset.size)
        clips = ", ".join(asset.clips) or "-"
        print(f"  {asset.id:<18} {asset.kind:<10} {size:>18} m   r={asset.radius:<5g} clips: {clips}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    plan = _build_plan(args)
    if plan is None:
        return 1

    out = Path(args.output) if args.output else BUILD_DIR / "plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    print(f"\n{plan['title']}")
    print(f"  frames     {plan['frame_start']}-{plan['frame_end']} @ {plan['fps']}fps ({plan['duration']}s)")
    print(f"  engine     {plan['render']['engine']} ({plan['render']['quality']}, {plan['render']['samples']} samples)")
    print(f"  resolution {plan['render']['resolution'][0]}x{plan['render']['resolution'][1]}")
    print(f"  hash       {plan['source_hash']}")
    print("\n  objects:")
    for obj in plan["objects"]:
        last = obj["keys"][-1]
        pos = ", ".join(f"{v:6.2f}" for v in last["position"])
        moving = " (moving)" if len(obj["keys"]) > 1 else ""
        print(f"    {obj['id']:<16} {obj['asset']:<18} ({pos})  yaw {last['yaw']:7.1f}deg{moving}")
    cam = plan["camera"]
    print(f"\n  camera: {cam['type']}, {cam['focal_mm']:g}mm, {len(cam['keys'])} key(s)")
    print(f"\nwrote {out}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    plan = _build_plan(args)
    if plan is None:
        return 1

    out = Path(args.output) if args.output else BUILD_DIR / "preview.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_svg(plan), encoding="utf-8")
    print(f"wrote {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    plan = _build_plan(args)
    if plan is None:
        return 1

    blender = find_blender(args.blender)
    if blender is None:
        print(
            "\nBlender was not found.\n"
            "  Install it, then either add it to PATH or set KENTSUGI_BLENDER to the\n"
            "  executable, or pass --blender <path>.\n"
            "  Windows: winget install BlenderFoundation.Blender\n"
            "  macOS:   brew install --cask blender\n"
            "  Linux:   sudo apt install blender\n\n"
            "  Meanwhile 'python -m kentsugi preview' renders the layout as SVG with no\n"
            "  dependencies at all.",
            file=sys.stderr,
        )
        return 2

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve `auto` now that Blender is in hand. Probing costs one tiny
    # headless render, and only when the plan asked for EEVEE.
    engine, note = resolve_engine(
        getattr(args, "engine", None),
        plan["render"]["engine"],
        blender,
        probe=not getattr(args, "no_probe", False),
    )
    if note:
        print(f"engine:  {note}")
    plan["render"]["engine"] = engine

    plan_path = BUILD_DIR / f"plan-{plan['source_hash']}.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    suffix = ".png" if getattr(args, "frame", None) is not None else ".mp4"
    output = Path(args.output) if args.output else BUILD_DIR / f"{plan['source_hash']}{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(blender), "--background", "--factory-startup",
        "--python", str(BLENDER_APPLIER),
        "--", "--plan", str(plan_path), "--output", str(output),
        "--device", args.device.upper(),
    ]
    if args.keep_blend:
        command += ["--save-blend", str(output.with_suffix(".blend"))]
    if getattr(args, "frame", None) is not None:
        command += ["--still-frame", str(args.frame)]
    if getattr(args, "samples", None) is not None:
        command += ["--samples", str(args.samples)]
    command += ["--resolution-scale", str(getattr(args, "resolution_scale", 100))]

    print(f"\nblender: {blender}")
    print(f"engine:  {plan['render']['engine']} ({args.device.upper()})")
    if getattr(args, "frame", None) is None:
        print(f"rendering {plan['frame_end'] - plan['frame_start'] + 1} frames -> {output}\n")
    else:
        print(f"rendering frame {args.frame} -> {output}\n")

    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"\nblender exited with code {result.returncode}", file=sys.stderr)
        return result.returncode

    # Blender exits 0 even when the embedded Python script raises, so the exit
    # code alone is not evidence of a render. Verify the file.
    if not output.is_file() or output.stat().st_size == 0:
        print(
            f"\nblender reported success but produced no video at {output}.\n"
            "  Scroll up for a Python traceback from the applier.",
            file=sys.stderr,
        )
        return 1

    print(f"\nwrote {output} ({output.stat().st_size / 1_000_000:.1f} MB)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"python   {sys.version.split()[0]}  ({sys.executable})")
    blender = find_blender(args.blender)
    print(f"blender  {blender if blender else 'NOT FOUND — render unavailable, preview still works'}")
    try:
        catalog = load_catalog(args.catalog)
        print(f"catalog  {len(catalog)} asset(s)")
    except (OSError, ValueError, KeyError) as exc:
        print(f"catalog  FAILED: {exc}")
        return 1
    print(f"applier  {BLENDER_APPLIER}{'' if BLENDER_APPLIER.is_file() else '  (MISSING)'}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Start the dependency-free local scene workbench."""
    from .web import serve

    serve(
        host=args.host,
        port=args.port,
        catalog_path=args.catalog,
        open_browser=args.open,
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the prompt-planning and render-job API used by Kentsugi Studio."""
    serve_api(args.host, args.port, Path(args.jobs) if args.jobs else None)
    return 0


def cmd_object(args: argparse.Namespace) -> int:
    """Run semantic object intent through blueprint selection and review memory."""
    if args.render_output and args.artifact:
        raise ValueError("choose either --render-output or --artifact, not both")
    if args.approve and not (args.render_output or args.artifact):
        raise ValueError("--approve requires a new --render-output or an inspected --artifact")

    engine = ObjectEngine(
        catalog=load_catalog(args.catalog),
        registry=load_blueprints(args.blueprints),
        knowledge=KnowledgeLibrary(args.knowledge),
    )
    run = engine.prepare(args.prompt, max_attempts=args.max_attempts)
    scene_path = Path(args.output) if args.output else BUILD_DIR / "object-scene.json"
    save_document(run.scene, scene_path)

    print(f"\nObjectSpec  {run.spec['category']} / {run.spec['family']} / {run.spec['variant']}")
    print(f"blueprint   {run.blueprint.id} -> {run.blueprint.asset_id}")
    print(f"attempts    {run.attempts}")
    print("checks")
    for check in run.checks:
        print(f"  {check.severity:<7} {check.stage:<18} {check.code}")
    print(f"scene       {scene_path}")

    artifact: Path | None = Path(args.artifact) if args.artifact else None
    render_code = 0
    if args.render_output:
        artifact = Path(args.render_output)
        render_args = argparse.Namespace(
            document=str(scene_path), catalog=args.catalog, quality=args.quality,
            engine=args.engine, no_probe=args.no_probe, device=args.device,
            blender=args.blender, keep_blend=args.keep_blend, frame=args.frame,
            samples=args.samples, resolution_scale=args.resolution_scale,
            output=str(artifact),
        )
        render_code = cmd_render(render_args)
        if render_code != 0:
            artifact = None

    record = engine.finalize(run, artifact=artifact, approved=bool(args.approve and artifact))
    print(f"knowledge   {record}")
    if args.approve:
        print("status      approved")
    elif artifact:
        print("status      pending review")
    else:
        print("status      pending render")
    return render_code


def cmd_forge(args: argparse.Namespace) -> int:
    """Validate a blueprint and compile it through Blender's Asset Forge."""
    blueprint = load_blueprint(args.blueprint)
    issues = validate_blueprint(blueprint)
    if issues:
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    blender = find_blender(args.blender)
    if blender is None:
        print("Blender was not found; pass --blender or set KENTSUGI_BLENDER", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else REPO_ROOT / "assets" / "generated" / f"{blueprint['id']}.blend"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(blender), "--background", "--factory-startup",
        "--python", str(REPO_ROOT / "blender" / "forge.py"),
        "--", "--blueprint", str(Path(args.blueprint).resolve()), "--output", str(output.resolve()),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        MemoryStore(args.memory_directory).record(MemoryEvent("forge", blueprint.get("name", blueprint.get("id", "asset")), "failure", {"blueprint": str(args.blueprint)}))
        print("asset forge failed to produce a .blend", file=sys.stderr)
        return result.returncode or 1
    memory = MemoryStore(args.memory_directory)
    skill_id = f"{blueprint['id']}_v1"
    propose_skill(
        memory,
        skill_id=skill_id,
        name=blueprint.get("name", blueprint["id"]),
        description=f"Procedurally build {blueprint.get('name', blueprint['id'])}",
        blueprint=blueprint,
        tags=[blueprint["id"], blueprint.get("kind", "asset"), *blueprint.get("tags", [])],
    )
    if args.promote_skill:
        memory.promote(skill_id)
    memory.record(MemoryEvent("forge", blueprint.get("name", blueprint["id"]), "success", {"skill_id": skill_id, "output": str(output)}))
    print(f"wrote {output}")
    print(f"remembered candidate skill {skill_id}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    store = MemoryStore(args.directory)
    if args.memory_action == "skills":
        for skill in store.skills():
            print(f"{skill.id:<24} {skill.source:<10} score={skill.score:.3f} uses={skill.uses} :: {skill.description}")
        return 0
    if args.memory_action == "retrieve":
        for skill in store.retrieve(args.query, limit=args.limit):
            print(f"{skill.id:<24} score={skill.score:.3f} :: {skill.description}")
        return 0
    if args.memory_action == "events":
        for event in store.events(limit=args.limit):
            print(json.dumps({"id": event.id, "kind": event.kind, "task": event.task, "outcome": event.outcome, "data": event.data}, ensure_ascii=False))
        return 0
    if args.memory_action == "feedback":
        skill = store.feedback(args.skill_id, args.success == "success", task=args.task, note=args.note)
        if skill is None:
            print(f"unknown skill: {args.skill_id}", file=sys.stderr)
            return 1
        print(f"{skill.id}: score={skill.score:.3f}, uses={skill.uses}, successes={skill.successes}, failures={skill.failures}")
        return 0
    if args.memory_action == "promote":
        skill = store.promote(args.skill_id)
        if skill is None:
            print(f"unknown skill: {args.skill_id}", file=sys.stderr)
            return 1
        print(f"promoted {skill.id}")
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kentsugi", description="Prompt-driven 3D shot authoring.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, document: bool = True) -> None:
        if document:
            p.add_argument("document", help="path to a scene document (JSON)")
        p.add_argument("--catalog", help="path to an asset catalog (defaults to assets/catalog.json)")

    p_validate = sub.add_parser("validate", help="check a document without rendering")
    add_common(p_validate)
    p_validate.add_argument("--repair-json", action="store_true", help="emit errors as JSON for an AI repair loop")
    p_validate.set_defaults(func=cmd_validate)

    p_catalog = sub.add_parser("catalog", help="list available assets")
    add_common(p_catalog, document=False)
    p_catalog.set_defaults(func=cmd_catalog)

    p_plan = sub.add_parser("plan", help="resolve layout and bake keyframes")
    add_common(p_plan)
    p_plan.add_argument("-o", "--output", help="where to write plan.json")
    p_plan.add_argument("--quality", choices=("preview", "final"))
    p_plan.set_defaults(func=cmd_plan)

    p_preview = sub.add_parser("preview", help="write a top-down SVG of the plan (no Blender needed)")
    add_common(p_preview)
    p_preview.add_argument("-o", "--output", help="where to write the SVG")
    p_preview.add_argument("--open", action="store_true", help="open it in a browser")
    p_preview.add_argument("--quality", choices=("preview", "final"))
    p_preview.set_defaults(func=cmd_preview)

    p_render = sub.add_parser("render", help="render the shot to video with Blender")
    add_common(p_render)
    p_render.add_argument("-o", "--output", help="output video path")
    p_render.add_argument("--quality", choices=("preview", "final"), help="preview=EEVEE, final=Cycles")
    p_render.add_argument(
        "--engine",
        choices=("auto", "eevee", "cycles"),
        default="auto",
        help="override the engine the quality setting implies; auto probes EEVEE and falls back to cycles",
    )
    p_render.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the EEVEE capability probe that --engine auto runs; assume cycles",
    )
    p_render.add_argument("--device", choices=("cpu", "gpu"), default="cpu", help="Cycles compute device")
    p_render.add_argument("--blender", help="path to the Blender executable")
    p_render.add_argument("--keep-blend", action="store_true", help="also save the .blend for inspection")
    p_render.add_argument("--frame", type=int, help="render one frame to PNG for visual review")
    p_render.add_argument("--samples", type=int, help="override render samples for a review pass")
    p_render.add_argument(
        "--resolution-scale", type=int, default=100,
        help="render at 1-100 percent of the document resolution",
    )
    p_render.set_defaults(func=cmd_render)

    p_doctor = sub.add_parser("doctor", help="report environment readiness")
    add_common(p_doctor, document=False)
    p_doctor.add_argument("--blender", help="path to the Blender executable")
    p_doctor.set_defaults(func=cmd_doctor)

    p_web = sub.add_parser("web", help="start the local browser workbench")
    p_web.add_argument("--host", default="127.0.0.1", help="interface to bind (default: localhost)")
    p_web.add_argument("--port", type=int, default=8765, help="TCP port (0 chooses a free port)")
    p_web.add_argument("--catalog", help="path to an asset catalog (defaults to assets/catalog.json)")
    p_web.add_argument("--open", action="store_true", help="open the workbench in the default browser")
    p_web.set_defaults(func=cmd_web)

    p_serve = sub.add_parser("serve", help="run the planning and render-job API for Kentsugi Studio")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--jobs", help="job storage directory (defaults to .kentsugi/jobs)")
    p_serve.set_defaults(func=cmd_serve)

    p_object = sub.add_parser("object", help="run prompt -> ObjectSpec -> blueprint -> checks -> knowledge")
    p_object.add_argument("prompt", help="object request in natural language")
    p_object.add_argument("-o", "--output", help="where to write the compiled scene JSON")
    p_object.add_argument("--catalog", help="asset catalog path")
    p_object.add_argument("--blueprints", help="blueprint registry path")
    p_object.add_argument("--knowledge", help="knowledge-library directory")
    p_object.add_argument("--max-attempts", type=int, default=3, help="bounded blueprint attempts")
    p_object.add_argument("--render-output", help="optional PNG or MP4 output path")
    p_object.add_argument("--artifact", help="previously rendered artifact to review")
    p_object.add_argument("--approve", action="store_true", help="approve a successful artifact")
    p_object.add_argument("--quality", choices=("preview", "final"))
    p_object.add_argument("--engine", choices=("auto", "eevee", "cycles"), default="auto")
    p_object.add_argument("--no-probe", action="store_true")
    p_object.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    p_object.add_argument("--blender", help="path to Blender")
    p_object.add_argument("--keep-blend", action="store_true")
    p_object.add_argument("--frame", type=int, help="render one review frame to PNG")
    p_object.add_argument("--samples", type=int, help="override review samples")
    p_object.add_argument("--resolution-scale", type=int, default=100)
    p_object.set_defaults(func=cmd_object)

    p_forge = sub.add_parser("forge", help="compile a declarative asset blueprint into a .blend")
    p_forge.add_argument("blueprint", help="path to an asset blueprint JSON")
    p_forge.add_argument("-o", "--output", help="output .blend path")
    p_forge.add_argument("--blender", help="path to Blender executable")
    p_forge.add_argument("--memory-directory", help="directory for forge memory")
    p_forge.add_argument("--promote-skill", action="store_true", help="promote the generated skill immediately")
    p_forge.set_defaults(func=cmd_forge)

    p_memory = sub.add_parser("memory", help="inspect and update local procedural memory")
    p_memory.add_argument("memory_action", choices=("skills", "retrieve", "events", "feedback", "promote"))
    p_memory.add_argument("query", nargs="?", help="retrieval query")
    p_memory.add_argument("--directory", help="memory directory (defaults to .kentsugi/memory)")
    p_memory.add_argument("--limit", type=int, default=10)
    p_memory.add_argument("--skill-id")
    p_memory.add_argument("--success", choices=("success", "failure"), default="success")
    p_memory.add_argument("--task", default="")
    p_memory.add_argument("--note", default="")
    p_memory.set_defaults(func=cmd_memory)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
