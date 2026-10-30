"""extraction_agent - Stage 1.

Phase 2 supported PDF only. Phase 10 adds PPTX. Dispatches by source_type
through ``integrations.extraction_adapter``. Always emits:
  - runs/<run_id>/extracted/extraction_manifest.json   (schema-conformant)
  - runs/<run_id>/QA_REPORTS/extraction_report.md      (human readable)

PPTX runs land here too: text extraction works locally (python-pptx, pure
Python), and slide PNG rendering uses LibreOffice when available. If
LibreOffice is missing, the image_rendering_status in the manifest is
``pending_local`` and the report flags the situation; per-slide statuses
become ``partial`` and the extraction_manifest still validates.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now
from integrations.extraction_adapter import extract as _extract_dispatch

EXTRACTION_MANIFEST_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ExtractionResult:
    manifest_path: Path
    report_path: Path
    page_count: int
    pages_with_text: int
    pages_empty: int
    pages_failed: int
    image_rendering_status: str = "complete"
    image_rendering_error: Optional[str] = None


def _build_report_md(
    *,
    run_id: str,
    source_file: str,
    source_copy: str,
    source_sha256: str,
    source_type: str,
    page_count: int,
    pages: list,
    extracted_at: str,
    image_rendering_status: str,
    image_rendering_error: Optional[str],
    adapter_notes: List[str],
) -> str:
    pages_with_text = sum(1 for p in pages if p["extraction_status"] == "ok")
    pages_empty = sum(1 for p in pages if p["extraction_status"] == "empty_text")
    pages_failed = sum(
        1 for p in pages if p["extraction_status"] in ("render_failed", "partial")
    )

    lines = [
        f"# Extraction Report - {run_id}",
        "",
        f"> Stage 1 ({source_type.upper()}). Generated automatically; do not edit by hand.",
        "",
        "## Summary",
        "",
        f"- **run_id**: `{run_id}`",
        f"- **source_file**: `{source_file}`",
        f"- **source_copy**: `{source_copy}`",
        f"- **source_type**: `{source_type}`",
        f"- **source_sha256**: `{source_sha256}`",
        f"- **page_count**: {page_count}",
        f"- **extracted_at**: {extracted_at}",
        f"- **pages with text**: {pages_with_text}",
        f"- **pages empty (image-only)**: {pages_empty}",
        f"- **pages partial / failed**: {pages_failed}",
        f"- **image_rendering_status**: {image_rendering_status}",
    ]
    if image_rendering_error:
        lines.append(f"- **image_rendering_error**: `{image_rendering_error}`")
    lines.append("")
    if adapter_notes:
        lines.append("## Notes from extractor")
        lines.append("")
        for n in adapter_notes:
            lines.append(f"- {n}")
        lines.append("")
    lines.extend([
        "## Per-page",
        "",
        "| # | page_id | status | chars | image | warnings |",
        "|---|---------|--------|------:|-------|----------|",
    ])
    for p in pages:
        warns = "; ".join(p.get("warnings", [])) or ""
        lines.append(
            f"| {p['page_number']} | `{p['page_id']}` | {p['extraction_status']} "
            f"| {p['text_char_count']} | `{p['image_path']}` | {warns} |"
        )
    lines.extend([
        "",
        "## QA checks",
        "",
        f"- PNG count == page_count: "
        f"{'pass' if image_rendering_status == 'complete' else 'PENDING_LOCAL_CLAUDE_CODE_VALIDATION' if image_rendering_status == 'pending_local' else 'fail'}",
        f"- TXT count == page_count: pass (one TXT per page, even if empty)",
        f"- Every page has both image_path and text_path: "
        f"{'pass' if image_rendering_status == 'complete' else 'PENDING for images'}",
        "",
        "## Next stage",
        "",
        "Stage 2 (page classifier) consumes this manifest. If "
        "image_rendering_status is `pending_local`, downstream stages will "
        "remain PENDING until the run is repeated on a host with LibreOffice.",
        "",
    ])
    return "\n".join(lines)


def run_extraction(
    *,
    run_dir: Path,
    run_id: str,
    source_path: Path,
    source_copy: Path,
    source_sha256: str,
    source_type: str = "pdf",
    dpi: int = 200,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> ExtractionResult:
    """Run Stage 1 extraction (PDF or PPTX) and emit manifest + report."""
    st = (source_type or "").lower()
    if st not in ("pdf", "pptx"):
        raise ValueError(f"unsupported source_type: {source_type!r}")

    pages_dir = run_dir / "extracted" / "pages"
    text_dir = run_dir / "extracted" / "page_text"
    pages_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    disp = _extract_dispatch(
        source_copy,
        source_type=st,
        pages_dir=pages_dir,
        text_dir=text_dir,
        dpi=dpi,
        on_page=on_page,
    )
    pages = disp.pages

    # Convert absolute paths to run-relative paths in the manifest
    def _rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(run_dir.resolve()).as_posix())
        except ValueError:
            return str(Path(p).as_posix())

    for entry in pages:
        entry["image_path"] = _rel(entry["image_path"])
        entry["text_path"] = _rel(entry["text_path"])

    extracted_at = iso_now()

    issues: List[str] = []
    if disp.image_rendering_status == "pending_local":
        issues.append("image_rendering=PENDING_LOCAL_CLAUDE_CODE_VALIDATION")
        if disp.image_rendering_error:
            issues.append(f"image_rendering_error={disp.image_rendering_error}")
    elif disp.image_rendering_status == "failed":
        issues.append("image_rendering=FAILED")
        if disp.image_rendering_error:
            issues.append(f"image_rendering_error={disp.image_rendering_error}")

    manifest = {
        "schema_version": EXTRACTION_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "source_file": str(source_path.as_posix()),
        "source_copy": str(source_copy.relative_to(run_dir).as_posix()),
        "source_type": st,
        "file_type": st,
        "source_sha256": source_sha256,
        "page_count": len(pages),
        "pages": pages,
        "issues": issues,
        "image_rendering_status": disp.image_rendering_status,
        "image_rendering_error": disp.image_rendering_error,
        "image_rendering_strategy": disp.image_rendering_strategy,
        "extracted_at": extracted_at,
        "extraction_timestamp": extracted_at,
    }

    manifest_path = run_dir / "extracted" / "extraction_manifest.json"
    atomic_write_json(manifest_path, manifest)

    report_md = _build_report_md(
        run_id=run_id,
        source_file=manifest["source_file"],
        source_copy=manifest["source_copy"],
        source_sha256=source_sha256,
        source_type=st,
        page_count=len(pages),
        pages=pages,
        extracted_at=extracted_at,
        image_rendering_status=disp.image_rendering_status,
        image_rendering_error=disp.image_rendering_error,
        adapter_notes=disp.notes,
    )
    report_path = run_dir / "QA_REPORTS" / "extraction_report.md"
    atomic_write_text(report_path, report_md)

    return ExtractionResult(
        manifest_path=manifest_path,
        report_path=report_path,
        page_count=len(pages),
        pages_with_text=sum(1 for p in pages if p["extraction_status"] == "ok"),
        pages_empty=sum(1 for p in pages if p["extraction_status"] == "empty_text"),
        pages_failed=sum(
            1 for p in pages if p["extraction_status"] in ("render_failed", "partial")
        ),
        image_rendering_status=disp.image_rendering_status,
        image_rendering_error=disp.image_rendering_error,
    )
