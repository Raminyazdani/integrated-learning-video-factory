"""text_cleaning — Unicode normalization helpers for extracted text.

For Phase 2 the only required operation is Unicode NFKC normalization (per
docs/PIPELINE_SPEC.md Stage 1). Later phases (Stage 4 narration, Stage 8 TTS)
will add Persian-specific symbol maps and term replacements; those are
intentionally not implemented here yet.
"""
from __future__ import annotations

import re
import unicodedata


def nfkc(text: str) -> str:
    """Apply Unicode NFKC normalization."""
    if text is None:
        return ""
    return unicodedata.normalize("NFKC", text)


def strip_control_chars(text: str) -> str:
    """Remove ASCII control chars except newline (\\n) and tab (\\t)."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def collapse_whitespace(text: str) -> str:
    """Collapse runs of inline whitespace and trim trailing spaces per line."""
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        cleaned.append(line)
    return "\n".join(cleaned)


def clean_extracted_text(text: str) -> str:
    """Full cleaning chain used by the PDF extractor.

    Order:
      1. NFKC normalize.
      2. Strip control chars.
      3. Collapse whitespace per line.
    Newlines are preserved (they carry structure for downstream stages).
    """
    if not text:
        return ""
    out = nfkc(text)
    out = strip_control_chars(out)
    out = collapse_whitespace(out)
    return out.strip() + ("\n" if out.endswith("\n") is False and out else "")
