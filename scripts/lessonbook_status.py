"""lessonbook_status — read-only stage checklist for a lessonbook run.

Reports which workflow stages have produced their artifact and the next recommended
action. Decides nothing; authors nothing; renders nothing; touches no paid/TTS/video.
Generic for ANY run (no hardcoded test.pdf / package / page assumptions).

Stages + owners: docs/FUTURE_PDF_LESSONBOOK_WORKFLOW_CONTRACT.md and
configs/pipeline_modes.yaml (lessonbook_workflow).

Usage:
  python scripts/lessonbook_status.py --run runs/<id>
Exit: always 0 (informational).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_book_dir(run_dir: Path) -> Path | None:
    """Locate the lessonbook book folder for a run (generic; prefers `lessonbook/`)."""
    cands = [run_dir / "lessonbook"] + sorted(run_dir.glob("lessonbook_full*")) + sorted(run_dir.glob("lessonbook*"))
    for c in cands:
        if c.is_dir() and (
            any(c.glob("lessonbook_outline.json")) or any(c.glob("*_narrative.json"))
            or any(c.glob("*coverage_matrix.json")) or any(c.glob("*_visual_asset_plan.json"))
        ):
            return c
    return (run_dir / "lessonbook") if (run_dir / "lessonbook").is_dir() else None


def _any(d: Path, *pats: str) -> bool:
    return d.is_dir() and any(any(d.glob(p)) for p in pats)


def status(run_dir: Path) -> int:
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        print(f"[status] run dir not found: {run_dir}")
        return 0
    book = find_book_dir(run_dir)
    ex = run_dir / "extracted"
    cls = run_dir / "classification"
    pkg = run_dir / "packages"
    b = book or (run_dir / "lessonbook")

    # (stage label, owner, present?)
    rows = [
        ("1  extraction",            "python", _any(ex / "pages", "*.png")),
        ("2  page understanding",    "CLAUDE", _any(b, "claude_page_review.json") or (cls / "claude_page_review.json").is_file()),
        ("3  source coverage matrix", "CLAUDE", _any(b, "*source_slide_coverage_matrix.json")),
        ("4  package plan",          "CLAUDE", _any(b, "claude_package_plan.json") or (pkg / "claude_package_plan.json").is_file()),
        ("5  visual-asset plan",     "CLAUDE", _any(b, "*_visual_asset_plan.json")),
        ("6  visual-asset execution", "python", _any(b, "*_visual_asset_manifest.json") and _any(b / "visual_assets", "*.png")),
        ("7  chapter narratives",    "CLAUDE", _any(b, "chapter_*_narrative.json")),
        ("8  outline",               "CLAUDE", _any(b, "lessonbook_outline.json")),
        ("9  XeLaTeX render",        "python", _any(b, "final_lessonbook*.pdf")),
        ("10 PDF manifest / QA",     "python", _any(b, "lessonbook_pdf_manifest*.json")),
    ]

    print(f"=== lessonbook status: {run_dir.name} ===")
    print(f"book dir: {book if book else '(none yet — run --init then author stage 2)'}")
    next_action = None
    for label, owner, ok in rows:
        print(f"  [{'x' if ok else ' '}] {label:<28} ({owner})")
        if next_action is None and not ok:
            next_action = (label, owner)

    if next_action is None:
        print("next: all pipeline artifacts present -> run lessonbook_validate.py, then STOP for USER review (no auto-promote).")
    else:
        label, owner = next_action
        if owner == "CLAUDE":
            print(f"next: [CLAUDE authoring gate] author '{label.split('  ',1)[-1].strip()}' to its schema (manual-agent). Python must NOT fabricate it.")
        else:
            print(f"next: [python] run the '{label.split('  ',1)[-1].strip()}' stage via run_lessonbook_pipeline.py.")
    # --- strict source-exhaustive stage status (the full strict workflow order) ---
    inv = _any(b, "source_page_inventory.json", "*source_page_inventory.json") or (run_dir / "source_page_inventory.json").is_file()
    cov2 = _any(b, "*coverage_matrix_v2.json")
    depth = _any(b, "*depth_review.json")
    mode = "strict-v2" if (inv or cov2 or depth) else "legacy"
    strict_rows = [
        ("S01 extraction",                     _any(ex / "pages", "*.png")),
        ("S02 source page inventory",          inv),
        ("S03 page understanding",             _any(b, "claude_page_review.json") or (cls / "claude_page_review.json").is_file()),
        ("S04 criticality + coverage matrix v2", cov2),
        ("S05 package plan",                   _any(b, "claude_package_plan.json") or (pkg / "claude_package_plan.json").is_file()),
        ("S06 visual-asset plan",              _any(b, "*_visual_asset_plan.json")),
        ("S07 visual assets executed",         _any(b, "*_visual_asset_manifest.json") and _any(b / "visual_assets", "*.png")),
        ("S08 chapter plans",                  _any(b, "chapter_*_plan.json")),
        ("S09 chapter narratives",             _any(b, "chapter_*_narrative.json")),
        ("S10 depth review",                   depth),
        ("S11 coverage audit artifact",        _any(b, "*coverage_audit.json")),
        ("S12 XeLaTeX render",                 _any(b, "final_lessonbook*.pdf")),
    ]
    print(f"--- strict source-exhaustive stages (mode: {mode}) ---")
    next_strict = None
    for label, ok in strict_rows:
        print(f"  [{'x' if ok else ' '}] {label}")
        if next_strict is None and not ok:
            next_strict = label
    print("  [-] S13 strict QA            (run: lessonbook_validate.py --strict; gives coverage/depth pass/fail)")
    print("  [-] S14 user review          (manual gate -- STOP before promotion)")
    print("  [-] S15 promotion            (manual -- explicit user approval: copy to output/ + register sha)")
    if mode == "legacy":
        print("  legacy run: v2 artifacts absent -> validated in legacy mode (e.g. 004/005). Author the v2 gates "
              "(inventory -> criticality/coverage_matrix_v2 -> depth review) for strict enforcement; render needs --allow-legacy.")
    elif next_strict:
        gate = next_strict.split(" ", 1)[-1].strip()
        print(f"  next strict gate: author '{gate}' (CLAUDE; Python must NOT fabricate it), then re-check status.")
    else:
        print("  all strict artifacts present -> run lessonbook_validate.py --strict, then STOP for user review (no auto-promote).")

    print("note: video/TTS/paid OFF by default; promotion needs explicit user approval.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only lessonbook pipeline status for a run.")
    ap.add_argument("--run", required=True, help="run dir, e.g. runs/<id>")
    return status(Path(ap.parse_args().run))


if __name__ == "__main__":
    sys.exit(main())
