"""check_tts_providers - Phase 8A entry point (Claude Code compatible).

Scope (strict)
--------------
1. Detect the execution environment (Cowork sandbox vs Claude Code/local Windows).
2. Run a `static_health_check` on the Phase 8 code paths - this passes even
   without network and proves the code is healthy regardless of environment.
3. Read `configs/tts_config.json`.
4. Probe each provider in the chain (and any other enabled providers) for
   importability, voice catalog availability, and (if `--network`) live
   reachability.
5. Resolve a default + fallback Persian voice for the chosen primary.
6. Synthesize one short Persian smoke sentence (configurable via `--text`)
   into `<run>/audio/_smoke/smoke_test_fa.<ext>` - only when a provider is
   actually available. Never write a placeholder/empty audio file.
7. Decide a verdict via `integrations/env_detector.decide_verdict`:
     * `pass`                                  - real Phase 8A success
     * `pending_local_claude_code_validation`  - environment-blocked; re-run on local Windows
     * `fail`                                  - real failure (block Phase 8B)
8. Write `<run>/QA_REPORTS/tts_provider_check_report.md` and a JSON companion
   `tts_provider_check_report.json`, both carrying env_info, static_health,
   and verdict.
9. Exit. Does NOT loop over the 254 slides.

Preconditions
-------------
* `<run>/narration_scripts/full_narration.json` must exist (Phase 7B done).
* The script refuses to run if Phase 8B audio output already exists under
  `<run>/audio/` (other than the smoke folder we manage). Use
  `--allow-existing-audio` to override (Phase 8B will not be triggered;
  this is just a courtesy guard).

Usage
-----
    python scripts/check_tts_providers.py --run runs/001_1_20260520_2128
    python scripts/check_tts_providers.py --run runs/001_1_20260520_2128 --no-network
    python scripts/check_tts_providers.py --run runs/001_1_20260520_2128 --env-override local-windows

Exit codes
----------
    0 -> verdict == "pass"
    1 -> verdict == "fail"
    2 -> precondition error (missing narration, etc.)
    3 -> verdict == "pending_local_claude_code_validation"
         (environment-blocked; not a true failure)
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
    inspect_audio,
    load_tts_config,
    probe_chain,
    smoke_synthesize,
    static_health_check,
    write_provider_check_json,
    write_provider_check_report,
)
from integrations.env_detector import (  # noqa: E402
    decide_verdict,
    detect_environment,
)


SMOKE_FILE_NAME_TEMPLATE = "smoke_test_fa.{ext}"
DEFAULT_SMOKE_TEXT = "این یک آزمون TTS است."


def _ensure_run_dir(run: Path) -> Path:
    run = run.resolve()
    if not run.is_dir():
        raise FileNotFoundError(f"run directory not found: {run}")
    return run


def _check_phase7_done(run: Path) -> None:
    narration = run / "narration_scripts" / "full_narration.json"
    if not narration.is_file():
        raise FileNotFoundError(
            f"precondition failed: {narration} not found. "
            "Phase 7B must complete before Phase 8A runs."
        )


def _check_no_existing_real_audio(run: Path, allow_existing: bool) -> None:
    audio_dir = run / "audio"
    if not audio_dir.is_dir():
        return
    extras = [p for p in audio_dir.iterdir() if p.name != "_smoke"]
    if extras and not allow_existing:
        raise RuntimeError(
            "Phase 8B audio appears to already exist under "
            f"{audio_dir}. Refusing to run Phase 8A. "
            "Pass --allow-existing-audio to override."
        )


def _resolve_run_id(run: Path) -> str:
    return run.name


def _provider_ext(provider_name: Optional[str]) -> str:
    if provider_name == "pyttsx3":
        return "wav"
    return "mp3"


def _apply_env_override(env, override: Optional[str]):
    """Manually override the detected runtime (debugging / forced behavior)."""
    if not override:
        return env
    if override == "local-windows":
        env.runtime = "claude_code_local_windows"
        env.notes.append("runtime overridden via --env-override=local-windows")
    elif override == "sandbox":
        env.runtime = "cowork_sandbox"
        env.notes.append("runtime overridden via --env-override=sandbox")
    elif override == "auto":
        pass
    return env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_tts_providers",
        description=(
            "Phase 8A: probe TTS providers, resolve a Persian voice, "
            "and synthesize one short Persian smoke sentence (only when "
            "provider+network allow). Claude Code/local Windows compatible."
        ),
    )
    parser.add_argument(
        "--run",
        required=True,
        help="Path to a run directory (e.g. runs/001_1_20260520_2128).",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip live network calls (provider catalog lookup, smoke synth).",
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_SMOKE_TEXT,
        help=f"Persian smoke sentence (default: {DEFAULT_SMOKE_TEXT!r}).",
    )
    parser.add_argument(
        "--allow-existing-audio",
        action="store_true",
        help="Do not refuse if non-smoke audio already exists in the run.",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Probe only; do not synthesize the smoke sentence.",
    )
    parser.add_argument(
        "--env-override",
        choices=["auto", "sandbox", "local-windows"],
        default="auto",
        help="Manually override env detection (debug only).",
    )
    args = parser.parse_args(argv)

    # Resolve --run relative to project root if it isn't absolute.
    run_arg = Path(args.run)
    if not run_arg.is_absolute():
        run_arg = PROJECT_ROOT / run_arg

    try:
        run = _ensure_run_dir(run_arg)
        _check_phase7_done(run)
        _check_no_existing_real_audio(run, args.allow_existing_audio)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    run_id = _resolve_run_id(run)
    network = not args.no_network

    print(f"[check_tts_providers] project_root  = {PROJECT_ROOT}")
    print(f"[check_tts_providers] run           = {run}")
    print(f"[check_tts_providers] network probe = {network}")

    # ---- environment detection -----------------------------------------
    env = detect_environment(PROJECT_ROOT)
    env = _apply_env_override(env, args.env_override)
    print(
        f"[check_tts_providers] runtime       = {env.runtime} "
        f"(platform={env.platform}, can_exec_venv={env.can_execute_windows_venv}, "
        f"proxy_403={env.has_proxy_403_signature})"
    )

    # ---- static health check (works without network) -------------------
    health = static_health_check(PROJECT_ROOT)
    print(
        f"[check_tts_providers] static_health = "
        f"{'pass' if health['ok'] else 'fail'} "
        f"({sum(1 for v in health['imports'].values() if v == 'ok')}/"
        f"{len(health['imports'])} modules)"
    )
    if not health["ok"]:
        for e in health["errors"]:
            print(f"  [health] {e}", file=sys.stderr)

    config = load_tts_config(PROJECT_ROOT)
    print(
        f"[check_tts_providers] provider_chain  = {config.get('provider_chain')}"
    )

    outcome = probe_chain(config, project_root=PROJECT_ROOT, network=network)

    if outcome.primary:
        print(
            f"[check_tts_providers] selected primary: {outcome.primary} "
            f"(voice={outcome.selected_voice}, fallback={outcome.fallback_voice})"
        )
    else:
        print(
            "[check_tts_providers] no provider in the chain is available; "
            "smoke synthesis will be skipped.",
            file=sys.stderr,
        )

    # ---- smoke synthesis (only when a provider is genuinely available) -
    smoke_result = None
    smoke_audio_info = None
    smoke_text_used = args.text
    smoke_path = None

    if outcome.primary and not args.skip_smoke and network:
        ext = _provider_ext(outcome.primary)
        smoke_path = run / "audio" / "_smoke" / SMOKE_FILE_NAME_TEMPLATE.format(ext=ext)
        print(f"[check_tts_providers] running smoke synthesis -> {smoke_path}")
        smoke_result = smoke_synthesize(
            config=config,
            provider_name=outcome.primary,
            voice=outcome.selected_voice,
            out_path=smoke_path,
            text=smoke_text_used,
        )
        if smoke_result.success and smoke_result.out_path is not None:
            smoke_audio_info = inspect_audio(smoke_result.out_path)
            dur = smoke_audio_info.get("duration_seconds")
            print(
                f"[check_tts_providers] smoke OK: "
                f"{smoke_result.audio_bytes:,} bytes, duration={dur}s"
            )
        else:
            # No real audio produced - make absolutely sure no placeholder
            # file is left behind.
            try:
                if smoke_path and smoke_path.exists() and smoke_path.stat().st_size == 0:
                    smoke_path.unlink()
            except OSError:
                pass
            print(
                f"[check_tts_providers] smoke FAILED: {smoke_result.error}",
                file=sys.stderr,
            )
    elif outcome.primary and args.skip_smoke:
        print("[check_tts_providers] --skip-smoke set; no synthesis performed.")
    elif outcome.primary and not network:
        print("[check_tts_providers] --no-network set; smoke synthesis skipped.")

    # ---- verdict -------------------------------------------------------
    verdict = decide_verdict(
        env=env,
        primary_provider=outcome.primary,
        smoke_ok=bool(smoke_result and smoke_result.success),
        smoke_duration=(smoke_audio_info or {}).get("duration_seconds"),
    )
    print(f"[check_tts_providers] verdict       = {verdict}")

    # ---- reports -------------------------------------------------------
    qa_dir = run / "QA_REPORTS"
    qa_dir.mkdir(parents=True, exist_ok=True)
    md_path = qa_dir / "tts_provider_check_report.md"
    json_path = qa_dir / "tts_provider_check_report.json"

    write_provider_check_report(
        report_path=md_path,
        outcome=outcome,
        smoke=smoke_result,
        smoke_audio_info=smoke_audio_info,
        smoke_text=smoke_text_used,
        network=network,
        run_id=run_id,
        config=config,
        env_info=env,
        verdict=verdict,
        static_health=health,
    )
    write_provider_check_json(
        json_path=json_path,
        outcome=outcome,
        smoke=smoke_result,
        smoke_audio_info=smoke_audio_info,
        smoke_text=smoke_text_used,
        network=network,
        run_id=run_id,
        config=config,
        env_info=env,
        verdict=verdict,
        static_health=health,
    )
    print(f"[check_tts_providers] wrote {md_path}")
    print(f"[check_tts_providers] wrote {json_path}")

    # ---- summary -------------------------------------------------------
    print()
    print("=== Phase 8A summary ===")
    print(f"runtime          : {env.runtime}")
    print(f"verdict          : {verdict}")
    print(f"primary_provider : {outcome.primary or 'NONE'}")
    print(f"selected_voice   : {outcome.selected_voice}")
    print(f"fallback_voice   : {outcome.fallback_voice}")
    avail = [n for n, p in outcome.probes.items() if p.available]
    print(f"available        : {avail or '[]'}")
    importable_only = [
        n for n, p in outcome.probes.items() if p.importable and not p.available
    ]
    if importable_only:
        print(f"importable_only  : {importable_only}")
    missing = [n for n, p in outcome.probes.items() if not p.importable]
    if missing:
        print(f"not_importable   : {missing}")
    if smoke_result is not None:
        print(
            f"smoke_synthesis  : "
            f"{'success' if smoke_result.success else 'FAILED'} "
            f"({smoke_result.audio_bytes:,} bytes)"
        )
        if smoke_audio_info and smoke_audio_info.get("duration_seconds"):
            print(
                f"smoke_duration   : {smoke_audio_info['duration_seconds']} s "
                f"(bitrate {smoke_audio_info.get('bitrate_kbps')} kbps, "
                f"rate {smoke_audio_info.get('sample_rate_hz')} Hz)"
            )
    else:
        print("smoke_synthesis  : skipped")
    print()
    if verdict == "pending_local_claude_code_validation":
        print(
            "Verdict: PENDING_LOCAL_CLAUDE_CODE_VALIDATION. "
            "Code is healthy; re-run on local Windows to close Phase 8A."
        )
    print(
        "Stopped after Phase 8A. Do NOT proceed to Phase 8B (per-slide audio) "
        "without an explicit prompt."
    )

    if verdict == "pass":
        return 0
    if verdict == "fail":
        return 1
    if verdict == "pending_local_claude_code_validation":
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
