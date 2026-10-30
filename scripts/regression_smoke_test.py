"""regression_smoke_test - Phase 12 V1 regression / sanity check.

Three independent layers, each surfaced by its own ``--`` flag (and all run
by default):

  1. ``--static``           - import every project module + py_compile every
                              .py under agents/ integrations/ scripts/.
                              Fails on syntax / import errors.
  2. ``--artifact-exists``  - check Phase 0..7B artifact files (the
                              text/PDF/slide/narration deliverables) for the
                              latest run. Same shape as qa_critic but
                              cheaper - no qa report rewrite.
  3. ``--runtime``          - optional. If audio/video files exist for the
                              latest run, validate that each is non-empty
                              and (for audio) duration > 1 s via mutagen.
                              Skipped when no audio/video is present.

Verdict mapping
---------------
    pass     -> every selected layer passed
    fail     -> any static/artifact check failed
    pending  -> static + artifact pass, runtime check skipped because no
                audio/video files exist yet (typical sandbox state)

Exit codes match the rest of the CLI: 0/1/2/3.

Usage
-----
    python scripts/regression_smoke_test.py
    python scripts/regression_smoke_test.py --static
    python scripts/regression_smoke_test.py --runtime --run runs/001_1_20260520_2128
"""
from __future__ import annotations

import argparse
import importlib
import json
import py_compile
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Static layer
# ---------------------------------------------------------------------------


# Modules we expect to exist + import cleanly.
CORE_MODULES = [
    "integrations.atomic_io",
    "integrations.text_cleaning",
    "integrations.pdf_extractor",
    "integrations.pptx_extractor",
    "integrations.pptx_to_images",
    "integrations.extraction_adapter",
    "integrations.tts_cleaning",
    "integrations.tts_providers",
    "integrations.tts_providers.edge",
    "integrations.tts_providers.gtts_provider",
    "integrations.tts_providers.pyttsx3_provider",
    "integrations.tts_providers.registry",
    "integrations.ffmpeg_builder",
    "integrations.ffmpeg_concat",
    "integrations.env_loader",
    "integrations.env_detector",
    "integrations.artifact_manifest",
    "integrations.cost_tracker",
    "agents.input_intake",
    "agents.extraction_agent",
    "agents.orchestrator",
    "agents.narration_writer_agent",
    "agents.tts_audio_agent",
    "agents.video_builder_agent",
    "agents.qa_critic",
    "agents.autopilot",
    "agents.autopilot_rebuild",
    "agents.auto_fixer",
]


@dataclass
class StaticResult:
    ok: bool = True
    imports_ok: int = 0
    imports_failed: List[str] = field(default_factory=list)
    py_compile_failed: List[str] = field(default_factory=list)


def run_static() -> StaticResult:
    res = StaticResult()
    for m in CORE_MODULES:
        try:
            importlib.import_module(m)
            res.imports_ok += 1
        except Exception as exc:  # noqa: BLE001
            res.ok = False
            res.imports_failed.append(f"{m}: {exc!r}")

    # py_compile every .py under three dirs (skip __pycache__).
    for sub in ("agents", "integrations", "scripts"):
        d = PROJECT_ROOT / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                py_compile.compile(str(p), doraise=True)
            except Exception as exc:  # noqa: BLE001
                res.ok = False
                res.py_compile_failed.append(f"{p}: {exc!r}")
    return res


# ---------------------------------------------------------------------------
# Artifact-existence layer
# ---------------------------------------------------------------------------


@dataclass
class ArtifactResult:
    ok: bool = True
    checks: Dict[str, str] = field(default_factory=dict)   # name -> 'pass'|'fail'|'pending'
    notes: List[str] = field(default_factory=list)
    run_dir: Optional[Path] = None


def _find_latest_run(project_root: Path) -> Optional[Path]:
    runs = project_root / "runs"
    if not runs.is_dir():
        return None
    cands = sorted([p for p in runs.iterdir() if p.is_dir() and p.name[:3].isdigit()])
    return cands[-1] if cands else None


def run_artifact_exists(run_dir_arg: Optional[Path] = None) -> ArtifactResult:
    res = ArtifactResult()
    rd = run_dir_arg or _find_latest_run(PROJECT_ROOT)
    if rd is None:
        res.ok = False
        res.notes.append("no run directory found under runs/")
        return res
    res.run_dir = rd

    # Phase 2 - extraction
    res.checks["phase_2_pages_png"] = "pass" if list((rd / "extracted" / "pages").glob("page_*.png")) else "fail"
    res.checks["phase_2_pages_txt"] = "pass" if list((rd / "extracted" / "page_text").glob("page_*.txt")) else "fail"
    res.checks["phase_2_manifest"] = "pass" if (rd / "extracted" / "extraction_manifest.json").is_file() else "fail"

    # Phase 3 - packaging
    res.checks["phase_3_package_manifest"] = "pass" if (rd / "packages" / "package_manifest.json").is_file() else "fail"

    # Phase 4 - content (12 packages md + json)
    mds = list((rd / "content_md").glob("package_*_extended_fa.md"))
    jsons = list((rd / "content_json").glob("package_*_extended_fa.json"))
    res.checks["phase_4_content_md_count_12"] = "pass" if len(mds) >= 12 else "fail"
    res.checks["phase_4_content_json_count_12"] = "pass" if len(jsons) >= 12 else "fail"

    # Phase 5 - PDFs (12 + optional ZIP)
    pdfs = list((rd / "package_pdfs").glob("package_*_extended_fa.pdf"))
    res.checks["phase_5_package_pdfs_count_12"] = "pass" if len(pdfs) >= 12 else "fail"
    res.checks["phase_5_output_zip"] = (
        "pass" if (PROJECT_ROOT / "output" / "001_package_pdfs.zip").is_file() else "fail"
    )

    # Phase 6 - slides + slide images
    res.checks["phase_6_full_deck_pptx"] = "pass" if (rd / "slides" / "full_teaching_deck.pptx").is_file() else "fail"
    res.checks["phase_6_full_deck_pdf"] = "pass" if (rd / "slides" / "full_teaching_deck.pdf").is_file() else "fail"
    res.checks["phase_6_slide_manifest"] = "pass" if (rd / "slides" / "slide_manifest.json").is_file() else "fail"
    sl_imgs = list((rd / "slide_images").glob("full_teaching_deck-*.png"))
    res.checks["phase_6_slide_images_>=254"] = "pass" if len(sl_imgs) >= 254 else "fail"

    # Phase 7 - narration text
    res.checks["phase_7_full_narration"] = "pass" if (rd / "narration_scripts" / "full_narration.json").is_file() else "fail"
    res.checks["phase_7_per_pkg_count_12"] = (
        "pass" if len(list((rd / "narration_scripts").glob("package_*.json"))) >= 12 else "fail"
    )

    # Phase 8/9 - audio/video are runtime; mark pending if absent
    audio_dir = rd / "audio"
    video_dir = rd / "video_segments"
    res.checks["phase_8_audio_dir_or_pending"] = (
        "pass" if audio_dir.is_dir() and any(audio_dir.glob("package_*/slide_*.mp3"))
        else "pending"
    )
    res.checks["phase_9_video_dir_or_pending"] = (
        "pass" if video_dir.is_dir() and any(video_dir.glob("package_*.mp4"))
        else "pending"
    )
    final_video = PROJECT_ROOT / "output" / "001_final_educational_video.mp4"
    res.checks["final_video_or_pending"] = "pass" if final_video.is_file() else "pending"

    res.ok = not any(v == "fail" for v in res.checks.values())
    return res


# ---------------------------------------------------------------------------
# Runtime layer (optional)
# ---------------------------------------------------------------------------


@dataclass
class RuntimeResult:
    ok: bool = True
    skipped: bool = False
    audio_checked: int = 0
    audio_failed: List[str] = field(default_factory=list)
    video_checked: int = 0
    video_failed: List[str] = field(default_factory=list)


def run_runtime(run_dir_arg: Optional[Path] = None) -> RuntimeResult:
    res = RuntimeResult()
    rd = run_dir_arg or _find_latest_run(PROJECT_ROOT)
    if rd is None:
        res.skipped = True
        return res

    audio_files = list((rd / "audio").glob("package_*/slide_*.mp3")) if (rd / "audio").is_dir() else []
    video_files = list((rd / "video_segments").glob("package_*.mp4")) if (rd / "video_segments").is_dir() else []
    if not audio_files and not video_files:
        res.skipped = True
        return res

    # Audio: size > 0 AND (if mutagen available) duration > 1 s
    try:
        from mutagen.mp3 import MP3
        have_mutagen = True
    except Exception:
        have_mutagen = False
    for a in audio_files:
        res.audio_checked += 1
        if a.stat().st_size == 0:
            res.audio_failed.append(f"{a}: empty")
            continue
        if have_mutagen:
            try:
                m = MP3(str(a))
                if m.info.length <= 1.0:
                    res.audio_failed.append(f"{a}: duration={m.info.length}")
            except Exception as exc:  # noqa: BLE001
                res.audio_failed.append(f"{a}: mutagen error {exc!r}")

    # Video: size > 0 + ffprobe duration > 1 s if available
    try:
        from integrations.ffmpeg_builder import probe_duration_seconds
    except Exception:
        probe_duration_seconds = None  # type: ignore
    for v in video_files:
        res.video_checked += 1
        if v.stat().st_size == 0:
            res.video_failed.append(f"{v}: empty")
            continue
        if probe_duration_seconds is not None:
            dur = probe_duration_seconds(v)
            if dur is None or dur <= 1.0:
                res.video_failed.append(f"{v}: ffprobe dur={dur}")

    res.ok = not (res.audio_failed or res.video_failed)
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="regression_smoke_test",
        description=(
            "V1 regression smoke test. Three independent layers; by default "
            "all three run. Audio/video runtime checks auto-skip when those "
            "artifacts do not exist (e.g., in Cowork sandbox)."
        ),
    )
    parser.add_argument("--static", action="store_true", help="Run static layer only.")
    parser.add_argument("--artifact-exists", dest="artifact", action="store_true",
                        help="Run artifact-existence layer only.")
    parser.add_argument("--runtime", action="store_true",
                        help="Run runtime audio/video layer only.")
    parser.add_argument("--run", default=None,
                        help="Path to a run directory (defaults to latest under runs/).")
    args = parser.parse_args(argv)

    selected = []
    if args.static or args.artifact or args.runtime:
        if args.static:    selected.append("static")
        if args.artifact:  selected.append("artifact")
        if args.runtime:   selected.append("runtime")
    else:
        selected = ["static", "artifact", "runtime"]

    rd = Path(args.run).resolve() if args.run else None

    static = run_static() if "static" in selected else None
    artifact = run_artifact_exists(rd) if "artifact" in selected else None
    runtime = run_runtime(rd) if "runtime" in selected else None

    # Decide verdict
    fails = []
    pending = False
    if static is not None and not static.ok:
        fails.append("static")
    if artifact is not None and not artifact.ok:
        fails.append("artifact")
    if runtime is not None:
        if runtime.skipped:
            pending = True
        elif not runtime.ok:
            fails.append("runtime")

    print("=== Regression smoke test - V1 ===")
    if static is not None:
        print(f"[static]    imports_ok={static.imports_ok}/{len(CORE_MODULES)}  "
              f"failed_imports={len(static.imports_failed)}  "
              f"py_compile_failed={len(static.py_compile_failed)}  "
              f"-> {'PASS' if static.ok else 'FAIL'}")
        for f in static.imports_failed[:5]:
            print(f"  import_fail: {f}")
        for f in static.py_compile_failed[:5]:
            print(f"  compile_fail: {f}")
    if artifact is not None:
        n_pass = sum(1 for v in artifact.checks.values() if v == "pass")
        n_fail = sum(1 for v in artifact.checks.values() if v == "fail")
        n_pend = sum(1 for v in artifact.checks.values() if v == "pending")
        print(f"[artifact]  run_dir={artifact.run_dir}  pass={n_pass} fail={n_fail} pending={n_pend} "
              f"-> {'PASS' if artifact.ok else 'FAIL'}")
        for name, st in artifact.checks.items():
            print(f"  {name:36s} {st.upper()}")
    if runtime is not None:
        if runtime.skipped:
            print(f"[runtime]   skipped (no audio/video found) -> PENDING_RUNTIME_ARTIFACTS")
        else:
            print(f"[runtime]   audio={runtime.audio_checked} "
                  f"audio_fail={len(runtime.audio_failed)} "
                  f"video={runtime.video_checked} "
                  f"video_fail={len(runtime.video_failed)} "
                  f"-> {'PASS' if runtime.ok else 'FAIL'}")
            for f in runtime.audio_failed[:5]:
                print(f"  audio: {f}")
            for f in runtime.video_failed[:5]:
                print(f"  video: {f}")

    print()
    if fails:
        print(f"VERDICT: FAIL  ({', '.join(fails)})")
        return 1
    if pending:
        print("VERDICT: PENDING_RUNTIME_ARTIFACTS  "
              "(static + artifact layers pass; audio/video not yet produced)")
        return 3
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
