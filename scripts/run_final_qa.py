"""run_final_qa - Phase 9D CLI.

Scans every phase's artifacts under a run directory and writes:
  * runs/<run_id>/artifact_manifest.json
  * runs/<run_id>/QA_REPORTS/final_validation_report.md
  * output/<run_number>_artifact_summary.md

Never produces audio/video. Never sets PASS unless every phase actually
validated. When Phase 8 or Phase 9 outputs are missing because the sandbox
can't run them, the verdict is `PENDING_RUNTIME_ARTIFACTS` (not PASS).

Usage
-----
    python scripts/run_final_qa.py --run runs/001_1_20260520_2128

Exit codes
----------
    0 -> verdict == "pass"
    1 -> verdict == "fail"
    2 -> precondition error (missing run dir)
    3 -> verdict == "pending_runtime_artifacts"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.qa_critic import run_final_qa  # noqa: E402
from integrations.env_detector import detect_environment  # noqa: E402


def _resolve_run_dir(run_arg: str) -> Path:
    p = Path(run_arg)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"run directory not found: {p}")
    return p


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_final_qa",
        description=(
            "Phase 9D: scan every phase's artifacts, write the artifact "
            "manifest, the final validation report, and the user-facing "
            "summary. Never fabricates a PASS verdict."
        ),
    )
    parser.add_argument(
        "--run",
        required=True,
        help="Path to a run directory (e.g. runs/001_1_20260520_2128).",
    )
    parser.add_argument(
        "--env-override",
        choices=["auto", "sandbox", "local-windows"],
        default="auto",
    )
    args = parser.parse_args(argv)

    try:
        run_dir = _resolve_run_dir(args.run)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    env = detect_environment(PROJECT_ROOT)
    if args.env_override == "local-windows":
        env.runtime = "claude_code_local_windows"
        env.notes.append("runtime overridden via --env-override=local-windows")
    elif args.env_override == "sandbox":
        env.runtime = "cowork_sandbox"
        env.notes.append("runtime overridden via --env-override=sandbox")

    print(f"[run_final_qa] project_root  = {PROJECT_ROOT}")
    print(f"[run_final_qa] run           = {run_dir}")
    print(
        f"[run_final_qa] runtime       = {env.runtime} "
        f"(platform={env.platform})"
    )

    qa = run_final_qa(project_root=PROJECT_ROOT, run_dir=run_dir, env_info=env)

    print()
    print("=== Phase 9D summary ===")
    print(f"verdict          : {qa.verdict.upper()}")
    if qa.reason:
        print(f"reason           : {qa.reason}")
    for phase_id, check in qa.checks.items():
        marker = {
            "pass": "PASS",
            "fail": "FAIL",
            "pending_runtime_artifacts": "PENDING",
            "warn": "WARN",
        }.get(check.status, check.status.upper())
        expected = (
            f"{check.found_count}/{check.expected_count}"
            if check.expected_count is not None
            else f"{check.found_count}"
        )
        print(f"  {phase_id:24s} {marker:8s}  {expected:>9s}  {check.total_bytes:>12,} B")
    print()
    print(f"artifact_manifest : {qa.artifact_manifest_path}")
    print(f"final_report      : {qa.final_report_path}")
    print(f"summary           : {qa.summary_path}")

    if qa.verdict == "pass":
        print()
        print("Phase 9D PASS. The MVP video pipeline is end-to-end complete.")
        return 0
    if qa.verdict == "fail":
        return 1
    return 3


if __name__ == "__main__":
    sys.exit(main())
