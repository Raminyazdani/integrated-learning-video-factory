"""extraction_adapter - dispatch to the right extractor by file type.

Stage 1 supports two input branches:
  * PDF  -> integrations.pdf_extractor.extract_pdf
  * PPTX -> integrations.pptx_extractor.extract_pptx
            + integrations.pptx_to_images.pptx_to_pngs (LibreOffice; may be PENDING)

Both branches return the **same** per-page dict shape so the downstream
manifest writer in agents.extraction_agent doesn't need branching.

Public API:
    extract(source_copy, *, source_type, pages_dir, text_dir, dpi=200,
            on_page=None) -> ExtractDispatchResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ExtractDispatchResult:
    source_type: str
    pages: List[Dict[str, Any]] = field(default_factory=list)
    # Extra metadata when PPTX path is used (image rendering may be PENDING).
    image_rendering_status: str = "complete"   # "complete" | "pending_local" | "failed"
    image_rendering_error: Optional[str] = None
    image_rendering_strategy: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _normalize_status_for_image(page: Dict[str, Any], image_present: bool) -> None:
    """Adjust the per-page extraction_status when image is absent for pptx."""
    if image_present:
        # Image exists -> clear the pptx_to_images PENDING warning, if any.
        warnings = [
            w for w in (page.get("warnings") or [])
            if w != "image_pending_pptx_to_images"
        ]
        page["warnings"] = warnings
        # If status was 'partial' only because of the image gap, promote to 'ok'
        # iff there's text; else leave as 'empty_text'.
        if page.get("extraction_status") == "partial":
            page["extraction_status"] = (
                "ok" if (page.get("text_char_count") or 0) > 0 else "empty_text"
            )
    # If image is still missing we keep status='partial' as set by the extractor.


def extract(
    source_copy: Path,
    *,
    source_type: str,
    pages_dir: Path,
    text_dir: Path,
    dpi: int = 200,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> ExtractDispatchResult:
    """Dispatch to the per-format extractor and return a uniform result."""
    source_copy = Path(source_copy)
    st = (source_type or "").lower()

    if st == "pdf":
        # PDF branch (Phase 2).
        from integrations.pdf_extractor import ensure_pymupdf, extract_pdf
        ensure_pymupdf()
        pages = extract_pdf(
            source_copy, pages_dir=pages_dir, text_dir=text_dir,
            dpi=dpi, on_page=on_page,
        )
        return ExtractDispatchResult(
            source_type="pdf",
            pages=pages,
            image_rendering_status="complete",
            image_rendering_strategy="pymupdf",
        )

    if st == "pptx":
        # PPTX branch (Phase 10).
        from integrations.pptx_extractor import ensure_python_pptx, extract_pptx
        from integrations.pptx_to_images import pptx_to_pngs
        ensure_python_pptx()
        pages = extract_pptx(
            source_copy, pages_dir=pages_dir, text_dir=text_dir, on_page=on_page,
        )
        # Render images with LibreOffice (may be PENDING).
        render = pptx_to_pngs(source_copy, pages_dir, dpi=dpi)
        result = ExtractDispatchResult(
            source_type="pptx",
            pages=pages,
            image_rendering_strategy=render.used_strategy,
        )
        if render.success:
            result.image_rendering_status = "complete"
            # Map success back into each page's status: clear the PENDING warning
            # and (when text is present) promote 'partial' to 'ok'.
            png_set = {p.resolve() for p in render.png_paths}
            for p in pages:
                image_present = Path(p["image_path"]).resolve() in png_set or Path(p["image_path"]).is_file()
                _normalize_status_for_image(p, image_present)
        elif render.pending:
            result.image_rendering_status = "pending_local"
            result.image_rendering_error = render.error
            result.notes.append(
                "PPTX text was extracted, but slide PNGs were NOT rendered "
                "because LibreOffice was unavailable. Downstream image-using "
                "stages will be PENDING until the run is repeated locally with "
                "soffice on PATH."
            )
            # Don't downgrade existing per-page statuses; they are already
            # 'partial' or 'empty_text' per the extractor.
        else:
            result.image_rendering_status = "failed"
            result.image_rendering_error = render.error
            result.notes.append(
                "LibreOffice was present but slide PNG rendering failed; see "
                "the extraction report's warnings for details. No fake PNGs "
                "were created."
            )
        for n in (render.notes or []):
            result.notes.append(n)
        return result

    raise ValueError(
        f"unsupported source_type {st!r}; expected 'pdf' or 'pptx'"
    )
