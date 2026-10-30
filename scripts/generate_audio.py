"""generate_audio - Phase 8B/8C CLI (per-package or all-package audio).

Scope
-----
* Phase 8B (single package): ``--package package_NNN`` (or ``--package N``).
  Reads narration_scripts/<package>.json (falls back to full_narration.json),
  synthesizes one MP3 per slide into audio/<package>/slide_NNN.mp3, and
  updates audio/audio_manifest.json atomically.
* Phase 8C (all packages):  ``--all``.
  Reads narration_scripts/full_narration.json and iterates every package.
  Idempotent: existing audio files that already validate (size > 0 AND
  duration > 1 s) are reused unless ``--force`` is passed.

Both modes write a unified QA_REPORTS/tts_report.md.

Sandbox / Cowork compatibility
------------------------------
When the runtime is the Cowork Linux sandbox (or the network is restricted)
and no provider is reachable, the script REFUSES to synthesize and writes a
PENDING_LOCAL_CLAUDE_CODE_VALIDATION report with the exact Windows command to
run. No fake audio files are ever created.

Usage
-----
    # Phase 8B - one package
    python scripts/generate_audio.py --run runs/001_1_20260520_2128 --package package_001
    python scripts/generate_audio.py --run runs/001_1_20260520_2128 --package 1

    # Phase 8C - all 12 packages
    python scripts/generate_audio.py --run runs/001_1_20260520_2128 --all
    python scripts/generate_audio.py --run runs/001_1_20260520_2128 --all --force

Exit codes
----------
    0 -> verdict == "pass"
    1 -> verdict == "fail"
    2 -> precondition error (missing narration, etc.)
    3 -> verdict == "pending_local_claude_code_validation"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.tts_audio_agent import (  # noqa: E402
    load_tts_config,
    probe_chain,
    static_health_check,
    synthesize_all_audio,
    synthesize_package_audio,
    write_tts_report,
)
from integrations.env_detector import (  # noqa: E402
    decide_verdict,
    detect_environment,
)


def _normalize_package_id(arg: str) -> str:
    arg = arg.strip()
    if not arg:
        raise ValueError("--package cannot be empty")
    if arg.startswith("package_"):
        return arg
    if arg.isdigit():
        return f"package_{int(arg):03d}"
    raise ValueError(
        f"unrecognized --package value: {arg!r}; "
        "use 'package_001' or a numeric index like '1'"
    )


def _resolve_run_dir(run_arg: str) -> Path:
    p = Path(run_arg)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"run directory not found: {p}")
    return p


def _check_full_narration(run_dir: Path) -> Path:
    full = run_dir / "narration_scripts" / "full_narration.json"
    if not full.is_file():
        raise FileNotFoundError(
            f"precondition failed: {full} not found. Phase 7B must complete."
        )
    return full


def _check_package_narration(run_dir: Path, package_id: str) -> Path:
    per_pkg = run_dir / "narration_scripts" / f"{package_id}.json"
    full = run_dir / "narration_scripts" / "full_narration.json"
    if per_pkg.is_file():
        return per_pkg
    if full.is_file():
        return full
    raise FileNotFoundError(
        f"precondition failed: neither {per_pkg} nor {full} found. "
        "Phase 7B must complete before Phase 8B runs."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_audio",
        description=(
            "Phase 8B/8C: synthesize TTS audio. --package = one package "
            "(8B). --all = every package (8C, idempotent unless --force). "
            "Never runs ffmpeg. Never creates fake audio when the provider "
            "is unreachable."
        ),
    )
    parser.add_argument(
        "--run",
        required=True,
        help="Path to a run directory (e.g. runs/001_1_20260520_2128).",
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--package",
        default=None,
        help="Synthesize a single package (e.g. 'package_001' or '1'). Phase 8B.",
    )
    mode.add_argument(
        "--all",
        action="store_true",
        help="Synthesize every package in full_narration.json. Phase 8C.",
    )

    parser.add_argument(
        "--provider",
        default=None,
        help="Override the primary provider (must exist in tts_config.json).",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="Override the Persian voice (e.g. 'fa-IR-FaridNeural').",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip live network probes; only the static checks will run.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-synthesize even if a target audio file already exists.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Alias for --overwrite. Required to overwrite already-valid audio.",
    )
    parser.add_argument(
        "--env-override",
        choices=["auto", "sandbox", "local-windows"],
        default="auto",
        help="Manually override env detection (debug only).",
    )
    args = parser.parse_args(argv)

    overwrite = bool(args.overwrite or args.force)
    is_all = bool(args.all)
    package_id: Optional[str] = None

    # ---- resolve inputs ----
    try:
        run_dir = _resolve_run_dir(args.run)
        if is_all:
            narration_source = _check_full_narration(run_dir)
        else:
            package_id = _normalize_package_id(args.package)
            narration_source = _check_package_narration(run_dir, package_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    run_id = run_dir.name
    network = not args.no_network
    scope = "all packages" if is_all else package_id

    print(f"[generate_audio] project_root  = {PROJECT_ROOT}")
    print(f"[generate_audio] run           = {run_dir}")
    print(f"[generate_audio] scope         = {scope}")
    print(f"[generate_audio] narration src = {narration_source}")
    print(f"[generate_audio] network probe = {network}")
    print(f"[generate_audio] overwrite     = {overwrite}")

    # ---- environment detection ----
    env = detect_environment(PROJECT_ROOT)
    if args.env_override == "local-windows":
        env.runtime = "claude_code_local_windows"
        env.notes.append("runtime overridden via --env-override=local-windows")
    elif args.env_override == "sandbox":
        env.runtime = "cowork_sandbox"
        env.notes.append("runtime overridden via --env-override=sandbox")
    print(
        f"[generate_audio] runtime       = {env.runtime} "
        f"(platform={env.platform}, can_exec_venv={env.can_execute_windows_venv}, "
        f"proxy_403={env.has_proxy_403_signature})"
    )

    # ---- static health check ----
    health = static_health_check(PROJECT_ROOT)
    print(
        f"[generate_audio] static_health = "
        f"{'pass' if health['ok'] else 'fail'} "
        f"({sum(1 for v in health['imports'].values() if v == 'ok')}/"
        f"{len(health['imports'])} modules)"
    )

    # ---- provider probe ----
    config = load_tts_config(PROJECT_ROOT)
    outcome = probe_chain(config, project_root=PROJECT_ROOT, network=network)
    primary = args.provider or outcome.primary
    voice = args.voice or outcome.selected_voice

    qa_dir = run_dir / "QA_REPORTS"
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_path = qa_dir / "tts_report.md"

    provider_reachable = (
        primary is not None
        and outcome.probes.get(primary) is not None
        and outcome.probes[primary].available
    )

    # ---- env-blocked / no-provider path ----
    if not provider_reachable:
        verdict = decide_verdict(
            env=env,
            primary_provider=primary,
            smoke_ok=False,
            smoke_duration=None,
        )
        reason = (
            "no provider in the chain is reachable from this environment; "
            "audio synthesis is **not attempted** to avoid creating fake or "
            "empty audio files."
        )
        print(f"[generate_audio] verdict       = {verdict}")
        write_tts_report(
            report_path=report_path,
            run_id=run_id,
            package_id=package_id,
            env_info=env,
            static_health=health,
            provider_chain=list(config.get("provider_chain") or []),
            primary_provider=primary,
            selected_voice=voice,
            package_result=None,
            run_result=None,
            verdict=verdict,
            reason=reason,
        )
        print(f"[generate_audio] wrote {report_path}")
        print()
        print("=== Phase 8B/8C summary ===")
        print(f"scope           : {scope}")
        print(f"runtime         : {env.runtime}")
        print(f"verdict         : {verdict}")
        print("audio_generated : no")
        print("provider_summary:", primary or "(none)")
        print("reason          :", reason)
        print()
        if verdict == "pass":
            return 0
        if verdict == "fail":
            return 1
        return 3

    # ---- actually synthesize ----
    print(f"[generate_audio] primary       = {primary} (voice={voice})")

    if is_all:
        last_pkg = {"id": None}

        def _all_progress(pkg_id: str, i: int, n: int, slide_id: str) -> None:
            if pkg_id != last_pkg["id"]:
                last_pkg["id"] = pkg_id
                print(f"  >> {pkg_id} ({n} slides)")
            print(f"     [{i:3d}/{n}] {slide_id}")

        try:
            run_result = synthesize_all_audio(
                project_root=PROJECT_ROOT,
                run_dir=run_dir,
                config=config,
                provider_name=primary,
                voice=voice,
                overwrite=overwrite,
                progress_callback=_all_progress,
            )
        except FileNotFoundError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2

        all_ok = run_result.n_slides_total > 0 and run_result.n_failed == 0
        verdict = "pass" if all_ok else (
            "pending_local_claude_code_validation"
            if env.runtime == "cowork_sandbox"
            else "fail"
        )

        write_tts_report(
            report_path=report_path,
            run_id=run_id,
            package_id=None,
            env_info=env,
            static_health=health,
            provider_chain=list(config.get("provider_chain") or []),
            primary_provider=primary,
            selected_voice=run_result.voice or voice,
            package_result=None,
            run_result=run_result,
            verdict=verdict,
        )

        print(f"[generate_audio] wrote {report_path}")
        print(f"[generate_audio] wrote {run_result.manifest_path}")
        print()
        print("=== Phase 8C summary ===")
        print(f"runtime         : {env.runtime}")
        print(f"verdict         : {verdict}")
        print(f"provider        : {run_result.provider}")
        print(f"voice           : {run_result.voice}")
        print(f"packages        : {run_result.n_packages}")
        print(f"slides_total    : {run_result.n_slides_total}")
        print(f"succeeded       : {run_result.n_succeeded}")
        print(f"failed          : {run_result.n_failed}")
        print(f"reused          : {run_result.n_reused}")
        print(f"audio_bytes     : {run_result.total_audio_bytes:,}")
        print(f"duration_total  : {run_result.total_duration_seconds:.3f} s")
        print(f"wall_total      : {run_result.total_wall_seconds:.3f} s")
        print()
        if verdict == "pass":
            print("Phase 8C complete. Phase 9A (ffmpeg) can now be planned.")
            return 0
        if verdict == "fail":
            return 1
        return 3

    # ---- single-package path (Phase 8B) ----
    assert package_id is not None

    def _pkg_progress(i: int, n: int, slide_id: str) -> None:
        print(f"  [{i:3d}/{n}] {slide_id}")

    try:
        pkg_result = synthesize_package_audio(
            project_root=PROJECT_ROOT,
            run_dir=run_dir,
            package_id=package_id,
            config=config,
            provider_name=primary,
            voice=voice,
            overwrite=overwrite,
            progress_callback=_pkg_progress,
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    all_ok = pkg_result.n_slides > 0 and pkg_result.n_failed == 0
    verdict = "pass" if all_ok else (
        "pending_local_claude_code_validation"
        if env.runtime == "cowork_sandbox"
        else "fail"
    )

    write_tts_report(
        report_path=report_path,
        run_id=run_id,
        package_id=package_id,
        env_info=env,
        static_health=health,
        provider_chain=list(config.get("provider_chain") or []),
        primary_provider=primary,
        selected_voice=voice,
        package_result=pkg_result,
        run_result=None,
        verdict=verdict,
    )

    print(f"[generate_audio] wrote {report_path}")
    print(f"[generate_audio] wrote {pkg_result.manifest_path}")
    print()
    print("=== Phase 8B summary ===")
    print(f"runtime         : {env.runtime}")
    print(f"verdict         : {verdict}")
    print(f"provider        : {pkg_result.provider}")
    print(f"voice           : {pkg_result.voice}")
    print(f"slides          : {pkg_result.n_slides}")
    print(f"succeeded       : {pkg_result.n_succeeded}")
    print(f"failed          : {pkg_result.n_failed}")
    print(f"reused          : {pkg_result.n_reused}")
    print(f"audio_bytes     : {pkg_result.total_audio_bytes:,}")
    print(f"duration_total  : {pkg_result.total_duration_seconds:.3f} s")
    print(f"wall_total      : {pkg_result.total_wall_seconds:.3f} s")
    print()
    if verdict == "pass":
        return 0
    if verdict == "fail":
        return 1
    return 3


if __name__ == "__main__":
    sys.exit(main())
