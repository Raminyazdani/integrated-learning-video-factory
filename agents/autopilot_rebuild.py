"""autopilot_rebuild - Phase 11B rebuild driver.

Lives next to ``agents/autopilot.py`` (not folded into it) to avoid the
recurring mount-truncation issue with a single large file. The CLI in
``scripts/run_pipeline.py`` imports ``run_rebuild`` from this module.

The five layer commands are:
  rebuild-pdfs       -> python scripts/build_package_pdf.py --all
  rebuild-slides     -> python scripts/build_teaching_slides.py --all --merge
  rebuild-narration  -> python scripts/generate_narration.py --all
  rebuild-audio      -> python scripts/generate_audio.py --run runs/<id> --all --force
  rebuild-video      -> python scripts/build_video.py --run runs/<id> --all --concat-final --force

Rebuild semantics
-----------------
* Only the requested layer is invoked. Upstream stages are NOT re-run.
* Idempotent dependents (e.g. cost ledger entry) are always updated.
* Audio/video rebuild stay PENDING when the runtime is unreachable
  (same pattern as Phase 8/9 scripts).
* The auto_fixer is consulted exactly once when a layer exits non-zero
  (and not exit 3 / pending); if a fix is applied, the layer is retried
  exactly once.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.auto_fixer import AutoFixResult, try_auto_fix
from agents.autopilot import (
    find_latest_run,
    resolve_input_file,
    write_execution_report,
)
from agents.autopilot import (
    ConvertResult,
    StageExecutionResult,
    _build_step_argv,
    _classify_exit,
    _tail,
)
from agents.qa_critic import run_final_qa
from integrations.atomic_io import atomic_write_bytes, iso_now
from integrations.cost_tracker import record_stage


# (layer, argv_template, units_kind, units_estimator_name)
# units_estimator_name picks a helper below at run time.
REBUILD_LAYERS: Dict[str, Tuple[str, List[str], str, str]] = {
    "pdfs": (
        "rebuild_pdfs",
        ["scripts/build_package_pdf.py", "--all"],
        "pdf_pages",
        "count_pdf_pages",
    ),
    "slides": (
        "rebuild_slides",
        ["scripts/build_teaching_slides.py", "--all", "--merge"],
        "slide_count",
        "count_slide_images",
    ),
    "narration": (
        "rebuild_narration",
        ["scripts/generate_narration.py", "--all"],
        "tts_chars",
        "count_narration_chars",
    ),
    "audio": (
        "rebuild_audio",
        ["scripts/generate_audio.py", "--run", "runs/{run_id}", "--all", "--force"],
        "tts_chars",
        "count_audio_chars",
    ),
    "video": (
        "rebuild_video",
        ["scripts/build_video.py", "--run", "runs/{run_id}", "--all", "--concat-final", "--force"],
        "video_seconds",
        "count_video_seconds",
    ),
}


_STAGE_TIMEOUT: Dict[str, int] = {
    "rebuild_pdfs":      60 * 30,
    "rebuild_slides":    60 * 30,
    "rebuild_narration": 60 * 15,
    "rebuild_audio":     60 * 60,
    "rebuild_video":     60 * 60,
}


# ---------------------------------------------------------------------------
# Unit-estimators (read-only)
# ---------------------------------------------------------------------------


def _count_pdf_pages(run_dir: Path) -> float:
    # Best-effort: number of package PDFs * an average page count from manifests.
    pdf_dir = run_dir / "package_pdfs"
    return float(len(list(pdf_dir.glob("package_*_extended_fa.pdf")))) if pdf_dir.is_dir() else 0.0


def _count_slide_images(run_dir: Path) -> float:
    slide_dir = run_dir / "slide_images"
    return float(len(list(slide_dir.glob("*.png")))) if slide_dir.is_dir() else 0.0


def _count_narration_chars(run_dir: Path) -> float:
    import json
    full = run_dir / "narration_scripts" / "full_narration.json"
    if not full.is_file():
        return 0.0
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    n = 0
    for pkg in data.get("packages", []):
        for s in pkg.get("slides", []):
            n += len(s.get("narration_fa") or "")
    return float(n)


def _count_audio_chars(run_dir: Path) -> float:
    # Use the narration char count as a proxy for what audio rebuild would synthesize.
    return _count_narration_chars(run_dir)


def _count_video_seconds(run_dir: Path) -> float:
    # If video_manifest exists, sum per-package durations.
    import json
    p = run_dir / "video_segments" / "video_manifest.json"
    if not p.is_file():
        return 0.0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    total = 0.0
    for block in (data.get("packages") or {}).values():
        v = block.get("package_video_duration_seconds")
        if isinstance(v, (int, float)):
            total += float(v)
    return total


_ESTIMATORS = {
    "count_pdf_pages":       _count_pdf_pages,
    "count_slide_images":    _count_slide_images,
    "count_narration_chars": _count_narration_chars,
    "count_audio_chars":     _count_audio_chars,
    "count_video_seconds":   _count_video_seconds,
}


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------


def _exec(
    *,
    project_root: Path, run_id: str, stage_id: str,
    argv_template: List[str], timeout: int,
) -> StageExecutionResult:
    argv = _build_step_argv(argv_template, project_root=project_root, run_id=run_id)
    res = StageExecutionResult(stage_id=stage_id, title=stage_id, command=argv)
    res.started_at = iso_now()
    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), env=os.environ.copy(),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        res.wall_seconds = round(time.time() - started, 3)
        res.finished_at = iso_now()
        res.error = f"timeout after {timeout}s: {exc!r}"
        res.verdict = "fail"
        return res
    except Exception as exc:  # noqa: BLE001
        res.wall_seconds = round(time.time() - started, 3)
        res.finished_at = iso_now()
        res.error = f"subprocess failed: {exc!r}"
        res.verdict = "fail"
        return res
    res.wall_seconds = round(time.time() - started, 3)
    res.finished_at = iso_now()
    res.exit_code = proc.returncode
    res.stdout_tail = _tail(proc.stdout, 1200)
    res.stderr_tail = _tail(proc.stderr, 1200)
    res.verdict = _classify_exit(proc.returncode)
    return res


def run_rebuild(
    *,
    project_root: Path,
    file_arg: str,
    layer: str,
    env_info=None,
) -> ConvertResult:
    """Rebuild one layer. Returns a ConvertResult so the CLI can re-use the
    same execution report writer + exit-code conventions as `convert`/`resume`.
    """
    project_root = Path(project_root).resolve()
    if layer not in REBUILD_LAYERS:
        raise ValueError(
            f"unknown rebuild layer {layer!r}; expected one of "
            f"{sorted(REBUILD_LAYERS.keys())}"
        )

    res = ConvertResult(file_arg=file_arg)
    try:
        res.input_file = resolve_input_file(project_root, file_arg)
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
        res.overall_verdict = "fail"
        res.overall_reason = str(exc)
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=None, result=res, env_info=env_info,
        )
        return res

    res.run_dir = find_latest_run(project_root, res.input_file)
    if res.run_dir is None:
        res.overall_verdict = "fail"
        res.overall_reason = "no run directory exists for this input"
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=None, result=res, env_info=env_info,
        )
        return res
    res.run_id = res.run_dir.name

    qa_before = run_final_qa(project_root=project_root, run_dir=res.run_dir, env_info=env_info)
    res.qa_verdict_before = qa_before.verdict
    res.qa_reason = qa_before.reason

    stage_id, argv_template, units_kind, est_name = REBUILD_LAYERS[layer]
    timeout = _STAGE_TIMEOUT.get(stage_id, 600)

    step = _exec(
        project_root=project_root, run_id=res.run_id,
        stage_id=stage_id, argv_template=argv_template, timeout=timeout,
    )
    res.steps.append(step)

    # One-shot auto-fix when the layer truly fails (not just PENDING).
    if step.verdict == "fail":
        fix = try_auto_fix(
            stage_id=stage_id,
            error_text=(step.stderr_tail or step.stdout_tail or step.error or ""),
            run_dir=res.run_dir, project_root=project_root,
        )
        if fix is not None and fix.applied:
            # Single retry after a fix
            step2 = _exec(
                project_root=project_root, run_id=res.run_id,
                stage_id=stage_id + ":retry_after_fix",
                argv_template=argv_template, timeout=timeout,
            )
            step2.skip_reason = (
                f"retry after auto_fixer applied "
                f"{fix.fixer_name} ({len(fix.files_removed)} files removed, "
                f"{len(fix.files_rewritten)} files rewritten)"
            )
            res.steps.append(step2)
            if step2.verdict == "pass":
                step = step2

    # Decide overall verdict.
    if step.verdict == "pass":
        res.overall_verdict = "pass"
    elif step.verdict == "pending_runtime_artifacts":
        res.overall_verdict = "pending_runtime_artifacts"
        res.overall_reason = (
            f"layer `{layer}` is PENDING_RUNTIME_ARTIFACTS - runtime "
            "(network / ffmpeg / etc.) unavailable in this environment."
        )
        res.aborted_at = stage_id
    else:
        res.overall_verdict = "fail"
        res.overall_reason = (
            f"layer `{layer}` failed with exit code {step.exit_code}; "
            "see step's stderr tail."
        )
        res.aborted_at = stage_id

    # Cost tracking - always record, even on fail / pending, with the units
    # estimator we have.
    units_fn = _ESTIMATORS.get(est_name)
    units = float(units_fn(res.run_dir)) if units_fn else 0.0
    provider = None
    if units_kind == "tts_chars":
        provider = "edge"  # MVP chain default
    notes = []
    if step.verdict == "pending_runtime_artifacts":
        notes.append("PENDING_RUNTIME_ARTIFACTS - units count is the *target* size")
    record_stage(
        project_root=project_root, run_id=res.run_id,
        stage_id=stage_id,
        wall_seconds=step.wall_seconds,
        units_kind=units_kind, units=units, provider=provider, notes=notes,
    )

    # Refresh QA + write the same execution report shape `convert`/`resume` use.
    qa_after = run_final_qa(project_root=project_root, run_dir=res.run_dir, env_info=env_info)
    res.qa_verdict_after = qa_after.verdict

    res.report_path = write_execution_report(
        project_root=project_root, run_dir=res.run_dir, result=res, env_info=env_info,
    )
    return res
