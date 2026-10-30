"""ffmpeg_concat - per-package and full-deck MP4 concatenation.

Phase 9A scope
--------------
This file ships **alongside** ``ffmpeg_builder.py`` so the Phase 9 import
graph is complete. Phase 9A only verifies the module imports and exposes
the planned API; full use lands in Phase 9B (per-package concat) and
Phase 9C (final-video concat).

Why a separate module
---------------------
The concat-demuxer approach (``ffmpeg -f concat -safe 0 -i list.txt -c
copy out.mp4``) requires:
  * uniform codec parameters across all clips (which we control in
    ``ffmpeg_builder.make_slide_clip``);
  * a tmp playlist file in a known location;
  * different error handling than per-clip rendering.

Keeping it separate avoids a 600-line builder file later.

Public API (provisional)
------------------------
``concat_clips(clip_paths, out_path, *, ffmpeg_binary=None) -> ConcatResult``
    Concatenate MP4 clips using the concat demuxer with stream-copy (no
    re-encode). Atomic write.

``concat_clips_reencode(clip_paths, out_path, ...) -> ConcatResult``
    Fallback path that re-encodes (slower but tolerates parameter drift
    between input clips).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .ffmpeg_builder import probe_duration_seconds


@dataclass
class ConcatResult:
    success: bool = False
    out_path: Optional[Path] = None
    size_bytes: int = 0
    duration_seconds: Optional[float] = None
    n_inputs: int = 0
    error: Optional[str] = None
    wall_seconds: float = 0.0
    stderr_tail: Optional[str] = None
    cmd: List[str] = field(default_factory=list)


def _write_playlist(playlist_path: Path, clip_paths: List[Path]) -> None:
    """Write the concat-demuxer playlist file."""
    lines = []
    for p in clip_paths:
        # Escape single quotes per ffmpeg concat demuxer rules.
        escaped = str(p).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_clips(
    clip_paths: List[Path],
    out_path: Path,
    *,
    ffmpeg_binary: Optional[str] = None,
    timeout_seconds: int = 300,
) -> ConcatResult:
    """Concatenate MP4 clips using the concat demuxer (no re-encode).

    All input clips must share codec parameters (this is the case when
    they're all produced by ``ffmpeg_builder.make_slide_clip`` with the
    same arguments).
    """
    result = ConcatResult(n_inputs=len(clip_paths))
    paths = [Path(p) for p in clip_paths]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        result.error = f"missing input clips: {missing[:5]}"
        return result
    if not paths:
        result.error = "no input clips"
        return result

    binary = ffmpeg_binary or shutil.which("ffmpeg")
    if not binary:
        result.error = "ffmpeg not on PATH"
        return result

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = out_path.parent
    playlist_fd, playlist_name = tempfile.mkstemp(
        prefix=out_path.name + ".concat.",
        suffix=".txt",
        dir=str(tmp_dir),
    )
    os.close(playlist_fd)
    playlist_path = Path(playlist_name)
    try:
        _write_playlist(playlist_path, paths)
    except OSError as exc:
        result.error = f"playlist write failed: {exc!r}"
        try:
            playlist_path.unlink(missing_ok=True)
        except OSError:
            pass
        return result

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".tmp.",
        suffix=".mp4",
        dir=str(tmp_dir),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    cmd = [
        binary,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(playlist_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp_path),
    ]
    result.cmd = list(cmd)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        result.error = f"ffmpeg invocation failed: {exc!r}"
        for p in (tmp_path, playlist_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        result.wall_seconds = round(time.time() - started, 3)
        return result
    finally:
        try:
            playlist_path.unlink(missing_ok=True)
        except OSError:
            pass

    result.wall_seconds = round(time.time() - started, 3)
    if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.error = f"ffmpeg concat returncode={proc.returncode}"
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


def concat_clips_reencode(*args, **kwargs) -> ConcatResult:
    """Re-encode fallback for clips that don't share codec parameters.

    Not yet implemented - reserved for Phase 9B/9C when we discover real
    parameter drift between clips. Phase 9A only needs the symbol to exist.
    """
    raise NotImplementedError(
        "concat_clips_reencode is reserved for Phase 9B/9C; use "
        "concat_clips() when inputs were all built by make_slide_clip()."
    )
