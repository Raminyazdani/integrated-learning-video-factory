"""pptx_to_pdf - export a PPTX to PDF via LibreOffice (soffice).

Used by the smoke test to validate the toolchain end-to-end (Persian
glyphs + RTL where supported by Office). When soffice is unavailable,
returns a structured "skipped" result so the caller can document the
limitation.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PdfExportResult:
    success: bool
    soffice_path: Optional[str]
    pdf_path: Optional[Path]
    pdf_bytes: int
    skipped_reason: str = ""
    stderr_tail: str = ""


def detect_soffice():
    """Return absolute path to libreoffice/soffice if installed, else None."""
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    return None


def pptx_to_pdf(pptx_path, output_dir, *, timeout_s=120):
    """Convert pptx_path to PDF inside output_dir using soffice."""
    pptx_path = Path(pptx_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    soffice = detect_soffice()
    if soffice is None:
        return PdfExportResult(
            success=False, soffice_path=None, pdf_path=None, pdf_bytes=0,
            skipped_reason="libreoffice/soffice not installed; PDF export skipped",
        )

    try:
        proc = subprocess.run(
            [
                soffice, "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
                "--convert-to", "pdf",
                "--outdir", str(output_dir),
                str(pptx_path),
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        stderr_tail = (proc.stderr or "")[-1500:]
    except Exception as exc:
        return PdfExportResult(
            success=False, soffice_path=soffice, pdf_path=None, pdf_bytes=0,
            skipped_reason="soffice invocation failed: " + repr(exc),
        )

    expected = output_dir / (pptx_path.stem + ".pdf")
    ok = expected.exists() and expected.stat().st_size > 0
    return PdfExportResult(
        success=ok,
        soffice_path=soffice,
        pdf_path=(expected if ok else None),
        pdf_bytes=(expected.stat().st_size if ok else 0),
        skipped_reason="" if ok else "soffice ran but PDF was not produced",
        stderr_tail=stderr_tail,
    )
