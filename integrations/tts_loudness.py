"""tts_loudness - loudness measurement + normalization (ffmpeg loudnorm).

Edge/HQ TTS output is often quiet (~-24 dB mean). This module measures loudness
and normalizes to a target LUFS. ffmpeg is only invoked when ``execute=True`` —
by default these functions return the planned command WITHOUT running ffmpeg
(so they are safe to import/plan in phases where ffmpeg must not run).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

TARGET_LUFS = -16.0
TARGET_TP = -1.5      # true peak ceiling (dBTP)
TARGET_LRA = 11.0


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def measure_mean_volume(path, *, execute: bool = False):
    """Return {'cmd':..., 'mean_db':float|None, 'max_db':float|None, 'ran':bool}.
    Runs ffmpeg volumedetect (analysis only, null output) ONLY if execute=True."""
    path = str(path)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect", "-f", "null", "-"]
    out = {"cmd": " ".join(cmd), "mean_db": None, "max_db": None, "ran": False}
    if not execute:
        return out
    if not ffmpeg_available():
        out["error"] = "ffmpeg not available"; return out
    p = subprocess.run(cmd, capture_output=True, text=True)
    txt = p.stderr
    m = re.search(r"mean_volume:\s*(-?\d+\.?\d*) dB", txt)
    mx = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", txt)
    out["mean_db"] = float(m.group(1)) if m else None
    out["max_db"] = float(mx.group(1)) if mx else None
    out["ran"] = True
    return out


def normalize_to_lufs(in_path, out_path, *, target_lufs: float = TARGET_LUFS,
                      tp: float = TARGET_TP, lra: float = TARGET_LRA, execute: bool = False):
    """Loudness-normalize in_path -> out_path via ffmpeg loudnorm. Returns the planned
    command; runs ffmpeg ONLY if execute=True (never overwrites in_path)."""
    in_path, out_path = str(in_path), str(out_path)
    filt = f"loudnorm=I={target_lufs}:TP={tp}:LRA={lra}"
    cmd = ["ffmpeg", "-y", "-i", in_path, "-af", filt, "-ar", "24000", "-ac", "1", out_path]
    res = {"cmd": " ".join(cmd), "target_lufs": target_lufs, "ran": False}
    if not execute:
        return res
    if not ffmpeg_available():
        res["error"] = "ffmpeg not available"; return res
    if Path(in_path) == Path(out_path):
        res["error"] = "refusing to overwrite input"; return res
    p = subprocess.run(cmd, capture_output=True, text=True)
    res["ran"] = (p.returncode == 0)
    res["error"] = "" if p.returncode == 0 else p.stderr[-200:]
    return res
