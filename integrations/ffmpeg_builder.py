"""ffmpeg_builder - thin Python wrapper around the ffmpeg CLI.

Phase 9A scope
--------------
This module is intentionally minimal:
  * ``detect_ffmpeg()`` - is the binary on PATH? what version?
  * ``detect_ffprobe()`` - same for ffprobe (used for duration checks).
  * ``probe_duration_seconds(path)`` - read a media duration with ffprobe.
  * ``make_slide_clip(image, audio, out_path, ...)`` - one slide PNG +
    one audio file -> one MP4 (libx264 + aac, faststart).

Phase 9B/9C will build on these helpers to render per-package and final
videos. The smoke test in ``agents.video_builder_agent`` only calls
``detect_ffmpeg`` and (optionally) ``make_slide_clip``.

No fake artifacts. ``make_slide_clip`` writes to a tmp file in the same
directory and atomically renames into place; failure leaves no partial
output.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class FFmpegInfo:
    binary: Optional[str] = None       # absolute path to ffmpeg
    version: Optional[str] = None      # e.g. "4.4.2"
    full_banner: Optional[str] = None  # first line of `ffmpeg -version`
    available: bool = False
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class FFprobeInfo:
    binary: Optional[str] = None
    version: Optional[str] = None
    available: bool = False
    error: Optional[str] = None


def detect_ffmpeg() -> FFmpegInfo:
    """Locate ffmpeg on PATH and capture its version."""
    info = FFmpegInfo()
    path = shutil.which("ffmpeg")
    if not path:
        info.error = "ffmpeg not found on PATH"
        return info
    info.binary = path
    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        info.error = f"ffmpeg -version failed: {exc!r}"
        return info
    if out.returncode != 0:
        info.error = (
            f"ffmpeg -version returned {out.returncode}: "
            f"{(out.stderr or out.stdout)[:200]!r}"
        )
        return info
    info.full_banner = (out.stdout or "").splitlines()[0] if out.stdout else None
    if info.full_banner and " version " in info.full_banner:
        info.version = info.full_banner.split(" version ", 1)[1].split(" ", 1)[0]
    info.available = True
    return info


def detect_ffprobe() -> FFprobeInfo:
    info = FFprobeInfo()
    path = shutil.which("ffprobe")
    if not path:
        info.error = "ffprobe not found on PATH"
        return info
    info.binary = path
    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        info.error = f"ffprobe -version failed: {exc!r}"
        return info
    if out.returncode != 0:
        info.error = (
            f"ffprobe -version returned {out.returncode}: "
            f"{(out.stderr or out.stdout)[:200]!r}"
        )
        return info
    if out.stdout and " version " in out.stdout.splitlines()[0]:
        info.version = out.stdout.splitlines()[0].split(" version ", 1)[1].split(" ", 1)[0]
    info.available = True
    return info


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def probe_duration_seconds(path: Path, *, ffprobe_binary: Optional[str] = None) -> Optional[float]:
    """Return media duration (s) via ffprobe, or None if it can't be read."""
    path = Path(path)
    if not path.is_file():
        return None
    binary = ffprobe_binary or shutil.which("ffprobe")
    if not binary:
        return None
    try:
        out = subprocess.run(
            [
                binary,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def probe_format_json(path: Path, *, ffprobe_binary: Optional[str] = None) -> dict:
    """Return ``ffprobe -show_format -show_streams -of json`` as a dict.

    Returns ``{}`` on any failure; never raises.
    """
    path = Path(path)
    binary = ffprobe_binary or shutil.which("ffprobe")
    if not (binary and path.is_file()):
        return {}
    try:
        out = subprocess.run(
            [
                binary,
                "-v", "error",
                "-show_format",
                "-show_streams",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return {}
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


@dataclass
class ClipResult:
    success: bool = False
    out_path: Optional[Path] = None
    size_bytes: int = 0
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    wall_seconds: float = 0.0
    stderr_tail: Optional[str] = None
    cmd: List[str] = field(default_factory=list)


def make_slide_clip(
    image: Path,
    audio: Path,
    out_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    audio_bitrate: str = "128k",
    pix_fmt: str = "yuv420p",
    fps: int = 30,
    ffmpeg_binary: Optional[str] = None,
    timeout_seconds: int = 120,
) -> ClipResult:
    """Render a single slide image + audio file into one MP4.

    The image is looped for the duration of the audio (``-shortest`` halts
    encoding when the audio stream ends). Output uses ``libx264 +tune
    stillimage`` for crisp, low-bitrate frames and AAC audio. Writes to a
    tmp file then ``os.replace`` for atomicity.
    """
    import time as _time

    image = Path(image)
    audio = Path(audio)
    out_path = Path(out_path)
    result = ClipResult()
    if not image.is_file():
        result.error = f"image not found: {image}"
        return result
    if not audio.is_file():
        result.error = f"audio not found: {audio}"
        return result
    binary = ffmpeg_binary or shutil.which("ffmpeg")
    if not binary:
        result.error = "ffmpeg not on PATH"
        return result

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".tmp.",
        suffix=".mp4",
        dir=str(out_path.parent),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    # libx264 needs even dimensions; ensure that with scale filter pad.
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    cmd = [
        binary,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-loop", "1",
        "-i", str(image),
        "-i", str(audio),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", video_codec,
        "-tune", "stillimage",
        "-pix_fmt", pix_fmt,
        "-c:a", audio_codec,
        "-b:a", audio_bitrate,
        "-shortest",
        "-movflags", "+faststart",
        str(tmp_path),
    ]
    result.cmd = list(cmd)
    started = _time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = f"ffmpeg timeout after {timeout_seconds}s: {exc!r}"
        result.wall_seconds = round(_time.time() - started, 3)
        return result
    except Exception as exc:  # noqa: BLE001
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = f"ffmpeg invocation failed: {exc!r}"
        result.wall_seconds = round(_time.time() - started, 3)
        return result

    result.wall_seconds = round(_time.time() - started, 3)
    if proc.returncode != 0:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = f"ffmpeg returncode={proc.returncode}"
        result.stderr_tail = (proc.stderr or "")[-400:]
        return result

    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = "ffmpeg produced no output (size 0)"
        result.stderr_tail = (proc.stderr or "")[-400:]
        return result

    try:
        os.replace(tmp_path, out_path)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = f"atomic move failed: {exc!r}"
        return result

    result.success = True
    result.out_path = out_path
    result.size_bytes = out_path.stat().st_size
    result.duration_seconds = probe_duration_seconds(out_path)
    return result
