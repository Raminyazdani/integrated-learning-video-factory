"""generate_narration - Phase 7 CLI for per-slide Persian narration generation.

Phase 7A: --package <pid>  (single package, no TTS)
Phase 7B: --all            (every package + combined full_narration.json)
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.narration_writer_agent import (  # noqa: E402
    run_narration_for_package, run_all_packages_narration,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="generate_narration",
        description="Phase 7 per-slide Persian narration writer (no TTS).",
    )
    parser.add_argument("--run", required=True, help="Path to runs/<run_id>/")
    parser.add_argument("--package",
                        help="Phase 7A: build narration for one package_id.")
    parser.add_argument("--all", action="store_true",
                        help="Phase 7B: build narration for every package + full_narration.json + QA report.")
    args = parser.parse_args(argv)

    modes = sum([bool(args.package), bool(args.all)])
    if modes == 0:
        parser.error("Specify --package or --all.")
    if modes > 1:
        parser.error("--package and --all are mutually exclusive.")

    run_dir = Path(args.run).resolve() if Path(args.run).is_absolute() else (PROJECT_ROOT / args.run).resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        print("[ERROR] run folder not found: " + str(run_dir), file=sys.stderr)
        return 2

    if args.package:
        return _run_one(run_dir, args.package)
    return _run_all(run_dir)


def _run_one(run_dir, package_id):
    print("[Phase 7A] Narration build for {} in: {}".format(package_id, run_dir))
    try:
        r = run_narration_for_package(run_dir, PROJECT_ROOT, package_id)
    except FileNotFoundError as e:
        print("[ERROR] " + str(e), file=sys.stderr); return 2
    except Exception:
        traceback.print_exc(); return 1

    print()
    print("=== Phase 7A summary ({}) ===".format(r.package_id))
    print("slides in / out      : {} / {}".format(r.n_slides_in, r.n_slides_out))
    print("total Persian chars  : {}".format(r.total_chars))
    print("total estimated_secs : {}".format(r.total_estimated_seconds))
    print("avg   estimated_secs : {}".format(r.avg_estimated_seconds))
    print("schema valid         : {}".format(r.schema_valid))
    if r.schema_errors:
        for e in r.schema_errors: print("  schema err: " + e)
    print("json path            : {}".format(r.json_path))
    print("report path          : {}".format(r.report_path))
    if r.success:
        print("[Phase 7A] NARRATION {}: pass".format(r.package_id)); return 0
    print("[Phase 7A] NARRATION {}: FAIL".format(r.package_id)); return 3


def _run_all(run_dir):
    print("[Phase 7B] Narration build for all packages in: " + str(run_dir))
    try:
        r = run_all_packages_narration(run_dir, PROJECT_ROOT)
    except FileNotFoundError as e:
        print("[ERROR] " + str(e), file=sys.stderr); return 2
    except Exception:
        traceback.print_exc(); return 1

    print()
    print("=== Phase 7B per-package summary ===")
    for e in r.per_package:
        flag = "pass" if e["ok"] else "FAIL"
        print("  {}: n_in={}  n_out={}  chars={}  secs={}  {}".format(
            e["package_id"], e["n_in"], e["n_out"], e["chars"], e["secs"], flag))
    print()
    print("=== Phase 7B aggregate ===")
    print("packages              : {} ({} ok, {} fail)".format(r.n_packages, r.n_ok, r.n_fail))
    print("total slides in / out : {} / {}".format(r.total_slides_in, r.total_slides_out))
    print("total Persian chars   : {}".format(r.total_chars))
    print("total estimated secs  : {:.1f}  ({:.1f} min)".format(r.total_seconds, r.total_seconds/60))
    print("full_narration.json   : {} ({} bytes)".format(r.full_narration_path,
        r.full_narration_path.stat().st_size if r.full_narration_path.exists() else 0))
    print("qa_report             : {}".format(r.qa_report_path))
    print("combined schema valid : {}".format(r.combined_schema_valid))
    if r.combined_schema_errors:
        for e in r.combined_schema_errors: print("  schema err: " + e)
    print()
    if r.success:
        print("[Phase 7B] NARRATION ALL: pass"); return 0
    print("[Phase 7B] NARRATION ALL: FAIL"); return 3


if __name__ == "__main__":
    sys.exit(main())
