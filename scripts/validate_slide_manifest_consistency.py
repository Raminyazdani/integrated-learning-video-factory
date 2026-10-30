"""validate_slide_manifest_consistency - enforce combined-manifest source-of-truth.

Read-only validator (writes only a QA report). Confirms that
`slides/slide_manifest.json` (combined deck) is complete and self-consistent and
that, where present, narration/audio cover every combined slide. Per-package
manifests are secondary/debug; their divergence is reported as informational.

Usage:
    python scripts/validate_slide_manifest_consistency.py --run runs/<RUN_ID>

Exit codes: 0 = PASS, 3 = FAIL, 2 = run not found.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.slide_manifest_consistency import (  # noqa: E402
    check_slide_manifest_consistency,
    write_consistency_report,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="validate_slide_manifest_consistency")
    ap.add_argument("--run", required=True, help="Path to runs/<run_id>/")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run folder not found: {run_dir}", file=sys.stderr)
        return 2

    result = check_slide_manifest_consistency(run_dir)
    report = write_consistency_report(run_dir, result)

    print(f"[slide-manifest consistency] run = {run_dir.name}")
    print(f"  strategy           : {result['strategy']}")
    print(f"  authoritative      : {result['authoritative_manifest']}")
    print(f"  combined slides    : {result['combined_slide_count']} ({len(result['combined_packages'])} packages)")
    print(f"  per-package files  : {result['per_package_files']} (sum={result['per_package_slide_sum']}, divergence={result['per_package_divergence']})")
    for k, v in result["checks"].items():
        word = "pass" if v is True else ("FAIL" if v is False else "n/a")
        print(f"  - {k}: {word}")
    if result["error"]:
        print(f"  error              : {result['error']}")
    for i in result["issues"]:
        print(f"  ISSUE              : {i}")
    for i in result["informational"]:
        print(f"  info               : {i}")
    print(f"  report             : {report}")
    print(f"  VERDICT            : {result['verdict']}")
    return 0 if result["verdict"] == "PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
