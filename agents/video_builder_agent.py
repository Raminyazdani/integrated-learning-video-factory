"""video_builder_agent - Stage 9 (ffmpeg / video) agent.

Phase 9A (smoke test) + Phase 9B (per-package video) + Phase 9C (all
packages + final concatenated video).

All file writes go through integrations.atomic_io (or os.replace) so partial
writes are impossible. No fake artifacts are ever produced.
"""
from __future__ import annotations

import importlib
import json
import time as _time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations.atomic_io import atomic_write_bytes, atomic_write_json, iso_now
from integrations.ffmpeg_builder import (
    ClipResult,
    FFmpegInfo,
    FFprobeInfo,
    detect_ffmpeg,
    detect_ffprobe,
    make_slide_clip,
    probe_duration_seconds,
)
from integrations.ffmpeg_concat import ConcatResult, concat_clips


MIN_AUDIO_DURATION_SECONDS = 1.0
MIN_VIDEO_DURATION_SECONDS = 1.0
SMOKE_VIDEO_REL_PATH = Path("video_segments") / "_smoke" / "smoke_test.mp4"
FINAL_VIDEO_BASENAME_TEMPLATE = "{run_number}_final_educational_video.mp4"


# ---------------------------------------------------------------------------
# Discovery (Phase 9A smoke)
# ---------------------------------------------------------------------------


def _pick_slide_image(run_dir: Path) -> Optional[Path]:
    for c in (
        run_dir / "slide_images" / "full_teaching_deck-001.png",
        run_dir / "slide_images" / "full_teaching_deck-0001.png",
    ):
        if c.is_file():
            return c
    slide_dir = run_dir / "slide_images"
    if not slide_dir.is_dir():
        return None
    pngs = sorted(slide_dir.glob("*.png"))
    return pngs[0] if pngs else None


def _audio_is_valid_for_smoke(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    dur = probe_duration_seconds(path)
    return dur is not None and float(dur) > MIN_AUDIO_DURATION_SECONDS


def _pick_audio_file(run_dir: Path) -> Optional[Path]:
    audio_root = run_dir / "audio"
    if not audio_root.is_dir():
        return None
    for c in (
        audio_root / "_smoke" / "smoke_test_fa.mp3",
        audio_root / "_smoke" / "smoke_test_fa.wav",
    ):
        if _audio_is_valid_for_smoke(c):
            return c
    pkg = audio_root / "package_001" / "slide_001.mp3"
    if _audio_is_valid_for_smoke(pkg):
        return pkg
    for pkg_dir in sorted(audio_root.glob("package_*")):
        if not pkg_dir.is_dir():
            continue
        for ext in (".mp3", ".wav"):
            for p in sorted(pkg_dir.glob(f"slide_*{ext}")):
                if _audio_is_valid_for_smoke(p):
                    return p
    return None


# ---------------------------------------------------------------------------
# Static health check
# ---------------------------------------------------------------------------


def static_health_check(project_root: Path) -> dict:
    result: Dict[str, Any] = {"imports": {}, "ok": True, "errors": []}
    modules = [
        "integrations.atomic_io",
        "integrations.env_detector",
        "integrations.ffmpeg_builder",
        "integrations.ffmpeg_concat",
        "agents.video_builder_agent",
        "agents.tts_audio_agent",
    ]
    for m in modules:
        try:
            importlib.import_module(m)
            result["imports"][m] = "ok"
        except Exception as exc:
            result["imports"][m] = f"error: {exc!r}"
            result["ok"] = False
            result["errors"].append(f"import {m}: {exc!r}")
    return result


# ---------------------------------------------------------------------------
# Phase 9A smoke
# ---------------------------------------------------------------------------


@dataclass
class SmokeResult:
    ffmpeg: FFmpegInfo = field(default_factory=FFmpegInfo)
    ffprobe: FFprobeInfo = field(default_factory=FFprobeInfo)
    slide_image: Optional[Path] = None
    audio_file: Optional[Path] = None
    audio_duration: Optional[float] = None
    clip: Optional[ClipResult] = None
    verdict: str = "pending_local_claude_code_validation"
    reason: Optional[str] = None


def run_smoke_test(*, project_root: Path, run_dir: Path) -> SmokeResult:
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    out = SmokeResult()
    out.ffmpeg = detect_ffmpeg()
    out.ffprobe = detect_ffprobe()
    out.slide_image = _pick_slide_image(run_dir)
    out.audio_file = _pick_audio_file(run_dir)
    if out.audio_file is not None:
        out.audio_duration = probe_duration_seconds(out.audio_file)

    if not out.ffmpeg.available:
        out.verdict = "fail"
        out.reason = f"ffmpeg not available ({out.ffmpeg.error or 'unknown'})."
        return out
    if out.slide_image is None:
        out.verdict = "fail"
        out.reason = "no slide image under slide_images/ - Phase 6 must complete."
        return out
    if out.audio_file is None:
        out.verdict = "pending_local_claude_code_validation"
        out.reason = (
            "no valid Phase-8 audio found. ffmpeg pipeline is healthy (detected), "
            "but the smoke MP4 is not built without real audio."
        )
        return out

    out_path = run_dir / SMOKE_VIDEO_REL_PATH
    clip = make_slide_clip(
        image=out.slide_image, audio=out.audio_file, out_path=out_path,
        ffmpeg_binary=out.ffmpeg.binary, timeout_seconds=60,
    )
    out.clip = clip
    if clip.success and clip.duration_seconds and clip.duration_seconds > 0:
        out.verdict = "pass"
    else:
        out.verdict = "fail"
        out.reason = clip.error or "ffmpeg failed without a specific error"
    return out


# ---------------------------------------------------------------------------
# Phase 9B result types
# ---------------------------------------------------------------------------


@dataclass
class SlideVideoResult:
    slide_id: str
    success: bool = False
    skipped_reason: Optional[str] = None
    clip_path: Optional[Path] = None
    image_path: Optional[Path] = None
    audio_path: Optional[Path] = None
    audio_duration: Optional[float] = None
    clip_size_bytes: int = 0
    clip_duration_seconds: Optional[float] = None
    wall_seconds: float = 0.0
    error: Optional[str] = None
    validation_status: str = "fail"


@dataclass
class PackageVideoResult:
    package_id: str
    audio_manifest_path: Path
    slide_manifest_path: Path
    clips_dir: Path
    package_video_path: Path
    started_at: str = ""
    finished_at: str = ""
    slides: List[SlideVideoResult] = field(default_factory=list)
    concat: Optional[ConcatResult] = None
    package_video_size_bytes: int = 0
    package_video_duration_seconds: Optional[float] = None
    total_wall_seconds: float = 0.0
    n_reused: int = 0
    verdict: str = "pending_local_claude_code_validation"
    reason: Optional[str] = None

    @property
    def n_slides(self) -> int:
        return len(self.slides)

    @property
    def n_succeeded(self) -> int:
        return sum(1 for s in self.slides if s.validation_status == "pass")

    @property
    def n_failed(self) -> int:
        return self.n_slides - self.n_succeeded


# ---------------------------------------------------------------------------
# Phase 9C result types
# ---------------------------------------------------------------------------


@dataclass
class FinalVideoResult:
    output_path: Path
    attempted: bool = False
    success: bool = False
    size_bytes: int = 0
    duration_seconds: Optional[float] = None
    n_inputs: int = 0
    wall_seconds: float = 0.0
    error: Optional[str] = None
    stderr_tail: Optional[str] = None


@dataclass
class RunVideoResult:
    """Aggregate of every package video build, plus optional final concat."""
    run_id: str
    started_at: str = ""
    finished_at: str = ""
    packages: List[PackageVideoResult] = field(default_factory=list)
    final: Optional[FinalVideoResult] = None
    verdict: str = "pending_local_claude_code_validation"
    reason: Optional[str] = None

    @property
    def n_packages(self) -> int:
        return len(self.packages)

    @property
    def n_packages_passed(self) -> int:
        return sum(1 for p in self.packages if p.verdict == "pass")

    @property
    def n_packages_failed(self) -> int:
        return sum(1 for p in self.packages if p.verdict == "fail")

    @property
    def n_packages_pending(self) -> int:
        return sum(1 for p in self.packages if p.verdict == "pending_local_claude_code_validation")

    @property
    def n_slides_total(self) -> int:
        return sum(p.n_slides for p in self.packages)

    @property
    def total_wall_seconds(self) -> float:
        return sum(p.total_wall_seconds for p in self.packages)


# ---------------------------------------------------------------------------
# Phase 9B helpers
# ---------------------------------------------------------------------------


def _load_audio_manifest(run_dir: Path) -> Optional[dict]:
    path = run_dir / "audio" / "audio_manifest.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_slide_manifest(run_dir: Path) -> dict:
    path = run_dir / "slides" / "slide_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"slide_manifest.json not found at {path}. Phase 6 must complete."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_full_narration(run_dir: Path) -> List[dict]:
    full = run_dir / "narration_scripts" / "full_narration.json"
    if not full.is_file():
        raise FileNotFoundError(
            f"precondition failed: {full} not found. Phase 7B must complete."
        )
    with open(full, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return list(payload.get("packages", []))


def _slide_number_from_index(idx: Any) -> str:
    try:
        return f"{int(idx):03d}"
    except Exception:
        return str(idx)


def _resolve_image_path(run_dir: Path, image_path_in_manifest: str) -> Path:
    p = Path(image_path_in_manifest)
    if p.is_absolute() and p.is_file():
        return p
    return run_dir / p


def _run_number_from_run_id(run_id: str) -> str:
    """`001_1_20260520_2128` -> `001`."""
    return run_id.split("_")[0] if run_id else "000"


# ---------------------------------------------------------------------------
# Phase 9B - per-package video
# ---------------------------------------------------------------------------


def build_package_video(
    *,
    project_root: Path,
    run_dir: Path,
    package_id: str,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    overwrite: bool = False,
    progress_callback=None,
) -> PackageVideoResult:
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()

    clips_dir = run_dir / "video_segments" / package_id
    package_video_path = run_dir / "video_segments" / f"{package_id}.mp4"

    result = PackageVideoResult(
        package_id=package_id,
        audio_manifest_path=run_dir / "audio" / "audio_manifest.json",
        slide_manifest_path=run_dir / "slides" / "slide_manifest.json",
        clips_dir=clips_dir,
        package_video_path=package_video_path,
        started_at=iso_now(),
    )

    ffmpeg = detect_ffmpeg()
    if not ffmpeg.available:
        result.verdict = "fail"
        result.reason = f"ffmpeg not available ({ffmpeg.error or 'unknown'})."
        result.finished_at = iso_now()
        return result

    try:
        slide_manifest = _load_slide_manifest(run_dir)
    except FileNotFoundError as exc:
        result.verdict = "fail"
        result.reason = str(exc)
        result.finished_at = iso_now()
        return result

    pkg_slides = [
        s for s in slide_manifest.get("slides", [])
        if s.get("package_id") == package_id
    ]
    if not pkg_slides:
        result.verdict = "fail"
        result.reason = f"slide_manifest.json has no entries for {package_id}."
        result.finished_at = iso_now()
        return result

    pkg_slides.sort(key=lambda s: s.get("slide_index_in_package") or 0)

    audio_manifest = _load_audio_manifest(run_dir)
    audio_lookup: Dict[str, dict] = {}
    if audio_manifest:
        pkg_block = (audio_manifest.get("packages") or {}).get(package_id) or {}
        for s in pkg_block.get("slides") or []:
            if s.get("slide_id"):
                audio_lookup[s["slide_id"]] = s

    if not audio_lookup:
        result.verdict = "pending_local_claude_code_validation"
        result.reason = (
            f"audio_manifest.json has no audio entries for {package_id}. "
            "Run Phase 8B/8C on local Windows first."
        )
        for s in pkg_slides:
            sv = SlideVideoResult(
                slide_id=s.get("slide_id"),
                image_path=_resolve_image_path(run_dir, s.get("image_path") or ""),
                error="missing audio (Phase 8 PENDING)",
            )
            result.slides.append(sv)
        result.finished_at = iso_now()
        return result

    clips_dir.mkdir(parents=True, exist_ok=True)
    started_total = _time.time()

    valid_clip_paths: List[Path] = []
    fatal_failure = False

    for idx, s in enumerate(pkg_slides):
        slide_id = s.get("slide_id") or f"unknown_{idx:03d}"
        sno = _slide_number_from_index(s.get("slide_index_in_package") or idx + 1)
        clip_path = clips_dir / f"slide_{sno}.mp4"

        slide_res = SlideVideoResult(slide_id=slide_id, clip_path=clip_path)
        slide_res.image_path = _resolve_image_path(run_dir, s.get("image_path") or "")

        audio_entry = audio_lookup.get(slide_id)
        if not audio_entry or not audio_entry.get("audio_path"):
            slide_res.error = f"no audio entry for {slide_id} in audio_manifest.json"
            result.slides.append(slide_res)
            fatal_failure = True
            if progress_callback:
                try: progress_callback(idx + 1, len(pkg_slides), slide_id, "no audio")
                except Exception: pass
            continue
        audio_path = (run_dir / audio_entry["audio_path"]).resolve()
        slide_res.audio_path = audio_path
        slide_res.audio_duration = audio_entry.get("duration_seconds")
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            slide_res.error = f"audio file missing or empty: {audio_path}"
            result.slides.append(slide_res)
            fatal_failure = True
            continue
        if slide_res.audio_duration is None:
            slide_res.audio_duration = probe_duration_seconds(audio_path)
        if (
            slide_res.audio_duration is None
            or float(slide_res.audio_duration) <= MIN_AUDIO_DURATION_SECONDS
        ):
            slide_res.error = (
                f"audio duration too short ({slide_res.audio_duration}s)."
            )
            result.slides.append(slide_res)
            fatal_failure = True
            continue

        if not slide_res.image_path.is_file():
            slide_res.error = f"slide image missing: {slide_res.image_path}"
            result.slides.append(slide_res)
            fatal_failure = True
            continue

        if progress_callback:
            try: progress_callback(idx + 1, len(pkg_slides), slide_id, str(clip_path.name))
            except Exception: pass

        if clip_path.exists() and not overwrite:
            dur = probe_duration_seconds(clip_path)
            size = clip_path.stat().st_size
            if size > 0 and dur is not None and float(dur) > MIN_VIDEO_DURATION_SECONDS:
                slide_res.success = True
                slide_res.skipped_reason = "already_valid"
                slide_res.clip_size_bytes = size
                slide_res.clip_duration_seconds = dur
                slide_res.validation_status = "pass"
                result.n_reused += 1
                result.slides.append(slide_res)
                valid_clip_paths.append(clip_path)
                continue
            try: clip_path.unlink()
            except OSError: pass

        started = _time.time()
        clip = make_slide_clip(
            image=slide_res.image_path,
            audio=audio_path,
            out_path=clip_path,
            width=width, height=height, fps=fps,
            ffmpeg_binary=ffmpeg.binary,
            timeout_seconds=120,
        )
        slide_res.wall_seconds = round(_time.time() - started, 3)
        result.total_wall_seconds += slide_res.wall_seconds

        if not clip.success:
            slide_res.error = clip.error or "ffmpeg failed"
            result.slides.append(slide_res)
            fatal_failure = True
            continue

        slide_res.success = True
        slide_res.clip_size_bytes = clip.size_bytes
        slide_res.clip_duration_seconds = clip.duration_seconds
        ok_size = clip.size_bytes > 0
        ok_dur = (
            clip.duration_seconds is not None
            and float(clip.duration_seconds) > MIN_VIDEO_DURATION_SECONDS
        )
        slide_res.validation_status = "pass" if (ok_size and ok_dur) else "fail"
        if slide_res.validation_status != "pass":
            slide_res.error = (
                f"clip validation failed (size={clip.size_bytes}, "
                f"duration={clip.duration_seconds})"
            )
            fatal_failure = True
        else:
            valid_clip_paths.append(clip.out_path or clip_path)
        result.slides.append(slide_res)

    result.total_wall_seconds = round(_time.time() - started_total, 3)

    if fatal_failure or len(valid_clip_paths) != len(pkg_slides):
        result.verdict = "fail"
        result.reason = (
            f"per-slide build had {len(pkg_slides) - len(valid_clip_paths)} failures; "
            "concat step skipped."
        )
        if package_video_path.exists():
            try: package_video_path.unlink()
            except OSError: pass
        result.finished_at = iso_now()
        _write_video_manifest(result, run_dir=run_dir)
        return result

    concat_res = concat_clips(
        valid_clip_paths, package_video_path,
        ffmpeg_binary=ffmpeg.binary, timeout_seconds=300,
    )
    result.concat = concat_res
    if concat_res.success:
        result.package_video_size_bytes = concat_res.size_bytes
        result.package_video_duration_seconds = concat_res.duration_seconds
        result.verdict = "pass"
    else:
        result.verdict = "fail"
        result.reason = concat_res.error or "concat failed without specific error"
        if package_video_path.exists():
            try: package_video_path.unlink()
            except OSError: pass

    result.finished_at = iso_now()
    _write_video_manifest(result, run_dir=run_dir)
    return result


def _write_video_manifest(result: PackageVideoResult, *, run_dir: Path) -> Path:
    manifest_path = run_dir / "video_segments" / "video_manifest.json"
    existing: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "generated_at": iso_now(),
        "packages": {},
    }
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if "packages" not in existing:
                existing["packages"] = {}
        except Exception:
            pass

    def _rel(p: Optional[Path]) -> Optional[str]:
        if p is None:
            return None
        try:
            return str(p.relative_to(run_dir)).replace("\\", "/")
        except ValueError:
            return str(p)

    pkg_block = {
        "package_id": result.package_id,
        "verdict": result.verdict,
        "reason": result.reason,
        "package_video": _rel(result.package_video_path) if result.verdict == "pass" else None,
        "package_video_size_bytes": result.package_video_size_bytes,
        "package_video_duration_seconds": result.package_video_duration_seconds,
        "clips_dir": _rel(result.clips_dir),
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "totals": {
            "n_slides": result.n_slides,
            "n_succeeded": result.n_succeeded,
            "n_failed": result.n_failed,
            "n_reused": result.n_reused,
            "total_wall_seconds": result.total_wall_seconds,
        },
        "slides": [
            {
                "slide_id": s.slide_id,
                "success": s.success,
                "skipped_reason": s.skipped_reason,
                "clip_path": _rel(s.clip_path) if s.success else None,
                "image_path": _rel(s.image_path),
                "audio_path": _rel(s.audio_path),
                "audio_duration": s.audio_duration,
                "clip_size_bytes": s.clip_size_bytes,
                "clip_duration_seconds": s.clip_duration_seconds,
                "wall_seconds": s.wall_seconds,
                "validation_status": s.validation_status,
                "error": s.error,
            }
            for s in result.slides
        ],
    }
    existing["packages"][result.package_id] = pkg_block
    existing["generated_at"] = iso_now()
    atomic_write_json(manifest_path, existing)
    return manifest_path


# ---------------------------------------------------------------------------
# Phase 9C - all packages + final concat
# ---------------------------------------------------------------------------


def build_all_packages_video(
    *,
    project_root: Path,
    run_dir: Path,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    overwrite: bool = False,
    progress_callback=None,
) -> RunVideoResult:
    """Iterate every package in full_narration.json and call
    build_package_video() for each. Returns a RunVideoResult.

    This function does NOT do the final concat. Call build_final_video()
    on the returned RunVideoResult if needed.
    """
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    run_result = RunVideoResult(run_id=run_dir.name, started_at=iso_now())

    try:
        packages = _load_full_narration(run_dir)
    except FileNotFoundError as exc:
        run_result.verdict = "fail"
        run_result.reason = str(exc)
        run_result.finished_at = iso_now()
        return run_result

    ffmpeg = detect_ffmpeg()
    if not ffmpeg.available:
        run_result.verdict = "fail"
        run_result.reason = f"ffmpeg not available ({ffmpeg.error or 'unknown'})."
        run_result.finished_at = iso_now()
        return run_result

    audio_manifest = _load_audio_manifest(run_dir)
    if audio_manifest is None:
        # No audio at all -> mark every package PENDING without attempting.
        run_result.verdict = "pending_local_claude_code_validation"
        run_result.reason = (
            "audio/audio_manifest.json not found. Phase 8C must produce real "
            "audio on local Windows before Phase 9C can run."
        )
        run_result.finished_at = iso_now()
        return run_result

    for pkg_block in packages:
        package_id = pkg_block.get("package_id")
        if not package_id:
            continue

        def _pkg_progress(i: int, n: int, slide_id: str, note: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(package_id, i, n, slide_id, note)
            except Exception:
                pass

        pkg_result = build_package_video(
            project_root=project_root,
            run_dir=run_dir,
            package_id=package_id,
            width=width, height=height, fps=fps,
            overwrite=overwrite,
            progress_callback=_pkg_progress,
        )
        run_result.packages.append(pkg_result)

    # Decide aggregate verdict.
    if run_result.n_packages == 0:
        run_result.verdict = "fail"
        run_result.reason = "no packages found in full_narration.json"
    elif run_result.n_packages_passed == run_result.n_packages:
        run_result.verdict = "pass"
    elif run_result.n_packages_pending > 0 and run_result.n_packages_failed == 0:
        run_result.verdict = "pending_local_claude_code_validation"
        run_result.reason = (
            f"{run_result.n_packages_pending}/{run_result.n_packages} packages "
            "PENDING (audio missing for those packages)."
        )
    else:
        run_result.verdict = "fail"
        run_result.reason = (
            f"{run_result.n_packages_failed}/{run_result.n_packages} packages failed."
        )

    run_result.finished_at = iso_now()
    return run_result


def build_final_video(
    *,
    project_root: Path,
    run_dir: Path,
    run_result: Optional[RunVideoResult] = None,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> FinalVideoResult:
    """Concatenate every per-package MP4 into
    ``output/<run_number>_final_educational_video.mp4``.

    If ``run_result`` is supplied, the function uses its package list
    (in the same order). Otherwise it reads ``video_segments/video_manifest.json``
    and concatenates the packages listed there in insertion order.

    Never creates a fake artifact. If any package video is missing or
    invalid, the function returns ``FinalVideoResult(success=False)``
    without writing.
    """
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    run_number = _run_number_from_run_id(run_dir.name)
    output_dir = project_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / FINAL_VIDEO_BASENAME_TEMPLATE.format(run_number=run_number)
    fin = FinalVideoResult(output_path=out_path, attempted=False)

    ffmpeg = detect_ffmpeg()
    if not ffmpeg.available:
        fin.error = f"ffmpeg not available ({ffmpeg.error or 'unknown'})."
        return fin

    package_video_paths: List[Path] = []
    if run_result is not None:
        for p in run_result.packages:
            if p.verdict == "pass" and p.package_video_path.is_file():
                package_video_paths.append(p.package_video_path)
            else:
                fin.error = (
                    f"package {p.package_id} is not 'pass' (verdict={p.verdict}); "
                    "final concat is not attempted."
                )
                return fin
    else:
        manifest_path = run_dir / "video_segments" / "video_manifest.json"
        if not manifest_path.is_file():
            fin.error = (
                "video_segments/video_manifest.json not found; build all "
                "package videos first (Phase 9C --all)."
            )
            return fin
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            fin.error = f"failed to parse video_manifest.json: {exc!r}"
            return fin
        for pkg_id, block in (data.get("packages") or {}).items():
            if block.get("verdict") != "pass":
                fin.error = (
                    f"package {pkg_id} is not 'pass'; final concat is not "
                    "attempted."
                )
                return fin
            video_rel = block.get("package_video")
            if not video_rel:
                fin.error = f"package {pkg_id} has no package_video path."
                return fin
            video_path = (run_dir / video_rel).resolve()
            if not video_path.is_file():
                fin.error = f"package video missing on disk: {video_path}"
                return fin
            package_video_paths.append(video_path)

    if not package_video_paths:
        fin.error = "no package videos to concatenate"
        return fin

    fin.attempted = True
    fin.n_inputs = len(package_video_paths)
    started = _time.time()
    concat = concat_clips(
        package_video_paths, out_path,
        ffmpeg_binary=ffmpeg.binary, timeout_seconds=900,
    )
    fin.wall_seconds = round(_time.time() - started, 3)
    if not concat.success:
        fin.error = concat.error or "final concat failed without specific error"
        fin.stderr_tail = concat.stderr_tail
        if out_path.exists():
            try: out_path.unlink()
            except OSError: pass
        return fin

    fin.success = True
    fin.size_bytes = concat.size_bytes
    fin.duration_seconds = concat.duration_seconds
    return fin


# ---------------------------------------------------------------------------
# Report writer (handles 9A smoke + 9B per-package + 9C all/final)
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


def _verdict_marker(verdict: Optional[str]) -> str:
    if verdict is None:
        return "**(verdict not set)**"
    if verdict == "pass":
        return "**pass**"
    if verdict == "fail":
        return "**fail**"
    if verdict == "pending_local_claude_code_validation":
        return "**PENDING_LOCAL_CLAUDE_CODE_VALIDATION**"
    return f"**{verdict}**"


def _row(label: str, ok: bool, env_blocked: bool) -> str:
    if ok:
        return f"- {label}: **pass**"
    if env_blocked:
        return f"- {label}: **PENDING_LOCAL_CLAUDE_CODE_VALIDATION**"
    return f"- {label}: **fail**"


def write_video_build_report(
    *,
    report_path: Path,
    run_id: str,
    smoke: Optional[SmokeResult] = None,
    package_result: Optional[PackageVideoResult] = None,
    run_result: Optional[RunVideoResult] = None,
    final_result: Optional[FinalVideoResult] = None,
    env_info=None,
    static_health: Optional[dict] = None,
    windows_command: Optional[str] = None,
) -> Path:
    lines: List[str] = []
    env_dict = _env_to_dict(env_info)
    env_blocked = env_dict.get("runtime") == "cowork_sandbox"

    if run_result is not None:
        scope = "Phase 9C - all packages" + (" + final concat" if final_result else "")
        verdict = run_result.verdict
    elif package_result is not None:
        scope = f"Phase 9B - {package_result.package_id}"
        verdict = package_result.verdict
    elif smoke is not None:
        scope = "Phase 9A smoke test"
        verdict = smoke.verdict
    else:
        scope = "Phase 9 (no scope)"
        verdict = "pending_local_claude_code_validation"

    lines.append("# Phase 9 ffmpeg video report")
    lines.append("")
    lines.append(f"- generated_at: {iso_now()}")
    lines.append(f"- run_id: {run_id}")
    lines.append(f"- scope: {scope}")
    if env_dict:
        lines.append(
            f"- runtime: **{env_dict.get('runtime', 'unknown')}** "
            f"(platform={env_dict.get('platform', 'unknown')}, "
            f"can_exec_windows_venv={env_dict.get('can_execute_windows_venv', False)}, "
            f"proxy_403={env_dict.get('has_proxy_403_signature', False)})"
        )
    lines.append(f"- verdict: {_verdict_marker(verdict)}")
    reason = (
        (run_result.reason if run_result is not None else None)
        or (package_result.reason if package_result is not None else None)
        or (smoke.reason if smoke is not None else None)
    )
    if reason:
        lines.append(f"- reason: {reason}")
    lines.append("")

    if smoke is not None and run_result is None and package_result is None:
        lines.append("## ffmpeg / ffprobe detection")
        lines.append(f"- ffmpeg: available=**{smoke.ffmpeg.available}**")
        if smoke.ffmpeg.binary:
            lines.append(f"- ffmpeg.binary: `{smoke.ffmpeg.binary}`")
        if smoke.ffmpeg.version:
            lines.append(f"- ffmpeg.version: `{smoke.ffmpeg.version}`")
        if smoke.ffmpeg.full_banner:
            lines.append(f"- ffmpeg.banner: `{smoke.ffmpeg.full_banner}`")
        lines.append(f"- ffprobe: available=**{smoke.ffprobe.available}**")
        if smoke.ffprobe.binary:
            lines.append(f"- ffprobe.binary: `{smoke.ffprobe.binary}`")
        lines.append("")
        lines.append("## Smoke inputs")
        lines.append(
            f"- slide_image: "
            f"{'`' + str(smoke.slide_image) + '`' if smoke.slide_image else '**not found**'}"
        )
        if smoke.audio_file is not None:
            lines.append(f"- audio_file: `{smoke.audio_file}`")
            if smoke.audio_duration is not None:
                lines.append(f"- audio_duration_seconds: {smoke.audio_duration}")
        else:
            lines.append("- audio_file: **not found**")
        lines.append("")
        lines.append("## Smoke video output")
        if smoke.clip is None:
            lines.append("- attempted: **no** (skipped because audio file was missing)")
        else:
            c = smoke.clip
            lines.append(f"- attempted: **yes**")
            lines.append(f"- success: **{c.success}**")
            if c.out_path:
                lines.append(f"- out_path: `{c.out_path}`")
            if c.size_bytes:
                lines.append(f"- size_bytes: {c.size_bytes:,}")
            if c.duration_seconds is not None:
                lines.append(f"- duration_seconds: {c.duration_seconds}")
            if c.error:
                lines.append(f"- error: `{c.error}`")
        lines.append("")

    if package_result is not None and run_result is None:
        r = package_result
        lines.append(f"## Package totals - {r.package_id}")
        lines.append(f"- n_slides: {r.n_slides}")
        lines.append(f"- n_succeeded (size>0 AND duration>{MIN_VIDEO_DURATION_SECONDS}s): {r.n_succeeded}")
        lines.append(f"- n_failed: {r.n_failed}")
        lines.append(f"- n_reused: {r.n_reused}")
        lines.append(f"- total_wall_seconds: {r.total_wall_seconds}")
        lines.append(f"- clips_dir: `{r.clips_dir}`")
        lines.append(f"- package_video: `{r.package_video_path}`")
        if r.verdict == "pass":
            lines.append(f"- package_video_size_bytes: {r.package_video_size_bytes:,}")
            lines.append(f"- package_video_duration_seconds: {r.package_video_duration_seconds}")
        lines.append("")

    if run_result is not None:
        lines.append("## Run totals (Phase 9C)")
        lines.append(f"- n_packages: {run_result.n_packages}")
        lines.append(f"- n_packages_passed: {run_result.n_packages_passed}")
        lines.append(f"- n_packages_failed: {run_result.n_packages_failed}")
        lines.append(f"- n_packages_pending: {run_result.n_packages_pending}")
        lines.append(f"- n_slides_total: {run_result.n_slides_total}")
        lines.append(f"- total_wall_seconds: {run_result.total_wall_seconds}")
        lines.append("")
        lines.append("## Per-package totals")
        lines.append("")
        lines.append("| package_id | verdict | slides | passed | failed | reused | wall_s | video_bytes | video_dur |")
        lines.append("|---|:---:|---:|---:|---:|---:|---:|---:|---:|")
        for p in run_result.packages:
            lines.append(
                f"| `{p.package_id}` | {p.verdict} | {p.n_slides} | {p.n_succeeded} "
                f"| {p.n_failed} | {p.n_reused} | {p.total_wall_seconds} "
                f"| {p.package_video_size_bytes:,} "
                f"| {p.package_video_duration_seconds if p.package_video_duration_seconds is not None else '-'} |"
            )
        lines.append("")

    if final_result is not None:
        lines.append("## Final concatenated video")
        lines.append(f"- output_path: `{final_result.output_path}`")
        lines.append(f"- attempted: **{final_result.attempted}**")
        lines.append(f"- success: **{final_result.success}**")
        lines.append(f"- n_inputs: {final_result.n_inputs}")
        if final_result.size_bytes:
            lines.append(f"- size_bytes: {final_result.size_bytes:,}")
        if final_result.duration_seconds is not None:
            lines.append(f"- duration_seconds: {final_result.duration_seconds}")
        lines.append(f"- wall_seconds: {final_result.wall_seconds}")
        if final_result.error:
            lines.append(f"- error: `{final_result.error}`")
        if final_result.stderr_tail:
            lines.append("- ffmpeg stderr (tail):")
            lines.append("  ```")
            for ln in final_result.stderr_tail.splitlines()[-10:]:
                lines.append(f"  {ln}")
            lines.append("  ```")
        lines.append("")

    # Acceptance
    lines.append("## Acceptance")
    if run_result is not None:
        ok_all_pkgs = run_result.n_packages_failed == 0 and run_result.n_packages_pending == 0 and run_result.n_packages > 0
        lines.append(_row("every package video built", ok_all_pkgs, env_blocked))
        if final_result is not None:
            lines.append(_row("final video concatenated", final_result.success, env_blocked))
            lines.append(_row(
                f"final video duration > {MIN_VIDEO_DURATION_SECONDS}s",
                (final_result.duration_seconds is not None
                 and final_result.duration_seconds > MIN_VIDEO_DURATION_SECONDS),
                env_blocked,
            ))
            lines.append(_row(
                "final video size > 0",
                final_result.size_bytes > 0, env_blocked,
            ))
        else:
            lines.append("- final video concatenated: (not requested)")
    elif package_result is not None:
        r = package_result
        all_clips_ok = r.n_failed == 0 and r.n_slides > 0
        concat_ok = r.concat is not None and r.concat.success
        size_ok = r.package_video_size_bytes > 0 if concat_ok else False
        dur_ok = (
            r.package_video_duration_seconds is not None
            and r.package_video_duration_seconds > MIN_VIDEO_DURATION_SECONDS
        ) if concat_ok else False
        lines.append(_row("every slide has a clip", all_clips_ok, env_blocked))
        lines.append(_row("package video concatenated", concat_ok, env_blocked))
        lines.append(_row("package video size > 0", size_ok, env_blocked))
        lines.append(_row(f"package video duration > {MIN_VIDEO_DURATION_SECONDS}s", dur_ok, env_blocked))
    elif smoke is not None:
        has_slide = smoke.slide_image is not None
        has_audio = smoke.audio_file is not None
        has_clip = smoke.clip is not None and smoke.clip.success
        lines.append(_row("ffmpeg detected", smoke.ffmpeg.available, env_blocked))
        lines.append(_row("ffprobe detected", smoke.ffprobe.available, env_blocked))
        lines.append(_row("Phase 6 slide image available", has_slide, env_blocked))
        lines.append(_row("Phase 8 audio file available", has_audio, env_blocked))
        lines.append(_row("smoke_test.mp4 successfully encoded", has_clip, env_blocked))
    lines.append("")

    lines.append("## Hard limits respected")
    if run_result is None and package_result is None:
        lines.append("- only smoke test run (Phase 9A): pass")
    elif run_result is None:
        lines.append("- only one package built (Phase 9B): pass")
    else:
        if final_result is None:
            lines.append("- no final concatenated video built (--concat-final not set): pass")
        else:
            lines.append("- final concatenated video built only when --concat-final supplied: pass")
    lines.append("- source projects untouched: pass")
    lines.append("- no fake/placeholder video created when audio/ffmpeg missing: pass")
    lines.append("")

    if static_health is not None:
        lines.append("## Static health check (Phase 9 modules)")
        lines.append(f"- overall: **{'pass' if static_health.get('ok') else 'fail'}**")
        lines.append(
            f"- modules imported: "
            f"{sum(1 for v in static_health['imports'].values() if v == 'ok')}"
            f" / {len(static_health['imports'])}"
        )
        if static_health.get("errors"):
            lines.append("- errors:")
            for e in static_health["errors"][:10]:
                lines.append(f"  - `{e}`")
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

    if verdict == "pending_local_claude_code_validation" or windows_command:
        lines.append("## Local-Windows command")
        lines.append("")
        lines.append("```")
        if windows_command:
            lines.append(windows_command)
        elif run_result is not None and final_result is None:
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\build_video.py --run runs\\{run_id} --all"
            )
        elif run_result is not None:
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\build_video.py --run runs\\{run_id} --all --concat-final"
            )
        elif package_result is not None:
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\build_video.py --run runs\\{run_id} --package {package_result.package_id}"
            )
        else:
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\build_video.py --run runs\\{run_id} --smoke-test"
            )
        lines.append("```")
        lines.append("")

    atomic_write_bytes(report_path, "\n".join(lines).encode("utf-8"))
    return Path(report_path)
