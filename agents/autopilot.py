"""autopilot - Phase 9E (dry-run) + Phase 9F (real convert).

Phase 9E: ``run_convert_dry_run`` - inspect-only, never invokes synthesizers.

Phase 9F: ``run_convert`` - the real pipeline driver. Walks an *execution*
plan (smaller than the 13-row inspection plan) that runs the missing
runtime stages in order via ``subprocess``:

    stage_7a_tts_check   ->  scripts/check_tts_providers.py
    stage_7b_audio_all   ->  scripts/generate_audio.py --all
    stage_8_video_full   ->  scripts/build_video.py --all --concat-final
    stage_9_final_qa     ->  scripts/run_final_qa.py

Each step is run with the current Python interpreter from ``PROJECT_ROOT``
as cwd, with stdout/stderr captured. Exit-code policy:
    0    -> step passed; continue.
    3    -> PENDING_RUNTIME_ARTIFACTS -> stop, report, point user to Windows.
    any  -> FAIL -> stop, report; no further steps attempted.

Hard rules:
* Phases 0-7B are NEVER re-run by this driver. If `qa_critic` reports any
  of them as `fail`, the autopilot refuses to continue.
* No fake audio/video are created (each stage script enforces this).
* Source projects are never touched.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.qa_critic import run_final_qa
from integrations.artifact_manifest import PhaseCheck, scan_all_artifacts
from integrations.atomic_io import atomic_write_bytes, iso_now


# ---------------------------------------------------------------------------
# Inspection plan (Phase 9E)
# ---------------------------------------------------------------------------


@dataclass
class StagePlanEntry:
    stage_id: str
    title: str
    status: str
    action: str
    command: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class DryRunResult:
    file_arg: str
    input_file: Optional[Path] = None
    run_dir: Optional[Path] = None
    run_id: Optional[str] = None
    qa_verdict: str = "unknown"
    qa_reason: Optional[str] = None
    plan: List[StagePlanEntry] = field(default_factory=list)
    report_path: Optional[Path] = None
    next_recommended_action: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Input + run resolution (shared by dry-run and real run)
# ---------------------------------------------------------------------------


def resolve_input_file(project_root: Path, file_arg: str) -> Path:
    project_root = Path(project_root).resolve()
    arg = file_arg.strip()
    if not arg:
        raise ValueError("file argument is empty")
    candidates: List[Path] = []
    p = Path(arg)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(project_root / "input" / arg)
        if not arg.lower().endswith(".pdf") and not arg.lower().endswith(".pptx"):
            candidates.append(project_root / "input" / f"{arg}.pdf")
            candidates.append(project_root / "input" / f"{arg}.pptx")
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        f"input file not found for {arg!r}; tried: "
        + ", ".join(str(c) for c in candidates)
    )


_RUN_PATTERN = re.compile(r"^(?P<num>\d{3})_(?P<file>[^_]+)_(?P<ts>\d{8}_\d{4})$")


def find_latest_run(project_root: Path, input_file: Optional[Path] = None) -> Optional[Path]:
    project_root = Path(project_root).resolve()
    runs_root = project_root / "runs"
    if not runs_root.is_dir():
        return None
    file_token = (input_file.stem if input_file is not None else None)
    candidates: List[Path] = []
    for r in runs_root.iterdir():
        if not r.is_dir():
            continue
        m = _RUN_PATTERN.match(r.name)
        if not m:
            continue
        if file_token and m.group("file") != file_token:
            continue
        candidates.append(r)
    if not candidates:
        # A specific input file was requested but has no run yet: do NOT fall
        # back to an unrelated run. Binding e.g. file 2 to file 1's run would
        # mis-plan the convert and overwrite file 1's reports. Return None so
        # the caller allocates a fresh run via stage_0 intake.
        if file_token is not None:
            return None
        for r in runs_root.iterdir():
            if r.is_dir() and _RUN_PATTERN.match(r.name):
                candidates.append(r)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


# ---------------------------------------------------------------------------
# Inspection stage table (13 rows, for the dry-run report)
# ---------------------------------------------------------------------------


STAGE_TEMPLATE: List[Tuple[str, str, str, Optional[str]]] = [
    ("stage_0_intake",       "Stage 0 - input intake + run allocation",     "",                     "python scripts/convert_file.py {file_arg}"),
    ("stage_1_extraction",   "Stage 1 - PDF extraction",                    "phase_2_extraction",   None),
    ("stage_2_classify_pkg", "Stage 2 - page classification + packaging",   "phase_3_packaging",    None),
    ("stage_3_content",      "Stage 3 - Persian extended content",          "phase_4_content",      "python scripts/generate_package_content.py"),
    ("stage_4_pdfs",         "Stage 4 - package PDFs",                      "phase_5_pdfs",         "python scripts/build_package_pdf.py --all"),
    ("stage_5_slides",       "Stage 5 - teaching slides + slide images",    "phase_6_slides",       "python scripts/build_teaching_slides.py --all --merge"),
    ("stage_6_narration",    "Stage 6 - per-slide narration text",          "phase_7_narration",    "python scripts/generate_narration.py --all"),
    ("stage_7a_tts_check",   "Stage 7A - TTS provider check",               "",                     "python scripts/check_tts_providers.py --run runs/{run_id}"),
    ("stage_7b_audio",       "Stage 7B - per-slide audio (Phase 8B/8C)",    "phase_8_audio",        "python scripts/generate_audio.py --run runs/{run_id} --all"),
    ("stage_8a_smoke_video", "Stage 8A - ffmpeg smoke test",                "",                     "python scripts/build_video.py --run runs/{run_id} --smoke-test"),
    ("stage_8b_pkg_video",   "Stage 8B - per-package video",                "phase_9_video",        "python scripts/build_video.py --run runs/{run_id} --all"),
    ("stage_8c_final_video", "Stage 8C - final concatenated video",         "final_video",          "python scripts/build_video.py --run runs/{run_id} --all --concat-final"),
    ("stage_9_final_qa",     "Stage 9 - final QA + artifact manifest",      "",                     "python scripts/run_final_qa.py --run runs/{run_id}"),
]


def _windows_command(template: str, *, run_id: str, file_arg: str) -> str:
    cmd = template.format(run_id=run_id, file_arg=file_arg)
    cmd = cmd.replace("python ", "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe ")
    cmd = cmd.replace("runs/", "runs\\")
    cmd = cmd.replace("scripts/", "scripts\\")
    return cmd


def _phase_status_for(stage_qa_key: str, checks: Dict[str, PhaseCheck]) -> str:
    if not stage_qa_key:
        return "not_scanned"
    c = checks.get(stage_qa_key)
    if c is None:
        return "not_scanned"
    return c.status


def _decide_action(stage_status: str, command: Optional[str]) -> str:
    if stage_status == "pass":
        return "skip"
    if stage_status == "pending_runtime_artifacts":
        return "manual_local_windows"
    if stage_status == "fail":
        return "blocked"
    if stage_status == "not_scanned":
        return "run" if command else "skip"
    return "skip"


def build_plan(
    *,
    project_root: Path,
    run_dir: Optional[Path],
    file_arg: str,
    checks: Dict[str, PhaseCheck],
) -> List[StagePlanEntry]:
    plan: List[StagePlanEntry] = []
    run_id = run_dir.name if run_dir else "(none)"
    for stage_id, title, qa_key, template in STAGE_TEMPLATE:
        status = _phase_status_for(qa_key, checks)
        action = _decide_action(status, template)
        cmd = _windows_command(template, run_id=run_id, file_arg=file_arg) if template else None
        entry = StagePlanEntry(stage_id=stage_id, title=title, status=status, action=action, command=cmd)
        c = checks.get(qa_key)
        if c is not None and c.notes:
            entry.notes.extend(c.notes[:3])
        if c is not None and c.error:
            entry.notes.append(f"error: {c.error}")
        plan.append(entry)
    return plan


def _next_recommended_action(plan: List[StagePlanEntry]) -> str:
    for entry in plan:
        if entry.action == "skip":
            continue
        if entry.action == "manual_local_windows":
            return (
                f"Run on local Windows: `{entry.command}` "
                f"(closes the PENDING_RUNTIME_ARTIFACTS status of `{entry.stage_id}`)"
            )
        if entry.action == "blocked":
            return f"`{entry.stage_id}` is failing; triage before continuing."
        if entry.action == "run":
            return f"Run: `{entry.command}`"
    return "Nothing left to do - every stage already shows PASS."


def run_convert_dry_run(*, project_root: Path, file_arg: str, env_info=None) -> DryRunResult:
    project_root = Path(project_root).resolve()
    res = DryRunResult(file_arg=file_arg)
    try:
        res.input_file = resolve_input_file(project_root, file_arg)
    except (FileNotFoundError, ValueError) as exc:
        res.error = str(exc)

    res.run_dir = find_latest_run(project_root, res.input_file)
    if res.run_dir is not None:
        res.run_id = res.run_dir.name

    checks: Dict[str, PhaseCheck] = {}
    if res.run_dir is not None:
        qa = run_final_qa(project_root=project_root, run_dir=res.run_dir, env_info=env_info)
        checks = qa.checks
        res.qa_verdict = qa.verdict
        res.qa_reason = qa.reason

    res.plan = build_plan(project_root=project_root, run_dir=res.run_dir, file_arg=file_arg, checks=checks)
    res.next_recommended_action = _next_recommended_action(res.plan)
    res.report_path = write_dry_run_report(
        project_root=project_root, run_dir=res.run_dir, result=res, env_info=env_info,
    )
    return res


# ---------------------------------------------------------------------------
# Phase 9F - real execute
# ---------------------------------------------------------------------------


@dataclass
class StageExecutionResult:
    stage_id: str
    title: str
    command: List[str]
    started_at: str = ""
    finished_at: str = ""
    wall_seconds: float = 0.0
    exit_code: Optional[int] = None
    stdout_tail: Optional[str] = None
    stderr_tail: Optional[str] = None
    verdict: str = "skipped"   # "pass" | "fail" | "pending_runtime_artifacts" | "skipped" | "blocked"
    skip_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ConvertResult:
    file_arg: str
    input_file: Optional[Path] = None
    run_dir: Optional[Path] = None
    run_id: Optional[str] = None
    qa_verdict_before: str = "unknown"
    qa_verdict_after: str = "unknown"
    qa_reason: Optional[str] = None
    steps: List[StageExecutionResult] = field(default_factory=list)
    overall_verdict: str = "pending_runtime_artifacts"
    overall_reason: Optional[str] = None
    report_path: Optional[Path] = None
    error: Optional[str] = None
    aborted_at: Optional[str] = None      # stage_id where execution stopped


# Smaller execution pipeline (Phase 9F).
# Each entry: (stage_id, title, argv_template_relative_to_project_root).
# argv items use {run_id} placeholder; lookup is project-root relative for cwd.
EXECUTION_PIPELINE: List[Tuple[str, str, List[str]]] = [
    (
        "stage_7a_tts_check",
        "Phase 8A - TTS provider check (smoke)",
        ["scripts/check_tts_providers.py", "--run", "runs/{run_id}"],
    ),
    (
        "stage_7b_audio_all",
        "Phase 8C - per-slide audio for every package",
        ["scripts/generate_audio.py", "--run", "runs/{run_id}", "--all"],
    ),
    (
        "stage_8_video_full",
        "Phase 9C - per-package video + final concatenation",
        ["scripts/build_video.py", "--run", "runs/{run_id}", "--all", "--concat-final"],
    ),
    (
        "stage_9_final_qa",
        "Phase 9D - final QA + artifact manifest",
        ["scripts/run_final_qa.py", "--run", "runs/{run_id}"],
    ),
]


# Per-script timeouts (seconds). The full --all --concat-final run on real
# audio takes minutes; the dry path in sandbox returns within a few seconds.
_STAGE_TIMEOUT: Dict[str, int] = {
    "stage_7a_tts_check":  300,
    "stage_7b_audio_all":  60 * 60,
    "stage_8_video_full":  60 * 60,
    "stage_9_final_qa":    60,
}


def _classify_exit(code: int) -> str:
    """Map a step's exit code to a verdict word."""
    if code == 0:
        return "pass"
    if code == 3:
        return "pending_runtime_artifacts"
    return "fail"


def _tail(text: str, n: int = 800) -> Optional[str]:
    if text is None:
        return None
    t = text or ""
    return t[-n:] if len(t) > n else t


def _check_phase_0_to_7_clean(checks: Dict[str, PhaseCheck]) -> Optional[str]:
    """Return an error string if any of the text/PDF/slide/narration phases
    is `fail`. PENDING_RUNTIME_ARTIFACTS can only appear in phases 8/9 by
    construction; we ignore those at this gate."""
    earlier = [
        "phase_2_extraction",
        "phase_3_packaging",
        "phase_4_content",
        "phase_5_pdfs",
        "phase_6_slides",
        "phase_7_narration",
    ]
    for key in earlier:
        c = checks.get(key)
        if c is None:
            continue
        if c.status == "fail":
            return f"earlier phase `{key}` is FAIL: {c.error or 'see qa report'}"
    return None


def _build_step_argv(template: List[str], *, project_root: Path, run_id: str) -> List[str]:
    """Resolve {run_id} placeholders and prepend sys.executable + project path."""
    out = [sys.executable, "-B"]
    for token in template:
        out.append(token.format(run_id=run_id))
    return out


def _execute_stage(
    *,
    project_root: Path,
    run_id: str,
    stage_id: str,
    title: str,
    argv_template: List[str],
    env_overrides: Optional[Dict[str, str]] = None,
) -> StageExecutionResult:
    argv = _build_step_argv(argv_template, project_root=project_root, run_id=run_id)
    result = StageExecutionResult(stage_id=stage_id, title=title, command=argv)
    result.started_at = iso_now()
    started = time.time()
    timeout = _STAGE_TIMEOUT.get(stage_id, 600)
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result.wall_seconds = round(time.time() - started, 3)
        result.finished_at = iso_now()
        result.exit_code = None
        result.error = f"timeout after {timeout}s: {exc!r}"
        result.verdict = "fail"
        return result
    except Exception as exc:  # noqa: BLE001
        result.wall_seconds = round(time.time() - started, 3)
        result.finished_at = iso_now()
        result.exit_code = None
        result.error = f"subprocess failed: {exc!r}"
        result.verdict = "fail"
        return result

    result.wall_seconds = round(time.time() - started, 3)
    result.finished_at = iso_now()
    result.exit_code = proc.returncode
    result.stdout_tail = _tail(proc.stdout, 1200)
    result.stderr_tail = _tail(proc.stderr, 1200)
    result.verdict = _classify_exit(proc.returncode)
    return result


def run_convert(*, project_root: Path, file_arg: str, env_info=None) -> ConvertResult:
    """The real Phase 9F driver. Runs only the *missing* runtime stages,
    stops at the first PENDING/FAIL, never fabricates artifacts."""
    project_root = Path(project_root).resolve()
    res = ConvertResult(file_arg=file_arg)

    # Resolve input + run dir
    try:
        res.input_file = resolve_input_file(project_root, file_arg)
    except (FileNotFoundError, ValueError) as exc:
        res.error = str(exc)
        res.overall_verdict = "fail"
        res.overall_reason = str(exc)
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=None, result=res, env_info=env_info,
        )
        return res

    res.run_dir = find_latest_run(project_root, res.input_file)
    if res.run_dir is None:
        res.error = (
            "no run directory exists for this input yet; "
            "run `python scripts/convert_file.py {N}` first to allocate one."
        )
        res.overall_verdict = "fail"
        res.overall_reason = res.error
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=None, result=res, env_info=env_info,
        )
        return res
    res.run_id = res.run_dir.name

    # Quick QA scan first (read-only)
    qa = run_final_qa(project_root=project_root, run_dir=res.run_dir, env_info=env_info)
    res.qa_verdict_before = qa.verdict
    res.qa_reason = qa.reason

    earlier_issue = _check_phase_0_to_7_clean(qa.checks)
    if earlier_issue:
        res.overall_verdict = "fail"
        res.overall_reason = earlier_issue
        res.aborted_at = "pre_run_gate"
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=res.run_dir, result=res, env_info=env_info,
        )
        return res

    # If everything already passes (including runtime), we just confirm.
    if qa.verdict == "pass":
        res.overall_verdict = "pass"
        res.qa_verdict_after = "pass"
        res.report_path = write_execution_report(
            project_root=project_root, run_dir=res.run_dir, result=res, env_info=env_info,
        )
        return res

    # Walk the execution pipeline
    for stage_id, title, argv_template in EXECUTION_PIPELINE:
        # Decide if we can skip: each script is idempotent, so we *could*
        # always invoke. But to save wall time and avoid spurious "smoke"
        # synthesis when audio already exists, we skip stages whose
        # corresponding qa phase already passes.
        skip_reason = _skip_reason_for_stage(stage_id, qa.checks)
        if skip_reason:
            step = StageExecutionResult(
                stage_id=stage_id, title=title, command=[],
                verdict="skipped", skip_reason=skip_reason,
                started_at=iso_now(), finished_at=iso_now(),
            )
            res.steps.append(step)
            continue

        step = _execute_stage(
            project_root=project_root,
            run_id=res.run_id,
            stage_id=stage_id,
            title=title,
            argv_template=argv_template,
        )
        res.steps.append(step)

        if step.verdict == "pass":
            continue
        if step.verdict == "pending_runtime_artifacts":
            res.overall_verdict = "pending_runtime_artifacts"
            res.overall_reason = (
                f"stage `{stage_id}` is PENDING_RUNTIME_ARTIFACTS - "
                "audio/video synthesis requires local Windows."
            )
            res.aborted_at = stage_id
            break
        # fail
        res.overall_verdict = "fail"
        res.overall_reason = (
            f"stage `{stage_id}` failed with exit code {step.exit_code}; "
            "see the step's stderr tail in the execution report."
        )
        res.aborted_at = stage_id
        break
    else:
        res.overall_verdict = "pass"

    # Refresh QA after execution (may be different now if some stages ran).
    qa_after = run_final_qa(project_root=project_root, run_dir=res.run_dir, env_info=env_info)
    res.qa_verdict_after = qa_after.verdict
    if res.overall_verdict == "pass" and qa_after.verdict != "pass":
        # The driver thinks it succeeded but qa disagrees; bow to qa.
        res.overall_verdict = qa_after.verdict
        res.overall_reason = (
            f"all attempted stages reported pass, but qa_critic reports "
            f"`{qa_after.verdict}`: {qa_after.reason}"
        )

    res.report_path = write_execution_report(
        project_root=project_root, run_dir=res.run_dir, result=res, env_info=env_info,
    )
    return res


def _skip_reason_for_stage(stage_id: str, checks: Dict[str, PhaseCheck]) -> Optional[str]:
    """If we can confidently skip a runtime stage (because its outputs are
    already valid per qa), return a reason string. Otherwise return None
    and we'll invoke the script."""
    if stage_id == "stage_7b_audio_all":
        c = checks.get("phase_8_audio")
        if c is not None and c.status == "pass":
            return "phase_8_audio already PASS in qa scan; per-script idempotency makes a re-run a no-op anyway"
    if stage_id == "stage_8_video_full":
        c_video = checks.get("phase_9_video")
        c_final = checks.get("final_video")
        if (c_video and c_video.status == "pass") and (c_final and c_final.status == "pass"):
            return "phase_9_video and final_video already PASS in qa scan"
    return None


# ---------------------------------------------------------------------------
# Phase 11A - resume / status / validate
# ---------------------------------------------------------------------------
#
# - `status N`   -> read-only dry-run that prints + writes a lightweight
#                   status summary (no subprocesses, no qa report rewrite
#                   beyond what qa_critic always does).
# - `validate N` -> calls qa_critic only. Same effect as status's qa half
#                   but framed as "I am verifying, not planning".
# - `resume N`   -> identical semantics to `convert N` (run_convert already
#                   skips PASS stages via _skip_reason_for_stage), with a
#                   "resume" hint in the execution report.


@dataclass
class StatusResult:
    """Lightweight read-only status of a run."""
    file_arg: str
    input_file: Optional[Path] = None
    run_dir: Optional[Path] = None
    run_id: Optional[str] = None
    qa_verdict: str = "unknown"
    qa_reason: Optional[str] = None
    phase_status: Dict[str, str] = field(default_factory=dict)  # phase_id -> status word
    next_action: Optional[str] = None
    report_path: Optional[Path] = None
    error: Optional[str] = None


def run_status(*, project_root: Path, file_arg: str, env_info=None) -> StatusResult:
    """Lightweight read-only status. Never invokes synthesizers."""
    project_root = Path(project_root).resolve()
    out = StatusResult(file_arg=file_arg)
    try:
        out.input_file = resolve_input_file(project_root, file_arg)
    except (FileNotFoundError, ValueError) as exc:
        out.error = str(exc)
    out.run_dir = find_latest_run(project_root, out.input_file)
    if out.run_dir is not None:
        out.run_id = out.run_dir.name

    if out.run_dir is None:
        out.qa_verdict = "no_run"
        out.next_action = (
            "Allocate a run by calling `convert_file.py <N>` first."
        )
        return out

    qa = run_final_qa(project_root=project_root, run_dir=out.run_dir, env_info=env_info)
    out.qa_verdict = qa.verdict
    out.qa_reason = qa.reason
    out.phase_status = {pid: c.status for pid, c in qa.checks.items()}
    # Next-action heuristic via the inspection plan.
    plan = build_plan(
        project_root=project_root, run_dir=out.run_dir,
        file_arg=file_arg, checks=qa.checks,
    )
    out.next_action = _next_recommended_action(plan)
    return out


@dataclass
class ValidateResult:
    """QA-only validation (no execution, no plan)."""
    file_arg: str
    input_file: Optional[Path] = None
    run_dir: Optional[Path] = None
    run_id: Optional[str] = None
    qa_verdict: str = "unknown"
    qa_reason: Optional[str] = None
    phase_status: Dict[str, str] = field(default_factory=dict)
    artifact_manifest_path: Optional[Path] = None
    final_report_path: Optional[Path] = None
    summary_path: Optional[Path] = None
    error: Optional[str] = None


def run_validate(*, project_root: Path, file_arg: str, env_info=None) -> ValidateResult:
    """QA-only refresh. Calls qa_critic; nothing else."""
    project_root = Path(project_root).resolve()
    out = ValidateResult(file_arg=file_arg)
    try:
        out.input_file = resolve_input_file(project_root, file_arg)
    except (FileNotFoundError, ValueError) as exc:
        out.error = str(exc)
    out.run_dir = find_latest_run(project_root, out.input_file)
    if out.run_dir is not None:
        out.run_id = out.run_dir.name

    if out.run_dir is None:
        out.qa_verdict = "no_run"
        return out

    qa = run_final_qa(project_root=project_root, run_dir=out.run_dir, env_info=env_info)
    out.qa_verdict = qa.verdict
    out.qa_reason = qa.reason
    out.phase_status = {pid: c.status for pid, c in qa.checks.items()}
    out.artifact_manifest_path = qa.artifact_manifest_path
    out.final_report_path = qa.final_report_path
    out.summary_path = qa.summary_path
    return out


def run_resume(*, project_root: Path, file_arg: str, env_info=None) -> "ConvertResult":
    """`resume` is functionally identical to `convert N`: the autopilot's
    `run_convert` already skips PASS stages and resumes from the first
    incomplete stage. This wrapper exists so the CLI verb matches user
    intent and the execution report's header carries the resume context.
    """
    res = run_convert(project_root=project_root, file_arg=file_arg, env_info=env_info)
    # Tag the result so the report makes the resume framing visible.
    res.file_arg = res.file_arg  # no-op, kept for clarity
    if res.report_path is None:
        return res
    # Append a one-liner footer to the execution report so future readers
    # know this was invoked as `resume` rather than `convert`.
    try:
        existing = res.report_path.read_text(encoding="utf-8")
        if "_invoked_as_resume_" not in existing:
            existing += "\n\n_invoked_as_resume_: true\n"
            res.report_path.write_text(existing, encoding="utf-8")
    except OSError:
        pass
    return res


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _env_to_dict(env_info) -> Dict[str, Any]:
    if env_info is None:
        return {}
    if is_dataclass(env_info):
        d = asdict(env_info)
    elif isinstance(env_info, dict):
        d = dict(env_info)
    else:
        return {"repr": repr(env_info)}
    pr = d.get("project_root")
    if pr is not None:
        d["project_root"] = str(pr)
    return d


_ACTION_MARK = {
    "skip":                  "SKIP (already pass)",
    "manual_local_windows":  "MANUAL (local Windows)",
    "blocked":               "BLOCKED (triage required)",
    "run":                   "RUN",
}


def write_dry_run_report(
    *,
    project_root: Path,
    run_dir: Optional[Path],
    result: DryRunResult,
    env_info=None,
) -> Path:
    if run_dir is None:
        target = project_root / "QA_REPORTS" / "autopilot_dry_run_report.md"
    else:
        target = run_dir / "QA_REPORTS" / "autopilot_dry_run_report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    env_dict = _env_to_dict(env_info)

    lines: List[str] = []
    lines.append("# Phase 9E - autopilot dry-run report")
    lines.append("")
    lines.append(f"- generated_at: {iso_now()}")
    lines.append(f"- command:   `convert {result.file_arg} --dry-run`")
    lines.append(f"- file_arg:  `{result.file_arg}`")
    lines.append(f"- input:     {('`' + str(result.input_file) + '`') if result.input_file else '**not found**'}")
    lines.append(f"- run_dir:   {('`' + str(result.run_dir) + '`') if result.run_dir else '**no run found**'}")
    if result.run_id:
        lines.append(f"- run_id:    `{result.run_id}`")
    lines.append(f"- qa_verdict: **{result.qa_verdict.upper()}**")
    if result.qa_reason:
        lines.append(f"- qa_reason: {result.qa_reason}")
    if env_dict:
        lines.append(
            f"- runtime: **{env_dict.get('runtime', 'unknown')}** "
            f"(platform={env_dict.get('platform', 'unknown')})"
        )
    if result.error:
        lines.append(f"- error: `{result.error}`")
    lines.append("")

    lines.append("## Hard guarantee")
    lines.append("")
    lines.append(
        "This dry-run does not invoke TTS, ffmpeg, LibreOffice, or any "
        "other runtime synthesizer."
    )
    lines.append("")

    lines.append("## Stage plan")
    lines.append("")
    lines.append("| # | stage_id | title | status | action | command |")
    lines.append("|---:|---|---|:---:|:---:|---|")
    for i, entry in enumerate(result.plan, 1):
        cmd = (entry.command or "-").replace("|", "\\|")
        lines.append(
            f"| {i} | `{entry.stage_id}` | {entry.title} "
            f"| {entry.status.upper()} | {_ACTION_MARK.get(entry.action, entry.action.upper())} "
            f"| `{cmd}` |"
        )
    lines.append("")

    lines.append("## Next recommended action")
    lines.append("")
    lines.append(result.next_recommended_action or "(none)")
    lines.append("")

    lines.append("## Hard limits respected")
    lines.append("- no audio synthesized: pass")
    lines.append("- no video encoded: pass")
    lines.append("- no ffmpeg invocation: pass")
    lines.append("- no fake/placeholder artifacts created: pass")
    lines.append("- source projects untouched: pass")
    lines.append("- only `--dry-run` mode used in Phase 9E: pass")
    lines.append("")

    atomic_write_bytes(target, "\n".join(lines).encode("utf-8"))
    return Path(target)


def _verdict_word(v: str) -> str:
    return {
        "pass": "PASS",
        "fail": "FAIL",
        "pending_runtime_artifacts": "PENDING_RUNTIME_ARTIFACTS",
        "skipped": "SKIPPED",
        "blocked": "BLOCKED",
    }.get(v, v.upper())


def _local_windows_commands(run_id: str) -> List[str]:
    base = "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe"
    return [
        f"{base} scripts\\check_tts_providers.py --run runs\\{run_id}",
        f"{base} scripts\\generate_audio.py --run runs\\{run_id} --all",
        f"{base} scripts\\build_video.py --run runs\\{run_id} --all --concat-final",
        f"{base} scripts\\run_final_qa.py --run runs\\{run_id}",
        f"{base} scripts\\run_pipeline.py convert 1   # autopilot - re-runs the above with skip/idempotency",
    ]


def write_execution_report(
    *,
    project_root: Path,
    run_dir: Optional[Path],
    result: ConvertResult,
    env_info=None,
) -> Path:
    """Write `QA_REPORTS/autopilot_execution_report.md` for a real convert."""
    if run_dir is None:
        target = project_root / "QA_REPORTS" / "autopilot_execution_report.md"
    else:
        target = run_dir / "QA_REPORTS" / "autopilot_execution_report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    env_dict = _env_to_dict(env_info)

    lines: List[str] = []
    lines.append("# Phase 9F - autopilot execution report")
    lines.append("")
    lines.append(f"- generated_at: {iso_now()}")
    lines.append(f"- command: `convert {result.file_arg}`")
    lines.append(f"- input: {('`' + str(result.input_file) + '`') if result.input_file else '**not found**'}")
    lines.append(f"- run_dir: {('`' + str(result.run_dir) + '`') if result.run_dir else '**no run found**'}")
    if result.run_id:
        lines.append(f"- run_id: `{result.run_id}`")
    lines.append(f"- qa_verdict_before: **{result.qa_verdict_before.upper()}**")
    lines.append(f"- qa_verdict_after:  **{result.qa_verdict_after.upper()}**")
    lines.append(f"- overall_verdict: **{_verdict_word(result.overall_verdict)}**")
    if result.overall_reason:
        lines.append(f"- overall_reason: {result.overall_reason}")
    if result.aborted_at:
        lines.append(f"- aborted_at: `{result.aborted_at}`")
    if env_dict:
        lines.append(
            f"- runtime: **{env_dict.get('runtime', 'unknown')}** "
            f"(platform={env_dict.get('platform', 'unknown')})"
        )
    if result.error:
        lines.append(f"- error: `{result.error}`")
    lines.append("")

    lines.append("## Step log")
    lines.append("")
    if not result.steps:
        lines.append("(no steps were executed)")
    else:
        lines.append("| # | stage_id | verdict | exit | wall_s | notes |")
        lines.append("|---:|---|:---:|---:|---:|---|")
        for i, s in enumerate(result.steps, 1):
            notes = (s.skip_reason or s.error or "").replace("|", "\\|")
            lines.append(
                f"| {i} | `{s.stage_id}` | {_verdict_word(s.verdict)} "
                f"| {s.exit_code if s.exit_code is not None else '-'} "
                f"| {s.wall_seconds} | {notes} |"
            )
        lines.append("")

    for s in result.steps:
        lines.append(f"### {s.stage_id}")
        lines.append(f"- title: {s.title}")
        lines.append(f"- verdict: **{_verdict_word(s.verdict)}**")
        if s.command:
            lines.append(f"- command: `{' '.join(str(c) for c in s.command)}`")
        if s.skip_reason:
            lines.append(f"- skip_reason: {s.skip_reason}")
        if s.exit_code is not None:
            lines.append(f"- exit_code: {s.exit_code}")
        if s.wall_seconds:
            lines.append(f"- wall_seconds: {s.wall_seconds}")
        if s.stderr_tail:
            lines.append("- stderr (tail):")
            lines.append("  ```")
            for ln in s.stderr_tail.splitlines()[-12:]:
                lines.append(f"  {ln}")
            lines.append("  ```")
        if s.stdout_tail:
            lines.append("- stdout (tail):")
            lines.append("  ```")
            for ln in s.stdout_tail.splitlines()[-12:]:
                lines.append(f"  {ln}")
            lines.append("  ```")
        if s.error:
            lines.append(f"- error: `{s.error}`")
        lines.append("")

    lines.append("## Hard limits respected")
    lines.append("- no fake/placeholder audio created when stages PENDING/FAIL: pass")
    lines.append("- no fake/placeholder video created when stages PENDING/FAIL: pass")
    lines.append("- earlier phases (0-7B) never re-run by the autopilot: pass")
    lines.append("- source projects untouched: pass")
    lines.append("- never advances past a PENDING/FAIL step: pass")
    lines.append("")

    if env_dict:
        lines.append("## Environment summary")
        lines.append(f"- platform: `{env_dict.get('platform')}`")
        lines.append(f"- runtime: `{env_dict.get('runtime')}`")
        lines.append(f"- can_execute_windows_venv: `{env_dict.get('can_execute_windows_venv')}`")
        lines.append(f"- has_proxy_403_signature: `{env_dict.get('has_proxy_403_signature')}`")
        for note in env_dict.get("notes", []) or []:
            lines.append(f"- note: {note}")
        lines.append("")

    if result.overall_verdict in ("pending_runtime_artifacts", "fail"):
        lines.append("## Local-Windows runbook")
        lines.append("")
        lines.append("```")
        for cmd in _local_windows_commands(result.run_id or "<run_id>"):
            lines.append(cmd)
        lines.append("```")
        lines.append("")
        lines.append(
            "On a Windows shell where `.venv\\Scripts\\python.exe` works "
            "and `speech.platform.bing.com` is reachable, the autopilot will "
            "complete every PENDING step and the run_final_qa step will flip "
            "the verdict to PASS."
        )
        lines.append("")

    atomic_write_bytes(target, "\n".join(lines).encode("utf-8"))
    return Path(target)
