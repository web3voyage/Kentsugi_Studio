"""Render-engine capability selection.

``--engine auto`` used to be a label rather than a decision: it accepted
whatever the baker's quality setting implied, and ``preview`` implies EEVEE. On
a machine whose OpenGL implementation cannot compile EEVEE's shaders that is a
hard crash inside Blender's C code -- an access violation, not an exception, so
nothing in the applier can catch it.

So we ask a throwaway subprocess to render one 32x32 EEVEE frame first. If it
comes back with the sentinel, EEVEE works here; any other outcome (non-zero
exit, signal, timeout, missing sentinel) means Cycles.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

PROBE_SCRIPT = Path(__file__).resolve().parent.parent / "blender" / "probe_engine.py"
PROBE_SENTINEL = "KENTSUGI_PROBE_OK"
PROBE_TIMEOUT_SECONDS = 180

EEVEE = "EEVEE"
CYCLES = "CYCLES"

_cache: dict[Path, bool] = {}


def clear_cache() -> None:
    """Forget probe results. For tests; a process otherwise probes once."""
    _cache.clear()


def probe_eevee(blender: Path, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Return whether EEVEE can actually render a frame with this Blender.

    Cached per executable, since the answer is a property of the machine and
    the build rather than of the document being rendered.
    """
    key = Path(blender)
    if key in _cache:
        return _cache[key]

    result = _run_probe(key, timeout)
    _cache[key] = result
    return result


def _run_probe(blender: Path, timeout: float) -> bool:
    if not PROBE_SCRIPT.is_file():
        return False

    with tempfile.TemporaryDirectory(prefix="kentsugi-probe-") as tmp:
        image = Path(tmp) / "probe.png"
        command = [
            str(blender), "--background", "--factory-startup",
            "--python", str(PROBE_SCRIPT),
            "--", "--output", str(image),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

    # A crashed probe is the expected negative result, so returncode is the
    # first gate; the sentinel then rules out Blender exiting 0 after the
    # embedded script failed, which it does.
    if completed.returncode != 0:
        return False
    return PROBE_SENTINEL in (completed.stdout or "")


def resolve_engine(
    requested: str | None,
    planned: str,
    blender: Path | None,
    *,
    probe: bool = True,
) -> tuple[str, str | None]:
    """Decide the engine to render with.

    ``requested`` is the ``--engine`` flag, ``planned`` what the baker chose.
    Returns the engine plus a note to print when the choice was not the
    obvious one, or None when there is nothing worth saying.
    """
    explicit = {"eevee": EEVEE, "cycles": CYCLES}
    if requested in explicit:
        return explicit[requested], None

    if planned != EEVEE:
        return planned, None

    if blender is None:
        return CYCLES, "no Blender to probe; choosing Cycles"

    if not probe:
        return CYCLES, "probe skipped; choosing Cycles"

    if probe_eevee(blender):
        return EEVEE, None
    return CYCLES, "EEVEE cannot render here (probe failed); using Cycles instead"
