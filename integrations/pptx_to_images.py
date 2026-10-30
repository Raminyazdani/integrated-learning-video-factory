"""pptx_to_images - render PPTX slides to per-page PNGs.

Two strategies, picked in this order:

  1. **soffice + pdftoppm** (preferred, deterministic): convert PPTX to PDF
     via LibreOffice headless, then rasterize the PDF page-by-page with
     pdftoppm. Same DPI as the PDF pipeline (Phase 2).
  2. **pymupdf-only fallback**: if pdftoppm is missing but PyMuPDF is
     installed, use it to rasterize the intermediate PDF.

If LibreOffice is not on PATH, this module returns a PENDING result and
NEVER writes a fake image. The caller (extraction_adapter / extraction_agent)
treats the per-slide status as ``partial`` and surfaces a clear PENDING
marker so downstream stages don't accidentally treat empty pages as ready.

Public API:
    detect_soffice() -> SOfficeInfo
    detect_pdftoppm() -> SimpleBinInfo
    pptx_to_pngs(pptx_path, pages_dir, *, dpi=200) -> PptxRenderResult
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class SOfficeInfo:
    binary: Optional[str] = None
    version: Optional[str] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class SimpleBinInfo:
    name: str
    binary: Optional[str] = None
    version: Optional[str] = None
    available: bool = False
    error: Optional[str] = None


@dataclass
class PptxRenderResult:
    success: bool = False
    pending: bool = False
    pages_dir: Optional[Path] = None
    png_paths: List[Path] = field(default_factory=list)
    used_strategy: Optional[str] = None  # "soffice+pdftoppm" | "soffice+pymupdf"
    wall_seconds: float = 0.0
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _find_soffice() -> Optional[str]:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def detect_soffice() -> SOfficeInfo:
    info = SOfficeInfo()
    path = _find_soffice()
    if not path:
        info.error = "LibreOffice (soffice / libreoffice) not on PATH"
        return info
    info.binary = path
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        info.error = f"soffice --version failed: {exc!r}"
        return info
    if out.returncode != 0:
        info.error = f"soffice --version returned {out.returncode}"
        return info
    first = (out.stdout or out.stderr or "").splitlines()[0] if (out.stdout or out.stderr) else ""
    info.version = first.strip()
    info.available = True
    return info


def detect_pdftoppm() -> SimpleBinInfo:
    info = SimpleBinInfo(name="pdftoppm")
    path = shutil.which("pdftoppm")
    if not path:
        info.error = "pdftoppm not on PATH (install poppler-utils)"
        return info
    info.binary = path
    try:
        out = subprocess.run(
            [path, "-v"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        info.error = f"pdftoppm -v failed: {exc!r}"
        return info
    # pdftoppm prints version to stderr.
    raw = (out.stderr or out.stdout or "").splitlines()[0] if (out.stderr or out.stdout) else ""
    info.version = raw.strip()
    info.available = True
    return info


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _convert_pptx_to_pdf(soffice: str, pptx_path: Path, work_dir: Path,
                         *, timeout: int = 180) -> Optional[Path]:
    """Run LibreOffice headless to convert PPTX -> PDF. Return PDF path."""
    cmd = [
        soffice, "--headless",
        "--convert-to", "pdf",
        "--outdir", str(work_dir),
        str(pptx_path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "HOME": str(work_dir)},
    )
    if proc.returncode != 0:
        return None
    pdfs = list(work_dir.glob("*.pdf"))
    if not pdfs:
        return None
    # If multiple, pick the one matching the input stem.
    target = work_dir / f"{pptx_path.stem}.pdf"
    if target.is_file():
        return target
    return pdfs[0]


def _pdf_to_pngs_pdftoppm(pdftoppm: str, pdf_path: Path, pages_dir: Path,
                          *, dpi: int = 200, timeout: int = 300) -> List[Path]:
    """Render PDF -> PNG per page using pdftoppm. Returns sorted list of PNG paths."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    # We render to a tmp prefix, then rename to page_NNN.png.
    with tempfile.TemporaryDirectory() as tmpd:
        tmp_prefix = Path(tmpd) / "ppslide"
        cmd = [
            pdftoppm, "-r", str(dpi), "-png", str(pdf_path), str(tmp_prefix),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pdftoppm returned {proc.returncode}: {(proc.stderr or '')[:200]}"
            )
        # pdftoppm names files like ppslide-1.png, ppslide-2.png, ... (or -01- if many)
        rendered = sorted(Path(tmpd).glob("ppslide-*.png"))
        out_paths: List[Path] = []
        for i, src in enumerate(rendered, start=1):
            dst = pages_dir / f"page_{i:03d}.png"
            shutil.move(str(src), str(dst))
            out_paths.append(dst)
    return out_paths


def _pdf_to_pngs_pymupdf(pdf_path: Path, pages_dir: Path,
                         *, dpi: int = 200) -> List[Path]:
    """Fallback rasterizer using PyMuPDF."""
    try:
        import fitz  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PyMuPDF not importable: {exc!r}") from exc
    pages_dir.mkdir(parents=True, exist_ok=True)
    out_paths: List[Path] = []
    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            dst = pages_dir / f"page_{i:03d}.png"
            pix.save(str(dst))
            out_paths.append(dst)
    finally:
        doc.close()
    return out_paths


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def pptx_to_pngs(
    pptx_path: Path,
    pages_dir: Path,
    *,
    dpi: int = 200,
    soffice_timeout: int = 180,
    raster_timeout: int = 300,
) -> PptxRenderResult:
    """Render every slide of ``pptx_path`` into ``pages_dir/page_NNN.png``.

    Returns ``PptxRenderResult`` with:
      - ``success=True`` if PNGs were produced
      - ``pending=True`` if a required binary is missing (no fake files)
      - ``error`` populated when something failed mid-way
    """
    pptx_path = Path(pptx_path)
    pages_dir = Path(pages_dir)
    result = PptxRenderResult(pages_dir=pages_dir)

    if not pptx_path.is_file():
        result.error = f"pptx not found: {pptx_path}"
        return result

    soffice = detect_soffice()
    if not soffice.available:
        result.pending = True
        result.error = soffice.error or "LibreOffice unavailable"
        result.notes.append(
            "LibreOffice (soffice) was not found on PATH. Install LibreOffice "
            "on local Windows (or use a runner that has it). Text extraction "
            "via python-pptx will still complete; only slide PNG rendering is "
            "PENDING."
        )
        return result

    pdftoppm = detect_pdftoppm()
    started = time.time()
    with tempfile.TemporaryDirectory() as work:
        work_dir = Path(work)
        try:
            pdf_path = _convert_pptx_to_pdf(
                soffice.binary, pptx_path, work_dir, timeout=soffice_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result.error = f"soffice timeout: {exc!r}"
            result.wall_seconds = round(time.time() - started, 3)
            return result
        except Exception as exc:  # noqa: BLE001
            result.error = f"soffice invocation failed: {exc!r}"
            result.wall_seconds = round(time.time() - started, 3)
            return result

        if pdf_path is None:
            result.error = "soffice did not produce an output PDF"
            result.wall_seconds = round(time.time() - started, 3)
            return result

        try:
            if pdftoppm.available:
                pngs = _pdf_to_pngs_pdftoppm(
                    pdftoppm.binary, pdf_path, pages_dir,
                    dpi=dpi, timeout=raster_timeout,
                )
                result.used_strategy = "soffice+pdftoppm"
            else:
                pngs = _pdf_to_pngs_pymupdf(pdf_path, pages_dir, dpi=dpi)
                result.used_strategy = "soffice+pymupdf"
        except Exception as exc:  # noqa: BLE001
            result.error = f"rasterization failed: {exc!r}"
            result.wall_seconds = round(time.time() - started, 3)
            return result

    result.wall_seconds = round(time.time() - started, 3)
    if not pngs:
        result.error = "rasterization produced no PNGs"
        return result
    result.png_paths = pngs
    result.success = True
    return result
