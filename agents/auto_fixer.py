"""auto_fixer - registry of small, conservative remediation hooks.

Phase 11B introduces this so the `rebuild-*` commands (and, optionally,
the autopilot) can attempt a *single* well-understood fix when a stage
fails, before giving up.

Design principles:
  * Never invent new artifacts.
  * Each fixer is **single-purpose** and idempotent.
  * Fixers return an ``AutoFixResult`` describing what was attempted.
  * On every applied fix, a row is appended to
    ``runs/<run_id>/QA_REPORTS/auto_fix_log.md`` (atomic write of the
    full file each time - the project is small enough that this is fine
    and avoids partial writes).
  * `try_auto_fix(stage_id, error_text, run_dir, project_root)` consults
    the registry, runs at most one fixer, and returns.

Built-in fixers (Phase 11B):
  * latex_invalid_command  -> calls integrations.auto_fixer_latex
                              (already present from Phase 5) to repair the
                              offending package_NNN_extended_fa.tex.
  * empty_audio_file       -> deletes any zero-byte file under
                              audio/<package>/slide_*.mp3 so the next
                              rebuild-audio can re-synthesize cleanly.
  * stale_video_clips      -> deletes any zero-byte file under
                              video_segments/<package>/slide_*.mp4 so the
                              next rebuild-video re-encodes cleanly.
  * partial_audio_manifest -> rewrites audio/audio_manifest.json without
                              entries that point to missing files (so the
                              autopilot's plan re-runs them).

This module *never* synthesizes audio or video itself - those are runtime
ops that belong in the corresponding stage scripts. Fixers only delete
clearly-invalid leftover files and patch metadata.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from integrations.atomic_io import atomic_write_bytes, atomic_write_json, iso_now, read_json


@dataclass
class AutoFixResult:
    stage_id: str
    fixer_name: str
    triggered_by: str               # short description of the matched error
    applied: bool = False
    files_removed: List[str] = field(default_factory=list)
    files_rewritten: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Individual fixers
# ---------------------------------------------------------------------------


def _fix_latex_invalid_command(
    stage_id: str, error_text: str, run_dir: Path
) -> AutoFixResult:
    res = AutoFixResult(stage_id=stage_id, fixer_name="latex_invalid_command",
                        triggered_by="LaTeX: undefined control sequence / missing $")
    # Identify the offending .tex from the error
    m = re.search(r"(package_\d{3}[\w_]*\.tex)", error_text or "")
    if not m:
        res.notes.append("could not identify a .tex from the error text")
        return res
    tex_name = m.group(1)
    tex_path = run_dir / "latex" / tex_name
    if not tex_path.is_file():
        res.notes.append(f"target {tex_path} not found")
        return res
    try:
        from integrations.auto_fixer_latex import auto_fix_latex_file  # type: ignore
    except Exception as exc:  # noqa: BLE001
        res.notes.append(
            f"integrations.auto_fixer_latex not importable: {exc!r}; "
            "no automatic fix attempted"
        )
        return res
    try:
        changed = auto_fix_latex_file(str(tex_path))
    except Exception as exc:  # noqa: BLE001
        res.error = f"auto_fix_latex_file failed: {exc!r}"
        return res
    if changed:
        res.applied = True
        res.files_rewritten.append(str(tex_path))
        res.notes.append("rewrote .tex with conservative auto-fixes; "
                         "re-run rebuild-pdfs to verify")
    else:
        res.notes.append("auto_fix_latex_file reported no fixable issues")
    return res


def _fix_empty_audio_file(
    stage_id: str, error_text: str, run_dir: Path
) -> AutoFixResult:
    res = AutoFixResult(stage_id=stage_id, fixer_name="empty_audio_file",
                        triggered_by="audio synth left zero-byte file")
    audio_root = run_dir / "audio"
    if not audio_root.is_dir():
        res.notes.append("no audio/ directory yet; nothing to clean")
        return res
    for f in audio_root.glob("package_*/slide_*.mp3"):
        try:
            if f.stat().st_size == 0:
                f.unlink()
                res.files_removed.append(str(f))
        except OSError as exc:  # noqa: BLE001
            res.notes.append(f"could not remove {f}: {exc!r}")
    for f in audio_root.glob("package_*/slide_*.wav"):
        try:
            if f.stat().st_size == 0:
                f.unlink()
                res.files_removed.append(str(f))
        except OSError:
            pass
    res.applied = bool(res.files_removed)
    if not res.applied:
        res.notes.append("no zero-byte audio files found")
    return res


def _fix_stale_video_clips(
    stage_id: str, error_text: str, run_dir: Path
) -> AutoFixResult:
    res = AutoFixResult(stage_id=stage_id, fixer_name="stale_video_clips",
                        triggered_by="video render left zero-byte clip(s)")
    vseg = run_dir / "video_segments"
    if not vseg.is_dir():
        res.notes.append("no video_segments/ directory yet; nothing to clean")
        return res
    for f in vseg.glob("package_*/slide_*.mp4"):
        try:
            if f.stat().st_size == 0:
                f.unlink()
                res.files_removed.append(str(f))
        except OSError:
            pass
    res.applied = bool(res.files_removed)
    if not res.applied:
        res.notes.append("no zero-byte clips found")
    return res


def _fix_partial_audio_manifest(
    stage_id: str, error_text: str, run_dir: Path
) -> AutoFixResult:
    res = AutoFixResult(stage_id=stage_id, fixer_name="partial_audio_manifest",
                        triggered_by="audio_manifest references missing files")
    manifest = run_dir / "audio" / "audio_manifest.json"
    if not manifest.is_file():
        res.notes.append("audio/audio_manifest.json not present; nothing to patch")
        return res
    try:
        data = read_json(manifest)
    except Exception as exc:  # noqa: BLE001
        res.error = f"audio_manifest.json unreadable: {exc!r}"
        return res
    if not isinstance(data, dict):
        res.error = "audio_manifest.json is not a dict"
        return res
    pkgs = data.get("packages") or {}
    changed = False
    for pkg_id, block in list(pkgs.items()):
        kept = []
        for s in (block.get("slides") or []):
            rel = s.get("audio_path") or ""
            if not rel:
                continue
            p = (run_dir / rel).resolve()
            if p.is_file() and p.stat().st_size > 0:
                kept.append(s)
            else:
                changed = True
                res.notes.append(f"drop manifest entry pointing to missing {rel}")
        if kept:
            block["slides"] = kept
        else:
            # Whole package's audio is gone -> drop the block entry
            pkgs.pop(pkg_id, None)
            changed = True
            res.notes.append(f"removed empty package block {pkg_id}")
    if changed:
        data["packages"] = pkgs
        data["generated_at"] = iso_now()
        atomic_write_json(manifest, data)
        res.files_rewritten.append(str(manifest))
        res.applied = True
    else:
        res.notes.append("audio_manifest already self-consistent")
    return res


# ---------------------------------------------------------------------------
# Registry + entry point
# ---------------------------------------------------------------------------


FixerFn = Callable[[str, str, Path], AutoFixResult]


# (name, error-pattern regex, fixer fn). First match wins.
_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "latex_invalid_command",
        "pattern": re.compile(r"(Undefined control sequence|Missing \$|! LaTeX Error)", re.I),
        "fn": _fix_latex_invalid_command,
    },
    {
        "name": "empty_audio_file",
        "pattern": re.compile(r"(empty audio|zero[-\s]?byte audio|size=0.*mp3|size=0.*wav)", re.I),
        "fn": _fix_empty_audio_file,
    },
    {
        "name": "stale_video_clips",
        "pattern": re.compile(r"(per-slide build had \d+ failures|clip validation failed|size=0.*mp4)", re.I),
        "fn": _fix_stale_video_clips,
    },
    {
        "name": "partial_audio_manifest",
        "pattern": re.compile(r"(missing audio|audio_manifest|no audio entry)", re.I),
        "fn": _fix_partial_audio_manifest,
    },
]


def try_auto_fix(
    *,
    stage_id: str,
    error_text: str,
    run_dir: Path,
    project_root: Optional[Path] = None,
) -> Optional[AutoFixResult]:
    """Try one matching fixer. Returns ``None`` if nothing matched.

    Always appends an entry to ``QA_REPORTS/auto_fix_log.md`` when a fixer
    was *attempted*, regardless of whether the fix was applied.
    """
    err = error_text or ""
    for entry in _REGISTRY:
        if entry["pattern"].search(err):
            fixer: FixerFn = entry["fn"]
            try:
                res = fixer(stage_id, err, Path(run_dir))
            except Exception as exc:  # noqa: BLE001
                res = AutoFixResult(
                    stage_id=stage_id, fixer_name=entry["name"],
                    triggered_by=entry["pattern"].pattern,
                    error=f"fixer crashed: {exc!r}",
                )
            _append_auto_fix_log(run_dir=Path(run_dir), res=res)
            return res
    return None


def _append_auto_fix_log(*, run_dir: Path, res: AutoFixResult) -> Path:
    """Maintain runs/<run_id>/QA_REPORTS/auto_fix_log.md cumulatively."""
    log_path = run_dir / "QA_REPORTS" / "auto_fix_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not log_path.is_file():
        header = (
            "# Auto-fix log\n\n"
            "> Phase 11B cumulative log. Each block records one attempted\n"
            "> auto-fix. Fixes are conservative: they only delete zero-byte\n"
            "> files or patch manifests; they never invent audio/video/PDF\n"
            "> content.\n\n"
        )
    block: List[str] = []
    block.append(f"## {iso_now()} - {res.stage_id} / {res.fixer_name}")
    block.append(f"- triggered_by: `{res.triggered_by}`")
    block.append(f"- applied: **{res.applied}**")
    if res.files_removed:
        block.append("- files_removed:")
        for f in res.files_removed[:16]:
            block.append(f"  - `{f}`")
        if len(res.files_removed) > 16:
            block.append(f"  - ... +{len(res.files_removed) - 16} more")
    if res.files_rewritten:
        block.append("- files_rewritten:")
        for f in res.files_rewritten[:16]:
            block.append(f"  - `{f}`")
    for note in res.notes:
        block.append(f"- note: {note}")
    if res.error:
        block.append(f"- error: `{res.error}`")
    block.append("")

    existing = ""
    if log_path.is_file():
        try:
            existing = log_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    payload = (header + existing + "\n".join(block) + "\n").encode("utf-8")
    atomic_write_bytes(log_path, payload)
    return log_path
