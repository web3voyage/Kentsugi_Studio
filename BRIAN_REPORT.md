# BRIAN_REPORT.md — task 001, automatic render-engine selection

Brian. Task 001 from `BRIAN_TASK.md` is done and tested. Suite is green at 61
tests, up from 44. Default `render` no longer crashes on this box.

## Changed files

| File | Change |
| --- | --- |
| `blender/probe_engine.py` | New. Throwaway EEVEE probe; renders one 32x32 frame, prints `KENTSUGI_PROBE_OK` only after the image is on disk. |
| `kentsugi/engine.py` | New. `probe_eevee` (cached per executable) and `resolve_engine`. No `bpy` import. |
| `kentsugi/cli.py` | Imports `resolve_engine`; resolves `auto` in `cmd_render` once Blender is known; adds `--no-probe`; `--engine` help corrected. |
| `tests/test_engine.py` | New. 21 tests, all mocked — no Blender launched. |
| `README.md` | Corrected the two false claims; documented probe behaviour; roadmap step 2 marked done. |

Untouched, as you asked: `validate.py`, `layout.py`, `timeline.py` and their
tests. `bpy` stays confined to `blender/`. Explicit `--engine` overrides and the
post-exit output check both still work.

## You were right about the probe

Your requirement to render a real frame rather than check the enum was the
thing that made this work. The crash lands *after* the assignment succeeds:

```
Error: The OpenGL implementation doesn't support ARB_shader_draw_parameters
gpu.shader | ERROR eevee_light_culling_cull Linking: Out of resource error.
Error : EXCEPTION_ACCESS_VIOLATION
EXIT=11
```

Assign-and-catch — what I had originally suggested in `AGENTS.md` — would have
reported EEVEE as working and handed the crash straight to the real render. The
subprocess boundary is what makes the access violation observable at all: it
becomes exit code 11 instead of taking the CLI down with it.

## Behaviour

| Request | Plan says | Result |
| --- | --- | --- |
| `auto` (default) | EEVEE | probes; Cycles here, with a printed reason |
| `auto` | Cycles | Cycles, no probe |
| `cycles` | either | Cycles, no probe |
| `eevee` | either | EEVEE, no probe — override stays an override |
| `--no-probe` | EEVEE | Cycles, no Blender launched |

Probe failure is any of: non-zero exit, crash, timeout (180s), `OSError`, or a
zero exit without the sentinel. That last case matters — Blender exits 0 even
when an embedded `--python` script raises, so returncode alone is not enough.

## Verification

- **61 tests pass** in 0.31s, stdlib `unittest`, no real render in the suite.
- **Probe against Blender 5.2**: `probe_eevee` → `False`, for the access
  violation above rather than a bug in the probe. Confirmed by running it
  manually and reading the crash output.
- **End-to-end**: `render .kentsugi/accept_default.json` with no `--engine`
  flag, preview quality, 320x180, 7 frames. Exit 0. Printed `EEVEE cannot
  render here (probe failed); using Cycles instead`, then wrote
  `out/accept_default.mp4` — 4,971 bytes, valid `ftypisom`. This is the exact
  invocation that used to crash.

## Remaining risks

1. **One machine, one GPU, one Blender.** The probe logic is general; the
   observed result is not. A box with working EEVEE has never exercised the
   success path against real Blender — only mocks.
2. **Probe cost.** ~8-10s per process when the plan asks for EEVEE, and on a
   failing box it is paid to learn "no" every run. Cached per process, not
   across runs. If that annoys you in a tight loop, `--engine cycles` skips it,
   and a cache file keyed on Blender path + build hash would kill it entirely —
   I left that out as it is a design call in your lane.
3. **`ENGINE_BY_QUALITY` still maps `preview` → EEVEE** at `timeline.py:18`. I
   did not touch it, since `timeline.py` is yours this round. The plan hash
   therefore stays machine-independent and selection happens at render time,
   which I think is the right split — but if you'd rather the baker never
   proposed EEVEE, that's a one-line change on your side.
4. **`--engine eevee` still crashes on this box**, by design. An explicit
   override is a user saying they know better. Worth a warning if you disagree.
5. **Probe writes to a temp dir** and cleans up via `TemporaryDirectory`. If
   Blender is killed mid-probe the directory is still removed, but a crash dump
   (`blender.crash.txt`) lands in `%TEMP%` and is not cleaned up. Cosmetic.

## Note on two copies

The repo I changed is `C:\Users\BUYPC COMPUTERS\Kentsugi`, per your task file.
You are working in `Documents\Codex\2026-08-06\re\kentsugi_repo`, which has its
own copy and its own git history. These will drift. Mine is not a git repo, so
there is no revert here — say which one is canonical and I will keep to it.
