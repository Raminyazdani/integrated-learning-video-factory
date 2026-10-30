"""pptx_extractor - Stage 1 PPTX text/notes extraction.

Uses ``python-pptx`` to read slide titles, body text, and speaker notes.
Image rendering is handled separately by ``pptx_to_images`` (LibreOffice).

The output shape mirrors ``pdf_extractor.extract_pdf`` so downstream stages
(classification, packaging, narration) need no changes.

Per-slide dict:
    {
      "page_number":         1,                       # 1-based
      "page_id":             "page_001",
      "image_path":          "<path or empty>",      # filled by pptx_to_images
      "text_path":           "<path>",
      "text_char_count":     int,
      "extraction_status":   "ok" | "empty_text" | "render_failed" | "partial",
      "warnings":            [str, ...],
      "speaker_notes":       str,                     # extra field, optional
    }

Image paths are written even when the image step is PENDING - they hold the
expected target so the manifest is self-describing. Callers should check
``extraction_status`` (and the image file's existence) before reading.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .atomic_io import atomic_write_text


@dataclass
class SlideText:
    title: str = ""
    body: str = ""
    notes: str = ""

    def merged(self) -> str:
        parts = []
        if self.title.strip():
            parts.append(self.title.strip())
        if self.body.strip():
            parts.append(self.body.strip())
        if self.notes.strip():
            parts.append("[NOTES]")
            parts.append(self.notes.strip())
        return "\n\n".join(parts)


def ensure_python_pptx() -> None:
    try:
        import pptx  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "python-pptx is required for PPTX extraction. "
            "Install it with `pip install python-pptx`."
        ) from exc


def _extract_one_slide(slide) -> SlideText:
    """Pull text from a python-pptx ``Slide`` object."""
    title = ""
    body_lines: List[str] = []

    # python-pptx exposes a slide.shapes.title shortcut (may be None).
    try:
        title_shape = getattr(slide.shapes, "title", None)
        if title_shape is not None and title_shape.has_text_frame:
            title = title_shape.text_frame.text or ""
    except Exception:
        pass

    # Walk all shapes for body text, skipping the title shape if present.
    for shape in slide.shapes:
        try:
            if shape is getattr(slide.shapes, "title", None):
                continue
            if getattr(shape, "has_text_frame", False) and shape.text_frame is not None:
                text = (shape.text_frame.text or "").strip()
                if text:
                    body_lines.append(text)
        except Exception:
            continue

    notes_text = ""
    try:
        if getattr(slide, "has_notes_slide", False) and slide.notes_slide is not None:
            tf = slide.notes_slide.notes_text_frame
            if tf is not None:
                notes_text = (tf.text or "").strip()
    except Exception:
        pass

    return SlideText(
        title=title.strip(),
        body="\n".join(body_lines).strip(),
        notes=notes_text,
    )


def extract_pptx(
    source_copy: Path,
    *,
    pages_dir: Path,
    text_dir: Path,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """Read a PPTX and write per-slide TXT files. Returns per-slide dicts.

    Image generation is **not** done here. The ``image_path`` field is the
    expected target path so the manifest is complete; the actual PNG must be
    produced by ``pptx_to_images.pptx_to_pngs``. If that step fails or is
    skipped, the per-slide ``extraction_status`` is set to ``partial`` and a
    warning is added.
    """
    ensure_python_pptx()
    from pptx import Presentation  # type: ignore

    source_copy = Path(source_copy)
    pages_dir = Path(pages_dir)
    text_dir = Path(text_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    prs = Presentation(str(source_copy))
    slides = list(prs.slides)
    total = len(slides)

    out: List[Dict[str, Any]] = []
    for idx, slide in enumerate(slides, start=1):
        if on_page is not None:
            try:
                on_page(idx, total)
            except Exception:
                pass

        page_id = f"page_{idx:03d}"
        text_path = text_dir / f"{page_id}.txt"
        image_path = pages_dir / f"{page_id}.png"

        st = _extract_one_slide(slide)
        merged = st.merged()
        # Always write a TXT (possibly empty) so the file count matches.
        atomic_write_text(text_path, merged + ("\n" if merged else ""))

        warnings: List[str] = []
        status = "ok" if merged.strip() else "empty_text"
        if not image_path.is_file():
            warnings.append("image_pending_pptx_to_images")
            if status == "ok":
                # Text present but image not yet rendered.
                status = "partial"
            # When status is "empty_text" we don't downgrade further.

        out.append({
            "page_number": idx,
            "page_id": page_id,
            "image_path": str(image_path),
            "text_path": str(text_path),
            "text_char_count": len(merged),
            "extraction_status": status,
            "warnings": warnings,
            "speaker_notes": st.notes,
        })

    return out
