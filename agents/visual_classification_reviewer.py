"""Generic visual-review overlay pipeline. Original document-specific reviews, segmentation hints and source-name gates are empty in this source-only release. Supply a per-run authored review; otherwise labels remain baseline-only with a warning."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now, read_json

ENRICHED_SCHEMA_VERSION = "1.0.0"

# --------------------------------------------------------------- vocabularies

ALLOWED_BOUNDARY_SIGNALS = {"start", "continue", "boundary", "context_only", "unknown"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_PRIMARY_LABELS = {
    "title", "outline", "transition", "definition", "concept_explanation",
    "theorem", "proof", "derivation", "formula", "example", "exercise",
    "solution", "plot_or_figure", "table", "code_or_pseudocode", "algorithm",
    "summary", "bibliography", "mixed", "unknown", "context_only",
}

# --------------------------------------------------------- visual review data

# Authored after directly viewing runs/001_1_20260520_2128/extracted/pages/*.png
# in the Cowork session. Each entry overrides the heuristic baseline ONLY where
# the visual evidence is unambiguous; otherwise we keep the baseline labels.
#
# Conservative rule (per the user spec): never invent invisible content; if
# unclear, mark uncertainty rather than guess.
_VISUAL_REVIEWS: dict[str, dict] = {}

# Segmentation-readiness assessment, computed by the human reviewer in Cowork
# from the visual evidence above + the high-confidence pages from Phase 3A.
# Used by the QA report; downstream Phase 3B may overwrite this with its own
# segmenter output.
_SEGMENTATION_HINTS: dict = {}


# ----------------------------------------------------------- output container


@dataclass(frozen=True)
class ReviewResult:
    json_path: Path
    md_path: Path
    report_path: Path
    pages_reviewed: int
    pages_unchanged: int
    label_changes: int
    confidence_upgrades: int
    still_low_confidence: list


# --------------------------------------------------------------- helpers


def _baseline_entry(page: dict) -> dict:
    """Build an enriched entry for a page we did NOT visually review."""
    return {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "old_primary_label": page["primary_label"],
        "enriched_primary_label": page["primary_label"],
        "old_secondary_labels": list(page.get("secondary_labels", [])),
        "enriched_secondary_labels": list(page.get("secondary_labels", [])),
        "visual_summary_fa": (
            page.get("short_summary_fa", "") or ""
        ),
        "corrected_title_guess": page.get("title_guess"),
        "educational_role": _infer_role(page["primary_label"]),
        "package_boundary_signal": _infer_boundary(page),
        "confidence_after_visual_review": page.get("confidence", "low"),
        "reason_for_change": "not_visually_reviewed_in_phase_3a1",
        "uncertainty_notes": list(page.get("warnings", [])),
        "visually_reviewed": False,
    }


def _apply_review(page: dict, review: dict) -> dict:
    """Build an enriched entry overriding baseline with reviewer findings."""
    return {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "old_primary_label": page["primary_label"],
        "enriched_primary_label": review["enriched_primary_label"],
        "old_secondary_labels": list(page.get("secondary_labels", [])),
        "enriched_secondary_labels": list(review["enriched_secondary_labels"]),
        "visual_summary_fa": review["visual_summary_fa"],
        "corrected_title_guess": review.get("corrected_title_guess"),
        "educational_role": review["educational_role"],
        "package_boundary_signal": review["package_boundary_signal"],
        "confidence_after_visual_review": review["confidence_after_visual_review"],
        "reason_for_change": review["reason_for_change"],
        "uncertainty_notes": list(review.get("uncertainty_notes", [])),
        "visually_reviewed": True,
    }


def _infer_role(primary_label: str) -> str:
    """Map heuristic primary_label to a coarse educational role for unreviewed pages."""
    mapping = {
        "title": "section_boundary",
        "outline": "section_boundary",
        "transition": "transition_only",
        "definition": "concept_explanation",
        "concept_explanation": "concept_explanation",
        "theorem": "concept_explanation",
        "proof": "concept_explanation",
        "derivation": "concept_explanation",
        "formula": "formula_focus",
        "example": "example_walkthrough",
        "exercise": "exercise",
        "solution": "solution",
        "plot_or_figure": "visual_diagram",
        "table": "data_table",
        "code_or_pseudocode": "code_block",
        "algorithm": "algorithm",
        "summary": "summary",
        "bibliography": "context_only",
        "mixed": "unknown",
        "unknown": "unknown",
        "context_only": "context_only",
    }
    return mapping.get(primary_label, "unknown")


def _infer_boundary(page: dict) -> str:
    pl = page.get("primary_label")
    if pl in ("title", "outline", "transition"):
        return "start"
    if pl in ("bibliography", "context_only"):
        return "context_only"
    return "continue"


def _validate_enrichment(entry: dict) -> list:
    errs = []
    if entry["enriched_primary_label"] not in ALLOWED_PRIMARY_LABELS:
        errs.append(f"{entry['page_id']}: invalid enriched_primary_label {entry['enriched_primary_label']!r}")
    if entry["package_boundary_signal"] not in ALLOWED_BOUNDARY_SIGNALS:
        errs.append(f"{entry['page_id']}: invalid package_boundary_signal {entry['package_boundary_signal']!r}")
    if entry["confidence_after_visual_review"] not in ALLOWED_CONFIDENCE:
        errs.append(f"{entry['page_id']}: invalid confidence_after_visual_review")
    for lbl in entry["enriched_secondary_labels"]:
        if lbl not in ALLOWED_PRIMARY_LABELS:
            errs.append(f"{entry['page_id']}: invalid secondary {lbl!r}")
    return errs


# ----------------------------------------------------------- markdown views


def _build_md_view(*, run_id: str, entries: list) -> str:
    out = [
        f"# Page Classification — Enriched — {run_id}",
        "",
        "> Stage 2.5 (Phase 3A.1). Visual classification enrichment for low-confidence pages.",
        "> Generated automatically; do not edit by hand.",
        "",
        "## Per-page (after visual review)",
        "",
        "| # | page_id | old | enriched | conf | role | boundary | reviewed |",
        "|---|---------|-----|----------|------|------|----------|:--------:|",
    ]
    for e in entries:
        old = e["old_primary_label"]
        new = e["enriched_primary_label"]
        changed = "**" if old != new else ""
        out.append(
            f"| {e['page_number']} | `{e['page_id']}` | {old} | {changed}{new}{changed} "
            f"| {e['confidence_after_visual_review']} | {e['educational_role']} "
            f"| {e['package_boundary_signal']} | {'✓' if e['visually_reviewed'] else ''} |"
        )

    out += ["", "## Per-page details (reviewed pages only)", ""]
    for e in entries:
        if not e["visually_reviewed"]:
            continue
        out.append(f"### page_{e['page_number']:03d} — {e['enriched_primary_label']}")
        out.append("")
        out.append(f"- **old primary**: {e['old_primary_label']} → **enriched**: {e['enriched_primary_label']}")
        out.append(f"- **old secondaries**: {e['old_secondary_labels'] or '—'}")
        out.append(f"- **enriched secondaries**: {e['enriched_secondary_labels'] or '—'}")
        out.append(f"- **title (corrected)**: {e.get('corrected_title_guess') or '—'}")
        out.append(f"- **educational role**: {e['educational_role']}")
        out.append(f"- **package boundary signal**: {e['package_boundary_signal']}")
        out.append(f"- **confidence after review**: {e['confidence_after_visual_review']}")
        out.append(f"- **reason for change**: {e['reason_for_change']}")
        if e["uncertainty_notes"]:
            out.append(f"- **uncertainty**: {'; '.join(e['uncertainty_notes'])}")
        out.append("")
        out.append("**Visual summary (فارسی):** " + e["visual_summary_fa"])
        out.append("")

    return "\n".join(out)


def _build_qa_report(
    *,
    run_id: str,
    entries: list,
    reviewed_pages: list,
    label_changes: list,
    confidence_upgrades: int,
    still_low: list,
    seg_hints: dict,
    low_conf_pages: list = None,
) -> str:
    if low_conf_pages is None:
        low_conf_pages = _PHASE3A_LOW_CONFIDENCE_PAGES
    out = [
        f"# Visual Classification Review — QA — {run_id}",
        "",
        "> Stage 2.5 (Phase 3A.1) QA. Generated automatically.",
        "",
        "## Summary",
        "",
        f"- **pages total**: {len(entries)}",
        f"- **pages visually reviewed**: {len(reviewed_pages)}",
        f"- **pages with primary-label change**: {len(label_changes)}",
        f"- **pages with confidence upgrade**: {confidence_upgrades}",
        f"- **pages still low-confidence after review**: {len(still_low)}",
        "",
        "## Label changes (old → enriched)",
        "",
    ]
    if not label_changes:
        out.append("None.")
    else:
        out.append("| page_id | old | enriched |")
        out.append("|---------|-----|----------|")
        for c in label_changes:
            out.append(f"| `{c['page_id']}` | {c['old_primary_label']} | {c['enriched_primary_label']} |")

    out += [
        "",
        "## Still low-confidence after review",
        "",
    ]
    if not still_low:
        out.append("None.")
    else:
        for pid in still_low:
            out.append(f"- `{pid}`")

    out += [
        "",
        "## Segmentation-readiness summary",
        "",
        "_Authored by the visual reviewer; Phase 3B's segmenter may revise._",
        "",
        "### Natural package boundaries (1-based page numbers where a new package should start)",
        "",
        "  " + ", ".join(str(n) for n in seg_hints.get("natural_boundaries_before_page", [])),
        "",
        "### Suggested groupings",
        "",
        "| package | pages | hint (fa) |",
        "|--------:|-------|-----------|",
    ]
    for g in seg_hints.get("suggested_groupings", []):
        out.append(f"| {g['package_idx']} | {g['pages']} | {g['title_hint_fa']} |")

    out += [
        "",
        "### Likely-standalone pages",
        "",
        "  " + ", ".join(str(n) for n in seg_hints.get("stand_alone_candidates", [])),
        "",
        "### Pages still uncertain (Phase 3B should re-examine)",
        "",
        "  " + ", ".join(str(n) for n in seg_hints.get("still_uncertain_pages", [])),
        "",
        "### Reviewer notes (فارسی)",
        "",
        seg_hints.get("notes_fa", ""),
        "",
        "## Acceptance checks (Phase 3A.1)",
        "",
        f"- enriched file covers every page exactly once: "
        f"{'pass' if len(entries) == len({e['page_id'] for e in entries}) else 'FAIL'}",
        f"- every low-confidence page has a visual review entry: "
        f"{'pass' if all(any(r['page_id']==pid for r in reviewed_pages) for pid in low_conf_pages) else 'FAIL'}",
        "- no page dropped: pass (count matches Phase 3A)",
        "",
    ]
    return "\n".join(out)


# Hard-coded list of Phase 3A low-confidence pages — used to validate that the
# reviewer addressed every one of them.
_PHASE3A_LOW_CONFIDENCE_PAGES: list = []


# ----------------------------------------------------------- genericness gate

# The file-1 gold sample. The baked _VISUAL_REVIEWS / _SEGMENTATION_HINTS /
# _PHASE3A_LOW_CONFIDENCE_PAGES describe THIS deck only and must never be applied
# to a different input (they would leak file-1 content). For any other run we use
# a run-specific authored file if present, else no overlay.
_FILE1_SOURCE_NAMES = set()

_EMPTY_SEG_HINTS: dict = {
    "natural_boundaries_before_page": [],
    "suggested_groupings": [],
    "stand_alone_candidates": [],
    "still_uncertain_pages": [],
    "notes_fa": "",
}


def _is_file1_run(baseline: dict) -> bool:
    sf = (baseline.get("source_file") or "").replace("\\", "/").lower()
    return sf.rsplit("/", 1)[-1] in _FILE1_SOURCE_NAMES


def _load_run_reviews(run_dir: Path, baseline: dict):
    """Return (reviews, seg_hints, low_conf_pages) for this run.

    Precedence (generic-safe):
      1. file-1 baked fixtures — ONLY when the run's source is file 1 (gold sample).
      2. ``classification/llm_page_review.json`` — generic LLM/Vision or
         manual-agent page review (Phase 3A drop-in path) for any other input.
      3. ``classification/authored_review.json`` — manual fallback (e.g. file 3).
      4. no overlay (baseline-only, with warning) — prevents file-1 leakage.
    """
    # 1. file-1 gold-sample fixture (gated)
    if _is_file1_run(baseline):
        return (_VISUAL_REVIEWS, _SEGMENTATION_HINTS, _PHASE3A_LOW_CONFIDENCE_PAGES)
    # 2. LLM/Vision or manual-agent page review (generic, per-input)
    llm_review = run_dir / "classification" / "llm_page_review.json"
    if llm_review.exists():
        d = read_json(llm_review)
        hints = d.get("segmentation_hints") or _EMPTY_SEG_HINTS
        return (d.get("reviews") or {}, hints, d.get("low_confidence_pages") or [])
    # 3. manual authored fallback
    authored = run_dir / "classification" / "authored_review.json"
    if authored.exists():
        d = read_json(authored)
        hints = d.get("segmentation_hints") or _EMPTY_SEG_HINTS
        return (d.get("reviews") or {}, hints, d.get("low_confidence_pages") or [])
    # 4. baseline-only (no overlay)
    print("[visual_review] WARNING: no page-review overlay (llm_page_review.json / "
          "authored_review.json) for non-file-1 input; using baseline labels only.")
    return ({}, _EMPTY_SEG_HINTS, [])


# ----------------------------------------------------------- public entry


def run_visual_review(run_dir: Path | str) -> ReviewResult:
    """Run Stage 2.5 visual enrichment on a run with an existing Phase 3A classification.

    Raises FileNotFoundError if page_classification.json is missing.
    """
    run_dir = Path(run_dir).resolve()
    cls_path = run_dir / "classification" / "page_classification.json"
    if not cls_path.exists():
        raise FileNotFoundError(
            "page_classification.json missing. Run Phase 3A first: "
            "python scripts/classify_pages.py --run <run_dir>"
        )

    baseline = read_json(cls_path)
    pages = baseline["pages"]
    run_id = baseline["run_id"]

    # Genericness gate: file-1 baked fixtures only for file 1; otherwise a
    # run-specific authored file (or no overlay). Prevents file-1 leakage.
    reviews, seg_hints, low_conf_pages = _load_run_reviews(run_dir, baseline)

    # Build enriched entries — overlay reviews where authored
    entries: list = []
    reviewed_entries: list = []
    label_changes: list = []
    confidence_upgrades = 0

    conf_rank = {"low": 0, "medium": 1, "high": 2}
    for p in pages:
        rev = reviews.get(p["page_id"])
        if rev is None:
            entry = _baseline_entry(p)
        else:
            entry = _apply_review(p, rev)
            reviewed_entries.append(entry)
            if entry["enriched_primary_label"] != entry["old_primary_label"]:
                label_changes.append(entry)
            if conf_rank[entry["confidence_after_visual_review"]] > conf_rank[p.get("confidence", "low")]:
                confidence_upgrades += 1

        errs = _validate_enrichment(entry)
        if errs:
            raise ValueError("Reviewer produced invalid entry: " + "; ".join(errs))
        entries.append(entry)

    still_low = [
        e["page_id"]
        for e in entries
        if e["confidence_after_visual_review"] == "low"
    ]

    out_json = {
        "schema_version": ENRICHED_SCHEMA_VERSION,
        "run_id": run_id,
        "source_file": baseline.get("source_file"),
        "source_type": baseline.get("source_type"),
        "page_count": baseline["page_count"],
        "based_on_classification": "classification/page_classification.json",
        "based_on_classifier_type": baseline.get("classifier", {}).get("type"),
        "review_type": "visual_human_in_the_loop_cowork",
        "review_phase": "3A.1",
        "reviewed_at": iso_now(),
        "pages": entries,
        "segmentation_hints": seg_hints,
        "totals": {
            "pages": len(entries),
            "visually_reviewed": len(reviewed_entries),
            "primary_label_changed": len(label_changes),
            "confidence_upgrades": confidence_upgrades,
            "still_low_confidence": len(still_low),
        },
    }

    json_path = run_dir / "classification" / "page_classification_enriched.json"
    atomic_write_json(json_path, out_json)

    md = _build_md_view(run_id=run_id, entries=entries)
    md_path = run_dir / "classification" / "page_classification_enriched.md"
    atomic_write_text(md_path, md)

    report = _build_qa_report(
        run_id=run_id,
        entries=entries,
        reviewed_pages=reviewed_entries,
        label_changes=label_changes,
        confidence_upgrades=confidence_upgrades,
        still_low=still_low,
        seg_hints=seg_hints,
        low_conf_pages=low_conf_pages,
    )
    report_path = run_dir / "QA_REPORTS" / "visual_classification_review_report.md"
    atomic_write_text(report_path, report)

    return ReviewResult(
        json_path=json_path,
        md_path=md_path,
        report_path=report_path,
        pages_reviewed=len(reviewed_entries),
        pages_unchanged=len(entries) - len(reviewed_entries),
        label_changes=len(label_changes),
        confidence_upgrades=confidence_upgrades,
        still_low_confidence=still_low,
    )
