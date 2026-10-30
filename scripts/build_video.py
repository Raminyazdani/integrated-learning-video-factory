"""build_video - Phase 9A/9B/9C CLI (ffmpeg video).

Modes
-----
* Phase 9A (smoke test):       ``--smoke-test``
* Phase 9B (per-package):      ``--package package_NNN``
* Phase 9C (all packages):     ``--all``
* Phase 9C (all + final):      ``--all --concat-final``
* Final concat only (re-use existing package MP4s): ``--concat-final``

Usage
-----
    python scripts/build_video.py --run runs/001_1_20260520_2128 --smoke-test
    python scripts/build_video.py --run runs/001_1_20260520_2128 --package 1
    python scripts/build_video.py --run runs/001_1_20260520_2128 --all
    python scripts/build_video.py --run runs/001_1_20260520_2128 --all --concat-final

Exit codes
----------
    0 -> verdict == "pass"
    1 -> verdict == "fail"
    2 -> precondition error
    3 -> verdict == "pending_local_claude_code_validation"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.video_builder_agent import (  # noqa: E402
    build_all_packages_video,
    build_final_video,
    build_package_video,
    run_smoke_test,
    static_health_check,
    write_video_build_report,
)
from integrations.env_detector import detect_environment  # noqa: E402


def _normalize_package_id(arg: str) -> str:
    arg = arg.strip()
    if not arg:
        raise ValueError("--package cannot be empty")
    if arg.startswith("package_"):
        return arg
    if arg.isdigit():
        return f"package_{int(arg):03d}"
    raise ValueError(f"unrecognized --package value: {arg!r}")


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
        prog="build_video",
        description=(
            "Phase 9A/9B/9C ffmpeg CLI. --smoke-test (9A), --package (9B), "
            "--all (9C). Add --concat-final to also concatenate every "
            "per-package MP4 into output/<run_number>_final_educational_video.mp4. "
            "Never fabricates missing audio."
        ),
    )
    parser.add_argument(
        "--run", required=True,
        help="Path to a run directory (e.g. runs/001_1_20260520_2128).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--smoke-test", action="store_true",
        help="Phase 9A: single ffmpeg smoke clip.",
    )
    mode.add_argument(
        "--package", default=None,
        help="Phase 9B: build a single package video.",
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Phase 9C: build every package video.",
    )
    mode.add_argument(
        "--concat-final-only", action="store_true",
        help="Skip package builds; just concatenate existing package MP4s into the final video.",
    )
    parser.add_argument(
        "--concat-final", action="store_true",
        help="Modifier for --all: after building all package videos, "
             "concatenate them into output/<run_number>_final_educational_video.mp4.",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-encode existing clips and package videos.",
    )
    parser.add_argument("--force", action="store_true", help="Alias for --overwrite.")
    parser.add_argument(
        "--env-override",
        choices=["auto", "sandbox", "local-windows"],
        default="auto",
    )
    args = parser.parse_args(argv)

    overwrite = bool(args.overwrite or args.force)

    try:
        run_dir = _resolve_run_dir(args.run)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    run_id = run_dir.name

    print(f"[build_video] project_root  = {PROJECT_ROOT}")
    print(f"[build_video] run           = {run_dir}")

    env = detect_environment(PROJECT_ROOT)
    if args.env_override == "local-windows":
        env.runtime = "claude_code_local_windows"
        env.notes.append("runtime overridden via --env-override=local-windows")
    elif args.env_override == "sandbox":
        env.runtime = "cowork_sandbox"
        env.notes.append("runtime overridden via --env-override=sandbox")
    print(
        f"[build_video] runtime       = {env.runtime} "
        f"(platform={env.platform}, can_exec_venv={env.can_execute_windows_venv}, "
        f"proxy_403={env.has_proxy_403_signature})"
    )

    health = static_health_check(PROJECT_ROOT)
    print(
        f"[build_video] static_health = "
        f"{'pass' if health['ok'] else 'fail'} "
        f"({sum(1 for v in health['imports'].values() if v == 'ok')}/"
        f"{len(health['imports'])} modules)"
    )

    qa_dir = run_dir / "QA_REPORTS"
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_path = qa_dir / "video_build_report.md"

    # ---- Phase 9A smoke ----
    if args.smoke_test:
        smoke = run_smoke_test(project_root=PROJECT_ROOT, run_dir=run_dir)
        print(f"[build_video] verdict       = {smoke.verdict}")
        if smoke.reason:
            print(f"[build_video] reason        = {smoke.reason}")
        write_video_build_report(
            report_path=report_path, run_id=run_id,
            smoke=smoke, env_info=env, static_health=health,
        )
        print(f"[build_video] wrote {report_path}")
        print()
        print("=== Phase 9A summary ===")
        print(f"runtime    : {env.runtime}")
        print(f"verdict    : {smoke.verdict}")
        if smoke.verdict == "pass":
            return 0
        if smoke.verdict == "fail":
            return 1
        return 3

    # ---- Phase 9B single-package ----
    if args.package is not None:
        try:
            package_id = _normalize_package_id(args.package)
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2
        print(f"[build_video] mode          = Phase 9B: {package_id}")

        def _progress(i: int, n: int, slide_id: str, note: str) -> None:
            print(f"  [{i:3d}/{n}] {slide_id}  -> {note}")

        pkg_result = build_package_video(
            project_root=PROJECT_ROOT, run_dir=run_dir, package_id=package_id,
            width=args.width, height=args.height, fps=args.fps,
            overwrite=overwrite, progress_callback=_progress,
        )
        write_video_build_report(
            report_path=report_path, run_id=run_id,
            package_result=pkg_result, env_info=env, static_health=health,
        )
        print(f"[build_video] wrote {report_path}")
        print()
        print("=== Phase 9B summary ===")
        print(f"runtime    : {env.runtime}")
        print(f"verdict    : {pkg_result.verdict}")
        if pkg_result.verdict == "pass":
            print(f"video      : {pkg_result.package_video_path} "
                  f"({pkg_result.package_video_size_bytes:,} B, "
                  f"{pkg_result.package_video_duration_seconds}s)")
            return 0
        if pkg_result.verdict == "fail":
            return 1
        return 3

    # ---- Phase 9C all + optional final concat ----
    if args.all:
        print(
            "[build_video] mode          = Phase 9C: all packages"
            + (" + final concat" if args.concat_final else "")
        )

        last_pkg = {"id": None}

        def _all_progress(pkg_id: str, i: int, n: int, slide_id: str, note: str) -> None:
            if pkg_id != last_pkg["id"]:
                last_pkg["id"] = pkg_id
                print(f"  >> {pkg_id} ({n} slides)")
            print(f"     [{i:3d}/{n}] {slide_id}  -> {note}")

        run_result = build_all_packages_video(
            project_root=PROJECT_ROOT, run_dir=run_dir,
            width=args.width, height=args.height, fps=args.fps,
            overwrite=overwrite, progress_callback=_all_progress,
        )

        final_result = None
        if args.concat_final:
            if run_result.verdict == "pass":
                print("[build_video] concat-final  = building final video ...")
                final_result = build_final_video(
                    project_root=PROJECT_ROOT, run_dir=run_dir,
                    run_result=run_result,
                    width=args.width, height=args.height, fps=args.fps,
                )
                print(
                    f"[build_video] final         = success={final_result.success} "
                    f"size={final_result.size_bytes:,} "
                    f"dur={final_result.duration_seconds}"
                )
            else:
                print(
                    "[build_video] concat-final  = SKIPPED (package builds did "
                    "not all pass; no fake final video created)."
                )

        # Aggregate verdict for the script
        if final_result is not None:
            if final_result.success and run_result.verdict == "pass":
                run_result.verdict = "pass"
            elif run_result.verdict == "pass" and not final_result.success:
                run_result.verdict = "fail"
                run_result.reason = (
                    "all package videos built but final concat failed: "
                    f"{final_result.error}"
                )

        write_video_build_report(
            report_path=report_path, run_id=run_id,
            run_result=run_result, final_result=final_result,
            env_info=env, static_health=health,
        )
        print(f"[build_video] wrote {report_path}")
        print()
        print("=== Phase 9C summary ===")
        print(f"runtime         : {env.runtime}")
        print(f"verdict         : {run_result.verdict}")
        if run_result.reason:
            print(f"reason          : {run_result.reason}")
        print(f"packages        : {run_result.n_packages}")
        print(f"  passed        : {run_result.n_packages_passed}")
        print(f"  failed        : {run_result.n_packages_failed}")
        print(f"  pending       : {run_result.n_packages_pending}")
        print(f"wall_total      : {run_result.total_wall_seconds} s")
        if final_result is not None:
            print(
                f"final_video     : "
                f"{'created' if final_result.success else 'NOT created'} "
                f"({final_result.size_bytes:,} B, "
                f"{final_result.duration_seconds}s)"
            )
            if final_result.success:
                print(f"final_path      : {final_result.output_path}")
        else:
            print("final_video     : not requested (no --concat-final)")
        if run_result.verdict == "pass":
            return 0
        if run_result.verdict == "fail":
            return 1
        return 3

    # ---- Final-concat-only mode ----
    if args.concat_final_only:
        print("[build_video] mode          = final concat only (no per-package builds)")
        final_result = build_final_video(
            project_root=PROJECT_ROOT, run_dir=run_dir, run_result=None,
            width=args.width, height=args.height, fps=args.fps,
        )
        # Synthesize a thin RunVideoResult so the report writer can render
        from agents.video_builder_agent import RunVideoResult
        rr = RunVideoResult(
            run_id=run_id,
            verdict=("pass" if final_result.success else "fail"),
            reason=(None if final_result.success else final_result.error),
        )
        write_video_build_report(
            report_path=report_path, run_id=run_id,
            run_result=rr, final_result=final_result,
            env_info=env, static_health=health,
        )
        print(f"[build_video] wrote {report_path}")
        print()
        print("=== Phase 9C summary (final-only) ===")
        print(f"final_video     : "
              f"{'created' if final_result.success else 'NOT created'} "
              f"({final_result.size_bytes:,} B)")
        return 0 if final_result.success else 1

    print("[ERROR] unreachable: no mode selected", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
