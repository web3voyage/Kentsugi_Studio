# Kentsugi — environment notes

I'm Brian, and you're the lead dev here — implementation and the calls about
approach and architecture are yours. I'm support. What I can offer is legwork:
I've been through this codebase and the render pipeline end to end on this
specific machine, and some of what I found costs real time to discover the hard
way. That's what this file is for. Overrule any of it freely; you have the
context I don't.

Where this file and the README disagree, the README is stale — see
[Corrections](#corrections-to-the-readme).

## Verified before you run anything

**EEVEE cannot render on this machine. Use `--engine cycles`.**

This box's OpenGL implementation lacks `ARB_shader_draw_parameters` (needs GL
4.3), so headless EEVEE dies with `EXCEPTION_ACCESS_VIOLATION` inside Blender's
C code during shader compilation — process exit 11. It is **not catchable** from
`blender/render.py`; by then you're past the Python frame. Any fix has to pick
the engine *before* handing off to Blender.

Cycles CPU works fine headless — verified with real output, not theory.

Blender 5.2 is at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`
and is **not on PATH**; `cli.py` glob-finds it. Python is `C:\Python314\python.exe`.

## Commands

```bash
python -m unittest discover -s tests -t .     # 44 tests, all green as of now
python -m kentsugi doctor
python -m kentsugi preview examples/rainy_city.json    # fast loop, zero deps
python -m kentsugi render  examples/rainy_city.json -o out/x.mp4 --engine cycles --keep-blend
```

`preview` draws the same baked plan as a top-down SVG in milliseconds, so most
layout bugs are visible without starting Blender. A full 1280×720 render of the
example took ~84 minutes here — for a smoke test, set `render.duration` under the
document's `render` key (there's no `--frames` flag).

## Properties the code currently relies on

Flagging these because they're easy to break by accident, not to fence you in —
if one is in your way, it's your call, I'd just want to know.

- **Only `blender/render.py` imports bpy.** A second backend means reimplementing
  that one file and nothing else.
- **Pure stdlib CPython 3.14**, no deps, no venv.
- **The model emits relations and catalog asset ids — never coordinates, never
  code.** The solver turns relations into positions. Keeps a hallucinated asset a
  lookup miss rather than a file read or an interpreter path.
- **The plan hash is a property of the document, not the machine**, which is why
  the engine override sits in `cli.py:118` rather than in the baker.
- Not a git repo — no history to revert to, so worth reading before overwriting.

## Where things stand

The applier is **proven**, contrary to the README. Two real videos exist:
`out/smoke.mp4` (9,691 bytes) and `out/rainy_city.mp4` (222,630 bytes, valid
`ftypisom`/`avc1`, full example at 1280×720). Video muxing is no longer unknown.

Placeholder geometry — capsules and boxes — is *expected* output. Visual quality
is a function of the asset catalog, not the pipeline.

### Open bug

`--engine auto` is a label, not a detector. `kentsugi/cli.py:118-120` only
overrides when `engine != "auto"`, so `auto` accepts the baker's pick — and
`ENGINE_BY_QUALITY` (`kentsugi/timeline.py:18`) maps `preview` → EEVEE. So the
default `render` invocation drives straight into the uncatchable crash on this
box. The `--engine cycles` escape hatch exists, but only helps someone who
already knows.

If you take it: probe by **assigning and catching**, not by reading `enum_items`
— in background mode `render.engine` under-reports, enumerating only
`BLENDER_EEVEE` while still accepting `"CYCLES"` on assignment. EEVEE's
identifier was `BLENDER_EEVEE_NEXT` in 4.2–4.5 and reverted to `BLENDER_EEVEE`
in 5.x, so try both. Blender also exits 0 even when the embedded script raises,
which is why `cmd_render` checks the output file exists and is non-empty.

## Corrections to the README

`README.md:103-135` will mislead you:

- "The Blender applier has never been run" — false, see above.
- "Blender is not installed on the development machine" — false, 5.2 is installed.
- Roadmap steps 2 and 3 still describe the render as unverified.

## Asking me for things

Happy to take the unglamorous work: running the suite, long Cycles renders,
bisecting a layout bug in `preview`, digging through Blender's headless
behaviour, verifying output containers. Say what you want checked and I'll come
back with findings rather than opinions.
