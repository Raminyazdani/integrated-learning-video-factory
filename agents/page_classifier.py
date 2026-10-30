"""page_classifier_agent — Stage 2 agent (Phase 3A, heuristic-only).

Loads extraction_manifest.json for a given run, classifies every page using
integrations.page_classifier, and emits:

  runs/<run_id>/classification/page_classification.json
  runs/<run_id>/classification/page_classification.md
  runs/<run_id>/QA_REPORTS/page_classification_report.md

In Phase 3A no LLM call is made. The prompt at prompts/page_classifier_prompt.md
remains the contract for future LLM passes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now, read_json
from integrations.page_classifier import (
    ALLOWED_LABELS,
    HeuristicClassification,
    classify_page,
)

PAGE_CLASSIFICATION_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ClassificationResult:
    json_path: Path
    md_path: Path
    report_path: Path
    page_count: int
    label_distribution: dict[str, int]
    low_confidence_pages: list[str]
    pages_with_warnings: list[str]


# ----------------------------------------------------------- pretty-printers


def _build_md_view(
    *,
    run_id: str,
    pages: list[dict],
) -> str:
    out = [
        f"# Page Classification — {run_id}",
        "",
        "> Stage 2 (page classifier). Phase 3A heuristic-only.",
        "> Generated automatically; do not edit by hand.",
        "",
        "## Per-page",
        "",
        "| # | page_id | primary | secondary | conf | formulas | figures | exercise | title_guess |",
        "|---|---------|---------|-----------|------|:--------:|:-------:|:--------:|-------------|",
    ]
    for p in pages:
        sec = ", ".join(p["secondary_labels"]) if p["secondary_labels"] else "—"
        tg = (p.get("title_guess") or "").replace("|", "\\|")
        if len(tg) > 60:
            tg = tg[:57] + "..."
        out.append(
            f"| {p['page_number']} | `{p['page_id']}` | **{p['primary_label']}** | {sec} "
            f"| {p['confidence']} | {'✓' if p['formulas_detected'] else ''} "
            f"| {'✓' if p['figures_or_plots_detected'] else ''} "
            f"| {'✓' if p['exercise_detected'] else ''} | {tg} |"
        )

    out += ["", "## Persian short summaries (per page)", ""]
    for p in pages:
        out.append(f"### page_{p['page_number']:03d} — {p['primary_label']}")
        out.append("")
        out.append(p.get("short_summary_fa") or "—")
        if p.get("key_terms"):
            out.append("")
            out.append("**Key terms:** " + ", ".join(p["key_terms"]))
        if p.get("warnings"):
            out.append("")
            out.append("**Warnings:** " + "; ".join(p["warnings"]))
        out.append("")

    return "\n".join(out)


def _build_qa_report(
    *,
    run_id: str,
    page_count: int,
    pages: list[dict],
    label_dist: dict[str, int],
    low_conf: list[str],
    pages_with_warnings: list[str],
) -> str:
    coverage_ok = all(p.get("primary_label") in ALLOWED_LABELS for p in pages)
    every_has_label = all(p.get("primary_label") for p in pages)
    every_has_conf = all(p.get("confidence") in ("high", "medium", "low") for p in pages)
    every_has_evidence_or_warn = all(
        p.get("evidence_text_excerpt") or p.get("warnings") for p in pages
    )

    out = [
        f"# Page Classification QA Report — {run_id}",
        "",
        "> Stage 2 QA. Generated automatically.",
        "",
        "## Summary",
        "",
        f"- **run_id**: `{run_id}`",
        f"- **pages classified**: {len(pages)} / {page_count}",
        f"- **labels in vocabulary**: {'pass' if coverage_ok else 'FAIL'}",
        f"- **every page has a primary_label**: {'pass' if every_has_label else 'FAIL'}",
        f"- **every page has confidence**: {'pass' if every_has_conf else 'FAIL'}",
        f"- **every page has evidence_excerpt OR a warning**: "
        f"{'pass' if every_has_evidence_or_warn else 'FAIL'}",
        "",
        "## Label distribution (primary_label)",
        "",
        "| label | count |",
        "|-------|------:|",
    ]
    for lbl, cnt in sorted(label_dist.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"| `{lbl}` | {cnt} |")

    out += [
        "",
        "## Low-confidence pages",
        "",
    ]
    if not low_conf:
        out.append("None.")
    else:
        for pid in low_conf:
            out.append(f"- `{pid}`")

    out += [
        "",
        "## Pages with warnings",
        "",
    ]
    if not pages_with_warnings:
        out.append("None.")
    else:
        for pid in pages_with_warnings:
            out.append(f"- `{pid}`")

    out += [
        "",
        "## Acceptance checks (Phase 3A)",
        "",
        f"- page_count match: "
        f"{'pass' if len(pages) == page_count else 'FAIL'} "
        f"({len(pages)}/{page_count})",
        f"- every page appears exactly once: "
        f"{'pass' if len({p['page_id'] for p in pages}) == len(pages) else 'FAIL'}",
        "- labels from closed vocabulary: "
        f"{'pass' if coverage_ok else 'FAIL'}",
        "",
        "## Next stage",
        "",
        "Phase 3B (educational package segmentation) is the natural next step "
        "and should consume `classification/page_classification.json` as its input.",
        "",
    ]
    return "\n".join(out)


# ----------------------------------------------------------------- entry


def run_classification(
    run_dir: Path | str,
    *,
    on_page: Optional[callable] = None,
) -> ClassificationResult:
    """Run Stage 2 classification on an existing run folder.

    Args:
      run_dir: path to runs/<run_id>/
      on_page: optional progress callback (i_1based, total).

    Returns ClassificationResult.

    Raises FileNotFoundError if extraction_manifest.json is missing.
    """
    run_dir = Path(run_dir).resolve()
    manifest_path = run_dir / "extracted" / "extraction_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"extraction_manifest.json not found: {manifest_path}\n"
            "Run Phase 2 first: python scripts/convert_file.py <N> --stop-after extraction"
        )

    manifest = read_json(manifest_path)
    run_id = manifest["run_id"]
    page_count = manifest["page_count"]
    pages = manifest["pages"]

    classification_dir = run_dir / "classification"
    classification_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "QA_REPORTS").mkdir(parents=True, exist_ok=True)

    classified: list[dict] = []
    total = len(pages)
    for i, page in enumerate(pages, start=1):
        if on_page is not None:
            on_page(i, total)
        text_path = run_dir / page["text_path"]
        try:
            text = text_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        result = classify_page(
            page_id=page["page_id"],
            page_number=page["page_number"],
            text=text,
            total_pages=page_count,
            extraction_status=page.get("extraction_status", "ok"),
        )
        classified.append(result.to_dict())

    # JSON output
    out_json = {
        "schema_version": PAGE_CLASSIFICATION_SCHEMA_VERSION,
        "run_id": run_id,
        "source_file": manifest.get("source_file"),
        "source_type": manifest.get("source_type"),
        "page_count": page_count,
        "labels_vocabulary": sorted(ALLOWED_LABELS),
        "classified_at": iso_now(),
        "classifier": {
            "type": "heuristic",
            "version": "phase3a-1.0.0",
            "prompt_reference": "prompts/page_classifier_prompt.md",
            "llm_used": False,
        },
        "pages": classified,
    }
    json_path = classification_dir / "page_classification.json"
    atomic_write_json(json_path, out_json)

    # Markdown view
    md = _build_md_view(run_id=run_id, pages=classified)
    md_path = classification_dir / "page_classification.md"
    atomic_write_text(md_path, md)

    # QA report
    label_dist: dict[str, int] = dict(
        Counter(p["primary_label"] for p in classified)
    )
    low_conf = [p["page_id"] for p in classified if p["confidence"] == "low"]
    pages_with_warnings = [p["page_id"] for p in classified if p["warnings"]]

    qa = _build_qa_report(
        run_id=run_id,
        page_count=page_count,
        pages=classified,
        label_dist=label_dist,
        low_conf=low_conf,
        pages_with_warnings=pages_with_warnings,
    )
    report_path = run_dir / "QA_REPORTS" / "page_classification_report.md"
    atomic_write_text(report_path, qa)

    return ClassificationResult(
        json_path=json_path,
        md_path=md_path,
        report_path=report_path,
        page_count=page_count,
        label_distribution=label_dist,
        low_confidence_pages=low_conf,
        pages_with_warnings=pages_with_warnings,
    )
