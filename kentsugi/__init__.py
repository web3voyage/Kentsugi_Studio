"""Kentsugi — prompt-driven 3D shot authoring.

The scene document is the product. Everything else is a consumer of it:
the validator checks it, the layout solver resolves it, the timeline baker
flattens it into keyframes, and the Blender applier renders those keyframes.

Deliberately, no module in this package imports ``bpy``. The whole pipeline
up to the final render is testable under plain CPython.
"""

__all__ = ["KENTSUGI_VERSION"]

KENTSUGI_VERSION = "0.1.0"
