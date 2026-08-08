"""Throwaway EEVEE capability probe. Not part of the pipeline.

Run headless:

    blender --background --factory-startup --python blender/probe_engine.py \
        -- --output <png>

Renders one tiny frame with EEVEE and prints ``KENTSUGI_PROBE_OK`` only once a
real image has reached disk. Accepting the engine name proves nothing: a box
whose OpenGL implementation cannot compile EEVEE's shaders survives the
assignment and then dies inside Blender's C code with an access violation.
That takes the whole process down rather than raising, which is precisely why
this runs in a subprocess the caller can outlive.
"""

import argparse
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]

SENTINEL = "KENTSUGI_PROBE_OK"

# BLENDER_EEVEE_NEXT for the 4.2-4.5 line, BLENDER_EEVEE before and after.
EEVEE_IDENTIFIERS = ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE")


def parse_args(argv: list[str]) -> argparse.Namespace:
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(prog="kentsugi-probe")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv)
    output = Path(args.output)

    scene = bpy.context.scene
    render = scene.render

    for identifier in EEVEE_IDENTIFIERS:
        try:
            render.engine = identifier
        except TypeError:
            continue
        if render.engine == identifier:
            break
    else:
        print("kentsugi: no EEVEE identifier accepted by this build")
        return 1

    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "taa_render_samples"):
        eevee.taa_render_samples = 1

    render.resolution_x = 32
    render.resolution_y = 32
    render.resolution_percentage = 100
    render.image_settings.file_format = "PNG"
    render.filepath = str(output)

    # Anything at all in frame, so a black-image false positive is unlikely.
    bpy.ops.mesh.primitive_cube_add(size=1.0)

    bpy.ops.render.render(write_still=True)

    if not output.is_file() or output.stat().st_size == 0:
        print("kentsugi: EEVEE render produced no image")
        return 1

    print(SENTINEL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
