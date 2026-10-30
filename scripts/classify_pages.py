"""classify_pages — Phase 3A CLI entry.

Usage:
  python scripts/classify_pages.py --run runs/001_1_20260520_2128

Operates on a previously-extracted run; classifies every page and writes:
  runs/<run_id>/classification/page_classification.json
  runs/<run_id>/classification/page_classification.md
  runs/<run_id>/QA_REPORTS/page_classification_report.md

Also updates the run-local and project-level run_state.json atomically:
  - completed_stages gets index 2 appended
  - completed_stage_names gets "page_classification" appended
  - current_stage advances to 3
  - status set to "halted" (Phase 3A is a clean halt; no Stage 3+ work)
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.page_classifier import run_classification  # noqa: E402
from integrations.atomic_io import atomic_write_json, iso_now, read_json  # noqa: E402

STAGE_INDEX = 2
STAGE_NAME = "page_classification"


def _update_state_files(project_root: Path, run_dir: Path) -> tuple[Path, Path]:
    """Append Stage 2 completion to run-local and project-level run_state."""
    run_state_path = run_dir / "state" / "run_state.json"
    project_state_path = project_root / "state" / "run_state.json"

    now = iso_now()

    # Run-local state
    if run_state_path.exists():
        rs = read_json(run_state_path)
        completed = rs.get("completed_stages", [])
        names = rs.get("completed_stage_names", [])
        if STAGE_INDEX not in completed:
            completed.append(STAGE_INDEX)
            names.append(STAGE_NAME)
        rs["completed_stages"] = completed
        rs["completed_stage_names"] = names
        rs["current_stage"] = max(rs.get("current_stage", 0), STAGE_INDEX + 1)
        rs["updated_at"] = now
        rs["status"] = "halted"  # Phase 3A: stop here cleanly
        atomic_write_json(
            run_state_path,
            rs,
            snapshot_dir=run_dir / "state" / "snapshots",
            snapshot_keep=10,
        )

    # Project pointer
    if project_state_path.exists():
        ps = read_json(project_state_path)
        completed = ps.get("completed_stages", [])
        names = ps.get("completed_stage_names", [])
        if STAGE_INDEX not in completed:
            completed.append(STAGE_INDEX)
            names.append(STAGE_NAME)
        ps["completed_stages"] = completed
        ps["completed_stage_names"] = names
        ps["current_stage"] = max(ps.get("current_stage", 0), STAGE_INDEX + 1)
        ps["updated_at"] = now
        ps["status"] = "halted"
        atomic_write_json(
            project_state_path,
            ps,
            snapshot_dir=project_root / "state" / "snapshots",
            snapshot_keep=10,
        )

    return run_state_path, project_state_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="classify_pages",
        description=(
            "Phase 3A: heuristic classification of every extracted page in a run. "
            "Does NOT call any LLM and does NOT touch source projects."
        ),
    )
    parser.add_argument(
        "--run",
        required=True,
        help="Path to the run folder, e.g. runs/001_1_20260520_2128",
    )
    args = parser.parse_args(argv)

    run_dir = (PROJECT_ROOT / args.run).resolve() if not Path(args.run).is_absolute() else Path(args.run).resolve()
    # If the user passed an absolute path under the project, fine; if relative
    # like "runs/001_..." it resolves under project root above.
    if not run_dir.exists() or not run_dir.is_dir():
        print(f"[ERROR] run folder not found: {run_dir}", file=sys.stderr)
        return 2

    print(f"[Stage 2] Classifying pages in: {run_dir}")

    def _progress(i: int, total: int) -> None:
        if i == 1 or i == total or i % 5 == 0:
            print(f"  page {i}/{total}", file=sys.stderr)

    try:
        result = run_classification(run_dir, on_page=_progress)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 3
    except Exception:
        print("[ERROR] Unexpected failure:", file=sys.stderr)
        traceback.print_exc()
        return 1

    # Update state atomically (best-effort: state files were created in Phase 2)
    rs_path, ps_path = _update_state_files(PROJECT_ROOT, run_dir)

    print()
    print("=== Phase 3A summary ===")
    print(f"pages classified : {result.page_count}")
    print(f"json             : {result.json_path}")
    print(f"markdown view    : {result.md_path}")
    print(f"qa report        : {result.report_path}")
    print(f"run state        : {rs_path}")
    print(f"project pointer  : {ps_path}")
    print()
    print("Label distribution (primary):")
    for lbl, cnt in sorted(result.label_distribution.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {lbl:24s} {cnt}")
    print()
    if result.low_confidence_pages:
        print(f"Low-confidence pages ({len(result.low_confidence_pages)}):")
        for pid in result.low_confidence_pages:
            print(f"  {pid}")
    else:
        print("No low-confidence pages.")
    print()
    print("Stopped cleanly. Phase 3B (package segmentation) not implemented.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
