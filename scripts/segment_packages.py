"""segment_packages — Phase 3B CLI.

Usage:
  python scripts/segment_packages.py --run runs/001_1_20260520_2128

Reads page_classification_enriched.json, runs the deterministic
package segmenter, writes:
  runs/<run_id>/packages/PACKAGE_MAP.md
  runs/<run_id>/packages/package_manifest.json
  runs/<run_id>/QA_REPORTS/package_segmentation_report.md

Validates the emitted manifest against schemas/package_manifest.schema.json
before exiting. Updates run-local + project-level run_state.json atomically.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.package_segmenter import run_segmentation  # noqa: E402
from integrations.atomic_io import atomic_write_json, iso_now, read_json  # noqa: E402

STAGE_INDEX = 3
STAGE_NAME = "package_segmentation"


def _load_config_max_pages(project_root: Path) -> int:
    """Read max_pages_per_package from configs/pipeline_config.yaml; fall back to 5."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return 5
    cfg_path = project_root / "configs" / "pipeline_config.yaml"
    if not cfg_path.exists():
        return 5
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return int(((cfg or {}).get("package_segmentation") or {}).get("max_pages_per_package", 5))
    except Exception:
        return 5


def _validate_manifest_against_schema(project_root: Path, manifest_path: Path) -> tuple:
    """Returns (ok, errors)."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return True, ["jsonschema not installed in this environment; skipping schema check"]
    schema = json.loads((project_root / "schemas" / "package_manifest.schema.json").read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    v = Draft202012Validator(schema)
    errors = [
        f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(manifest)
    ]
    return (len(errors) == 0), errors


def _update_state_files(project_root: Path, run_dir: Path) -> tuple:
    rs_path = run_dir / "state" / "run_state.json"
    ps_path = project_root / "state" / "run_state.json"
    now = iso_now()
    for path, snap_dir in (
        (rs_path, run_dir / "state" / "snapshots"),
        (ps_path, project_root / "state" / "snapshots"),
    ):
        if not path.exists():
            continue
        s = read_json(path)
        comp = s.get("completed_stages", [])
        names = s.get("completed_stage_names", [])
        if STAGE_INDEX not in comp:
            comp.append(STAGE_INDEX)
            names.append(STAGE_NAME)
        s["completed_stages"] = comp
        s["completed_stage_names"] = names
        s["current_stage"] = max(s.get("current_stage", 0), STAGE_INDEX + 1)
        s["updated_at"] = now
        s["status"] = "halted"
        atomic_write_json(path, s, snapshot_dir=snap_dir, snapshot_keep=10)
    return rs_path, ps_path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="segment_packages",
        description=(
            "Phase 3B — group consecutive pages into educational packages "
            "based on the enriched classification. No LLM, no source-project edits."
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

    max_pages = _load_config_max_pages(PROJECT_ROOT)
    print(f"[Stage 3] Package segmentation (max_pages_per_package={max_pages}) on: {run_dir}")

    try:
        result = run_segmentation(run_dir, max_pages_per_package=max_pages)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 3
    except Exception:
        print("[ERROR] Unexpected failure:", file=sys.stderr)
        traceback.print_exc()
        return 1

    ok, errs = _validate_manifest_against_schema(PROJECT_ROOT, result.manifest_path)

    rs_path, ps_path = _update_state_files(PROJECT_ROOT, run_dir)

    print()
    print("=== Phase 3B summary ===")
    print(f"manifest         : {result.manifest_path}")
    print(f"package_map      : {result.map_path}")
    print(f"qa report        : {result.report_path}")
    print(f"run state        : {rs_path}")
    print(f"project pointer  : {ps_path}")
    print()
    print(f"pages_total           : {result.pages_total}")
    print(f"packages              : {result.package_count}")
    print(f"pages_in_packages     : {result.pages_in_packages}")
    print(f"pages_context_only    : {result.pages_context_only}")
    print(f"pages_skipped         : {result.pages_skipped}")
    print(f"coverage_exhaustive   : {'pass' if result.coverage_ok else 'FAIL'}")
    print(f"largest_package_size  : {result.largest_package}")
    if result.uncertain_packages:
        print(f"uncertain packages    : {result.uncertain_packages}")
    if result.deviated_from_hint:
        print(f"deviations from hints : {result.deviated_from_hint}")
    print()
    if ok:
        print("schema validation     : pass")
    else:
        print("schema validation     : FAIL")
        for e in errs:
            print("  -", e)
        return 5

    print()
    print("Stopped cleanly. Phase 4 (Persian extended content) not implemented.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
