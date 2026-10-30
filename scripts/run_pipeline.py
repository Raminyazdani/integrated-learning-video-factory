"""run_pipeline - unified autopilot CLI.

Commands
--------
    convert  N [--dry-run]   Phase 9E/9F.
    resume   N               Phase 11A: alias for `convert N` (idempotent).
    status   N               Phase 11A: read-only summary.
    validate N               Phase 11A: QA-only refresh.
    rebuild-pdfs N           Phase 11B: rebuild package PDFs.
    rebuild-slides N         Phase 11B: rebuild teaching slides + slide images.
    rebuild-narration N      Phase 11B: rebuild per-slide narration text.
    rebuild-audio N          Phase 11B: rebuild per-slide audio (force).
    rebuild-video N          Phase 11B: rebuild per-package + final video (force).

Exit codes
----------
    0  -> verdict == "pass"
    1  -> verdict == "fail"
    2  -> precondition error
    3  -> verdict == "pending_runtime_artifacts"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.autopilot import (  # noqa: E402
    run_convert, run_convert_dry_run, run_resume, run_status, run_validate,
)
from agents.autopilot_rebuild import run_rebuild  # noqa: E402
from integrations.env_detector import detect_environment  # noqa: E402


REBUILD_VERBS = {
    "rebuild-pdfs":      "pdfs",
    "rebuild-slides":    "slides",
    "rebuild-narration": "narration",
    "rebuild-audio":     "audio",
    "rebuild-video":     "video",
}


def _apply_env_override(env, override: str):
    if override == "local-windows":
        env.runtime = "claude_code_local_windows"
        env.notes.append("runtime overridden via --env-override=local-windows")
    elif override == "sandbox":
        env.runtime = "cowork_sandbox"
        env.notes.append("runtime overridden via --env-override=sandbox")
    return env


def _add_common(cmd_parser: argparse.ArgumentParser) -> None:
    cmd_parser.add_argument("file", help="File number (1) or filename (1.pdf/1.pptx).")
    cmd_parser.add_argument(
        "--env-override",
        choices=["auto", "sandbox", "local-windows"],
        default="auto",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description=(
            "Unified autopilot CLI. `convert N [--dry-run]` (Phase 9E/9F); "
            "`status N` / `validate N` / `resume N` (Phase 11A); "
            "five `rebuild-*` verbs (Phase 11B)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    for verb in ["convert", "resume", "status", "validate"]:
        sp = sub.add_parser(verb, help=f"{verb} a run")
        _add_common(sp)
        if verb == "convert":
            sp.add_argument("--dry-run", action="store_true",
                            help="Inspect only (no synthesizers invoked).")
            sp.add_argument(
                "--mode",
                choices=["manual_agent", "llm_dry_run", "llm_paid", "mechanical_fallback"],
                default=None,
                help="Pipeline execution mode (see configs/pipeline_modes.yaml). "
                     "Sets ILVF_PIPELINE_MODE for downstream stages. Default resolves to manual_agent.",
            )
            sp.add_argument("--approve-paid", action="store_true",
                            help="Explicitly approve paid LLM calls (only honored in --mode llm_paid; "
                                 "still requires a real llm_client + API key + budget — fail-closed otherwise).")

    for verb in REBUILD_VERBS:
        sp = sub.add_parser(verb, help=f"Phase 11B: {verb}")
        _add_common(sp)

    return parser


def _print_phase_status_table(phase_status: dict) -> None:
    for phase_id, status in phase_status.items():
        word = status.upper().replace("PENDING_RUNTIME_ARTIFACTS", "PENDING")
        print(f"  {phase_id:24s} {word}")


def _exit_code_for_verdict(v: str) -> int:
    if v == "pass":
        return 0
    if v == "fail":
        return 1
    if v in ("pending_runtime_artifacts", "pending_local_claude_code_validation"):
        return 3
    if v == "no_run":
        return 2
    return 1


def _print_convert_summary(result) -> None:
    print(f"[run_pipeline] wrote {result.report_path}")
    print(f"overall verdict : {result.overall_verdict}")
    if result.overall_reason:
        print(f"reason          : {result.overall_reason}")
    if result.aborted_at:
        print(f"aborted_at      : {result.aborted_at}")


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve + record the pipeline execution mode (fail-closed policy).
    if getattr(args, "mode", None) is not None:
        import os as _os
        _os.environ["ILVF_PIPELINE_MODE"] = args.mode
    try:
        from integrations.pipeline_mode import resolve_mode, mode_allows_paid
        _mode = resolve_mode(getattr(args, "mode", None))
        print(f"[run_pipeline] pipeline_mode = {_mode} (paid_calls={mode_allows_paid(_mode)})")
        if getattr(args, "dry_run", False) and _mode == "llm_paid":
            print("[run_pipeline] note: --dry-run forces no paid calls regardless of mode.")
    except Exception as _e:
        print(f"[run_pipeline] mode resolution warning: {_e}")

    env = detect_environment(PROJECT_ROOT)
    _apply_env_override(env, args.env_override)

    print(f"[run_pipeline] project_root  = {PROJECT_ROOT}")
    print(f"[run_pipeline] cmd           = {args.cmd} {args.file}")
    print(f"[run_pipeline] runtime       = {env.runtime} (platform={env.platform})")

    # ---- convert (9E/9F) ----
    if args.cmd == "convert":
        # When --mode is given, use the full one-command orchestrator (all stages,
        # mode gates, resume, dry-run plan). Otherwise keep the legacy convert.
        if getattr(args, "mode", None) is not None:
            from agents.full_orchestrator import write_plan_report, run_full_convert, plan
            if args.dry_run:
                rp = write_plan_report(args.file, args.mode)
                pl = plan(args.file, args.mode)
                print(f"[run_pipeline] wrote full plan {rp}")
                for r in pl["stages"]:
                    flag = "✓" if r["done"] else " "
                    print(f"  [{flag}] {r['stage']:<16} {r['action']}")
                blocked = [r for r in pl["stages"] if r["action"].startswith("BLOCKED")]
                return 3 if blocked else 0
            res = run_full_convert(args.file, args.mode, approve_paid=getattr(args, "approve_paid", False))
            for sid, status in res["results"]:
                print(f"  {sid:<16} {status}")
            if res.get("stopped_at"):
                print(f"[run_pipeline] STOPPED at {res['stopped_at']}: {res.get('reason','')}")
                return 3
            print("[run_pipeline] convert complete (all stages done/skip).")
            return 0
        if args.dry_run:
            r = run_convert_dry_run(project_root=PROJECT_ROOT, file_arg=args.file, env_info=env)
            if r.error:
                print(f"[ERROR] {r.error}", file=sys.stderr); return 2
            print(f"[run_pipeline] wrote {r.report_path}")
            print(f"qa_verdict : {r.qa_verdict}")
            print(f"next       : {r.next_recommended_action}")
            return _exit_code_for_verdict(r.qa_verdict)
        r = run_convert(project_root=PROJECT_ROOT, file_arg=args.file, env_info=env)
        _print_convert_summary(r)
        return _exit_code_for_verdict(r.overall_verdict)

    if args.cmd == "resume":
        r = run_resume(project_root=PROJECT_ROOT, file_arg=args.file, env_info=env)
        _print_convert_summary(r)
        return _exit_code_for_verdict(r.overall_verdict)

    if args.cmd == "status":
        r = run_status(project_root=PROJECT_ROOT, file_arg=args.file, env_info=env)
        if r.error:
            print(f"[ERROR] {r.error}", file=sys.stderr); return 2
        print(f"run_dir    : {r.run_dir or '(none)'}")
        print(f"qa_verdict : {r.qa_verdict}")
        if r.qa_reason:
            print(f"qa_reason  : {r.qa_reason}")
        print("\n=== Phase status ===")
        _print_phase_status_table(r.phase_status)
        print(f"\nNext       : {r.next_action}")
        return _exit_code_for_verdict(r.qa_verdict)

    if args.cmd == "validate":
        r = run_validate(project_root=PROJECT_ROOT, file_arg=args.file, env_info=env)
        if r.error:
            print(f"[ERROR] {r.error}", file=sys.stderr); return 2
        if r.run_dir is None:
            print("(no run found)"); return 2
        print(f"run_dir          : {r.run_dir}")
        print(f"qa_verdict       : {r.qa_verdict}")
        if r.qa_reason:
            print(f"qa_reason        : {r.qa_reason}")
        print(f"artifact_manifest: {r.artifact_manifest_path}")
        print(f"final_report     : {r.final_report_path}")
        print(f"summary          : {r.summary_path}")
        print("\n=== Phase status ===")
        _print_phase_status_table(r.phase_status)
        return _exit_code_for_verdict(r.qa_verdict)

    # ---- rebuild-* (Phase 11B) ----
    if args.cmd in REBUILD_VERBS:
        layer = REBUILD_VERBS[args.cmd]
        r = run_rebuild(
            project_root=PROJECT_ROOT, file_arg=args.file,
            layer=layer, env_info=env,
        )
        _print_convert_summary(r)
        # Show last step's exit + wall as quick feedback.
        if r.steps:
            last = r.steps[-1]
            print(
                f"step.last       : stage_id={last.stage_id} "
                f"verdict={last.verdict} exit={last.exit_code} "
                f"wall={last.wall_seconds}s"
            )
        return _exit_code_for_verdict(r.overall_verdict)

    print(f"[ERROR] unknown command: {args.cmd}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
