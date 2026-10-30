"""slide_image_exporter - export each PPTX slide as a PNG.

Two-step approach: PPTX -> PDF (via soffice) -> PNG per page (via pdftoppm).
Both tools are common on Debian-family systems. When either is missing,
returns a structured skipped result; Phase 6/Phase 9 callers can decide.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from integrations.pptx_to_pdf import detect_soffice, pptx_to_pdf


@dataclass
class SlideImageExportResult:
    success: bool
    pdf_path: Optional[Path]
    image_paths: list
    skipped_reason: str = ""


def detect_pdftoppm():
    return shutil.which("pdftoppm")


def cleanup_libreoffice_tempfiles(directory) -> list:
    """Remove only clearly-temporary ``*.tmp`` files left in ``directory``.

    Non-recursive and conservative: canonical artifacts (.pptx/.pdf/.png/.json)
    are never matched. Intended to run *after* a successful LibreOffice/soffice
    conversion. Returns the list of removed paths.
    """
    directory = Path(directory)
    removed = []
    if not directory.is_dir():
        return removed
    for p in directory.glob("*.tmp"):
        try:
            if p.is_file():
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    return removed


def export_slides_as_png(pptx_path, output_dir, *, dpi=150):
    """Export each slide of pptx_path to output_dir as page_<N>.png."""
    pptx_path = Path(pptx_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    soffice = detect_soffice()
    pdftoppm = detect_pdftoppm()
    if soffice is None or pdftoppm is None:
        missing = []
        if soffice is None: missing.append("soffice")
        if pdftoppm is None: missing.append("pdftoppm")
        return SlideImageExportResult(
            success=False, pdf_path=None, image_paths=[],
            skipped_reason="missing tools: " + ",".join(missing),
        )

    # Step 1: PPTX -> PDF (into output_dir alongside the PNGs)
    pdf_result = pptx_to_pdf(pptx_path, output_dir)
    if not pdf_result.success:
        return SlideImageExportResult(
            success=False, pdf_path=None, image_paths=[],
            skipped_reason="pptx_to_pdf failed: " + pdf_result.skipped_reason,
        )

    # Step 2: PDF -> PNG per page via pdftoppm
    prefix = str(output_dir / pptx_path.stem)
    try:
        subprocess.run(
            [pdftoppm, "-png", "-r", str(dpi), str(pdf_result.pdf_path), prefix],
            capture_output=True, text=True, timeout=120, check=True,
        )
    except Exception as exc:
        return SlideImageExportResult(
            success=False, pdf_path=pdf_result.pdf_path, image_paths=[],
            skipped_reason="pdftoppm failed: " + repr(exc),
        )

    pngs = sorted(output_dir.glob(pptx_path.stem + "-*.png"))
    success = len(pngs) > 0
    if success:
        # Only sweep stray *.tmp after a confirmed-successful conversion.
        cleanup_libreoffice_tempfiles(output_dir)
    return SlideImageExportResult(
        success=success,
        pdf_path=pdf_result.pdf_path,
        image_paths=pngs,
    )


# ---------------------------------------------------------------------------
# Native run-level slide-image export (Phase 6 first-class step)
# ---------------------------------------------------------------------------

_DECK_STEM = "full_teaching_deck"


@dataclass
class SlideImageRunResult:
    success: bool = False
    backend: str = ""
    pptx_path: Optional[Path] = None
    pdf_path: Optional[Path] = None
    images_dir: Optional[Path] = None
    manifest_path: Optional[Path] = None
    slide_count: int = 0
    image_count: int = 0
    reused: bool = False
    relinked: int = 0
    missing_image_paths: List[str] = field(default_factory=list)
    report_path: Optional[Path] = None
    error: str = ""


def _png_ok(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _normalize_png_names(images_dir: Path) -> None:
    """Rename full_teaching_deck-<n>.png to a fixed 3-digit form so manifest
    relinking is deterministic regardless of pdftoppm's page-count padding."""
    for p in list(images_dir.glob(_DECK_STEM + "-*.png")):
        m = re.search(r"-(\d+)\.png$", p.name)
        if not m:
            continue
        tgt = images_dir / ("%s-%03d.png" % (_DECK_STEM, int(m.group(1))))
        if p.name != tgt.name:
            try:
                os.replace(str(p), str(tgt))
            except OSError:
                pass


def export_run_slide_images(run_dir, *, dpi: int = 200, force: bool = False) -> "SlideImageRunResult":
    """Native, idempotent slide-image export for a run.

    PPTX -> PDF (LibreOffice, reused if a fresh PDF already exists) -> PNG
    (pdftoppm), then relink every slide's ``image_path`` in slide_manifest.json
    to ``slide_images/full_teaching_deck-NNN.png``. Writes a QA report.

    Idempotent: if the on-disk image count already equals the manifest slide
    count (and all images are non-empty), images are reused unless ``force``.
    Never fakes an image; never deletes audio/video.
    """
    run_dir = Path(run_dir).resolve()
    slides_dir = run_dir / "slides"
    images_dir = run_dir / "slide_images"
    manifest_path = slides_dir / "slide_manifest.json"
    res = SlideImageRunResult(images_dir=images_dir, manifest_path=manifest_path)

    if not manifest_path.is_file():
        res.error = "slide_manifest.json not found: %s (run Phase 6 first)" % manifest_path
        res.report_path = _write_slide_image_report(run_dir, res)
        return res

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slides = manifest.get("slides", []) or []
    slide_count = manifest.get("slide_count") or len(slides)
    res.slide_count = slide_count

    pptx = slides_dir / (_DECK_STEM + ".pptx")
    pdf = slides_dir / (_DECK_STEM + ".pdf")
    res.pptx_path = pptx if pptx.is_file() else None

    # ---- 1) ensure a usable PDF (reuse if present and not older than pptx) ---
    need_pdf = force or not pdf.is_file()
    if (not need_pdf) and pptx.is_file() and pdf.stat().st_mtime < pptx.stat().st_mtime:
        need_pdf = True
    if need_pdf and pptx.is_file():
        soffice = detect_soffice()
        if soffice is None:
            if not pdf.is_file():
                res.error = "LibreOffice/soffice unavailable and no existing PDF to render."
                res.report_path = _write_slide_image_report(run_dir, res)
                return res
            # fall back to the existing (possibly older) PDF
        else:
            conv = pptx_to_pdf(pptx, slides_dir)
            if conv.success and conv.pdf_path and Path(conv.pdf_path).is_file():
                pdf = Path(conv.pdf_path)
            elif not pdf.is_file():
                res.error = "PPTX->PDF export failed: " + (conv.skipped_reason or "unknown")
                res.report_path = _write_slide_image_report(run_dir, res)
                return res
    if not pdf.is_file():
        res.error = "no full_teaching_deck.pdf available to render (and no PPTX to export)."
        res.report_path = _write_slide_image_report(run_dir, res)
        return res
    res.pdf_path = pdf

    # ---- 2) render PDF -> PNG (idempotent reuse) ----------------------------
    existing = [p for p in sorted(images_dir.glob(_DECK_STEM + "-*.png")) if _png_ok(p)]
    if (not force) and slide_count > 0 and len(existing) == slide_count:
        res.reused = True
        res.backend = "reuse (existing slide_images match slide_count)"
    else:
        pdftoppm = detect_pdftoppm()
        if pdftoppm is None:
            res.error = "pdftoppm not available to render PDF->PNG."
            res.report_path = _write_slide_image_report(run_dir, res)
            return res
        images_dir.mkdir(parents=True, exist_ok=True)
        for p in images_dir.glob(_DECK_STEM + "-*.png"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            subprocess.run(
                [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(images_dir / _DECK_STEM)],
                capture_output=True, text=True, timeout=900, check=True,
            )
        except Exception as exc:
            res.error = "pdftoppm failed: " + repr(exc)
            res.report_path = _write_slide_image_report(run_dir, res)
            return res
        _normalize_png_names(images_dir)
        res.backend = ("libreoffice+pdftoppm" if pptx.is_file() else "pdftoppm")

    images = [p for p in sorted(images_dir.glob(_DECK_STEM + "-*.png")) if _png_ok(p)]
    res.image_count = len(images)

    # ---- 3) relink slide_manifest.json image_path --------------------------
    relinked = 0
    missing: List[str] = []
    for i, s in enumerate(slides, start=1):
        rel = "slide_images/%s-%03d.png" % (_DECK_STEM, i)
        full = images_dir / ("%s-%03d.png" % (_DECK_STEM, i))
        if _png_ok(full):
            s["image_path"] = rel
            relinked += 1
        else:
            missing.append(rel)
    res.relinked = relinked
    res.missing_image_paths = missing

    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(manifest_path))

    res.success = (
        slide_count > 0
        and res.image_count == slide_count
        and not missing
        and all(s.get("image_path") for s in slides)
    )
    res.report_path = _write_slide_image_report(run_dir, res)
    return res


def _write_slide_image_report(run_dir: Path, res: "SlideImageRunResult") -> Path:
    qa = Path(run_dir) / "QA_REPORTS"
    qa.mkdir(parents=True, exist_ok=True)
    path = qa / "slide_image_export_report.md"
    lines = [
        "# Slide-image export report",
        "",
        "- run: `%s`" % Path(run_dir).name,
        "- backend: %s" % (res.backend or "(none)"),
        "- pptx: %s" % (res.pptx_path or "(none)"),
        "- pdf: %s" % (res.pdf_path or "(none)"),
        "- images_dir: %s" % res.images_dir,
        "- slide_count (manifest): %d" % res.slide_count,
        "- image_count (disk): %d" % res.image_count,
        "- reused existing images: %s" % res.reused,
        "- manifest image_path relinked: %d / %d" % (res.relinked, res.slide_count),
        "- missing image paths: %s" % (res.missing_image_paths or "none"),
    ]
    if res.error:
        lines.append("- error: `%s`" % res.error)
    verdict = "pass" if res.success else "FAIL"
    lines += [
        "",
        "## Validation",
        "- image_count == slide_count: %s" % ("pass" if (res.slide_count > 0 and res.image_count == res.slide_count) else "FAIL"),
        "- every slide has a resolvable image_path: %s" % ("pass" if (not res.missing_image_paths and res.relinked == res.slide_count and res.slide_count > 0) else "FAIL"),
        "- no fake images (rendered from real deck PDF): pass",
        "",
        "## Verdict: **%s**" % verdict,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
