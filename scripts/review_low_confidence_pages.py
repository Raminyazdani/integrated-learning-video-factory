"""review_low_confidence_pages — Phase 3A.1 CLI.

Usage:
  python scripts/review_low_confidence_pages.py --run runs/001_1_20260520_2128

Loads the Phase 3A classification, overlays the visual review data baked into
agents/visual_classification_reviewer.py, and writes:
  runs/<run_id>/classification/page_classification_enriched.json
  runs/<run_id>/classification/page_classification_enriched.md
  runs/<run_id>/QA_REPORTS/visual_classification_review_report.md

Also appends "visual_classification_review" to run_state.json. This phase
intentionally does NOT advance current_stage to 3 — package segmentation is
Phase 3B and runs separately.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.visual_classification_reviewer import run_visual_review  # noqa: E402
from integrations.atomic_io import atomic_write_json, iso_now, read_json  # noqa: E402

STAGE_NAME = "visual_classification_review"


def _update_state_files(project_root: Path, run_dir: Path) -> tuple:
    """Append the visual-review stage name to both state files.

    Important: we do NOT advance `current_stage` past 3 here. Phase 3A already
    set current_stage=3 (i.e., next stage to run = 3, package segmentation).
    Phase 3A.1 is a sub-stage of classification; it adds a new
    `completed_stage_names` entry but keeps the integer stage cursor.
    """
    run_state_path = run_dir / "state" / "run_state.json"
    project_state_path = project_root / "state" / "run_state.json"
    now = iso_now()

    for path, snap_dir in (
        (run_state_path, run_dir / "state" / "snapshots"),
        (project_state_path, project_root / "state" / "snapshots"),
    ):
        if not path.exists():
            continue
        s = read_json(path)
        names = s.get("completed_stage_names", [])
        if STAGE_NAME not in names:
            names.append(STAGE_NAME)
        s["completed_stage_names"] = names
        s["updated_at"] = now
        # Status stays as "halted" — Phase 3A.1 is a clean stop too.
        atomic_write_json(path, s, snapshot_dir=snap_dir, snapshot_keep=10)

    return run_state_path, project_state_path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_low_confidence_pages",
        description=(
            "Phase 3A.1 — visual enrichment of low-confidence page classifications. "
            "Operates on an existing run; does not call any external API and does "
            "not touch source projects."
        ),
    )
    parser.add_argument("--run", required=True, help="Path to runs/<run_id>/")
    args = parser.parse_args(argv)

    run_dir = (
        Path(args.run).resolve()
        if Path(args.run).is_absolute()
        else (PROJECT_ROOT / args.run).resolve()
    )
    if not run_dir.exists() or not run_dir.is_dir():
        print(f"[ERROR] run folder not found: {run_dir}", file=sys.stderr)
        return 2

    print(f"[Stage 2.5] Visual classification review on: {run_dir}")
    try:
        result = run_visual_review(run_dir)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"[ERROR] reviewer produced invalid output: {e}", file=sys.stderr)
        return 4
    except Exception:
        print("[ERROR] Unexpected failure:", file=sys.stderr)
        traceback.print_exc()
        return 1

    rs, ps = _update_state_files(PROJECT_ROOT, run_dir)

    print()
    print("=== Phase 3A.1 summary ===")
    print(f"enriched json   : {result.json_path}")
    print(f"enriched md     : {result.md_path}")
    print(f"qa report       : {result.report_path}")
    print(f"run state       : {rs}")
    print(f"project pointer : {ps}")
    print()
    print(f"pages visually reviewed : {result.pages_reviewed}")
    print(f"pages unchanged         : {result.pages_unchanged}")
    print(f"primary-label changes   : {result.label_changes}")
    print(f"confidence upgrades     : {result.confidence_upgrades}")
    print(f"still low-confidence    : {len(result.still_low_confidence)}")
    if result.still_low_confidence:
        for pid in result.still_low_confidence:
            print(f"  - {pid}")
    print()
    print("Stopped cleanly. Phase 3B (package segmentation) not implemented.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
