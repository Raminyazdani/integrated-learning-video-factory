"""pptx_merge - combine multiple PPTX files into one.

Phase 6A scope: stub that defines the public API only. Phase 6B/6C will
implement real merge using python-pptx slide copy. For the smoke test we
do not need to merge anything yet.
"""
from __future__ import annotations

from pathlib import Path


def merge_pptx(inputs, output_path):
    """Concatenate PPTX files in `inputs` into `output_path`.

    Phase 6A: not yet implemented. Raises NotImplementedError so callers in
    later phases know to wire in the real implementation.
    """
    raise NotImplementedError(
        "pptx_merge.merge_pptx is a Phase 6B feature; not used by the Phase 6A smoke test."
    )


def can_merge():
    """Capability probe for callers that want to gate on availability."""
    return False
