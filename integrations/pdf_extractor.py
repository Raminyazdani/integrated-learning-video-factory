"""pdf_extractor — PyMuPDF wrapper for Phase 2 PDF extraction.

Renders each page to PNG at configured DPI and extracts plain text.
Text is NFKC-normalized via integrations.text_cleaning.

Per docs/PIPELINE_SPEC.md Stage 1 (PDF branch). PPTX branch is Phase 10.

Public API:
  extract_pdf(pdf_path, pages_dir, text_dir, *, dpi=200, on_page=None) -> list[dict]

Each returned dict matches a page entry of extraction_manifest.schema.json's
page_entry (page_id, page_number, page_index, image_path, text_path,
text_sha256, image_sha256, text_char_count, extraction_status, warnings).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional

from .text_cleaning import clean_extracted_text

try:
    import fitz  # type: ignore  # PyMuPDF
except ImportError as _e:  # pragma: no cover
    fitz = None  # noqa: N816
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_pymupdf() -> None:
    """Raise a friendly error if PyMuPDF is not installed."""
    if fitz is None:
        raise RuntimeError(
            "PyMuPDF (`fitz`) is required for PDF extraction but is not installed.\n"
            "Install with: pip install pymupdf\n"
            f"Original ImportError: {_IMPORT_ERROR!r}"
        )


def sha256_of_file(path: str | Path) -> str:
    """Return SHA-256 hex digest of a file's bytes."""
    return _sha256_file(Path(path))


def extract_pdf(
    pdf_path: str | Path,
    pages_dir: str | Path,
    text_dir: str | Path,
    *,
    dpi: int = 200,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Extract one PNG and one TXT file per PDF page.

    Args:
      pdf_path:   source PDF path.
      pages_dir:  output dir for PNGs (created if missing).
      text_dir:   output dir for TXTs (created if missing).
      dpi:        render DPI (default 200, per configs/pipeline_config.yaml).
      on_page:    optional progress callback (current_index_1based, total).

    Returns:
      list of per-page metadata dicts ready to drop into the manifest.
    """
    ensure_pymupdf()
    pdf_path = Path(pdf_path)
    pages_dir = Path(pages_dir)
    text_dir = Path(text_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    # PyMuPDF zoom factor (72 dpi is PDF native).
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)  # type: ignore[union-attr]

    doc = fitz.open(str(pdf_path))  # type: ignore[union-attr]
    total = doc.page_count

    results: list[dict] = []
    try:
        for i in range(total):
            if on_page is not None:
                on_page(i + 1, total)

            page = doc.load_page(i)
            page_number = i + 1
            page_id = f"page_{page_number:03d}"

            warnings: list[str] = []
            status = "ok"

            # --- render PNG ---
            image_rel = pages_dir / f"{page_id}.png"
            try:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                pix.save(str(image_rel))
            except Exception as e:  # pragma: no cover
                warnings.append(f"render_failed: {e!r}")
                status = "render_failed"

            # --- extract text ---
            text_rel = text_dir / f"{page_id}.txt"
            try:
                raw_text = page.get_text("text") or ""
                cleaned = clean_extracted_text(raw_text)
            except Exception as e:  # pragma: no cover
                warnings.append(f"text_extract_failed: {e!r}")
                cleaned = ""
                status = "partial" if status == "ok" else status

            # Keep the written bytes identical to the bytes hashed below on Windows.
            text_rel.write_text(cleaned, encoding="utf-8", newline="\n")

            if status == "ok" and len(cleaned.strip()) == 0:
                status = "empty_text"
                warnings.append("page contains no extractable text (likely image-only)")

            # --- hashes ---
            text_sha = _sha256_bytes(cleaned.encode("utf-8"))
            image_sha = _sha256_file(image_rel) if image_rel.exists() else ""

            results.append(
                {
                    "page_id": page_id,
                    "page_number": page_number,
                    "page_index": i,
                    "image_path": str(image_rel.as_posix()),
                    "text_path": str(text_rel.as_posix()),
                    "image_sha256": image_sha,
                    "text_sha256": text_sha,
                    "text_char_count": len(cleaned),
                    "extraction_status": status,
                    "warnings": warnings,
                    "unreadable": status in ("render_failed",),
                }
            )
    finally:
        doc.close()

    return results
