"""atomic_io — safe atomic writes with optional snapshot rotation.

Pattern adapted from exam_tutor/ARCHITECTURE.mmd (atomic_io.py reference).
Source projects are read-only; this is a fresh port written for this project.

Algorithm (per docs/CONTEXT_MANAGEMENT_AND_STATE.md §4):
  1. write new content to <path>.tmp.<pid>.<rand>
  2. fsync the tmp file (best-effort, ignored on Windows where unsupported)
  3. os.replace(tmp, path)  — atomic on POSIX and Windows
  4. (optional) snapshot previous file content into <snapshot_dir>/<basename>.snap.<TS>.json
  5. rotate snapshots to keep last N

Public API:
  atomic_write_bytes(path, data, *, snapshot_dir=None, snapshot_keep=10)
  atomic_write_text(path, text, *, encoding="utf-8", snapshot_dir=None, snapshot_keep=10)
  atomic_write_json(path, obj, *, indent=2, snapshot_dir=None, snapshot_keep=10)
  read_json(path) -> dict
"""
from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, Path]


def _ts_for_snapshot() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _snapshot_existing(path: Path, snapshot_dir: Path) -> Optional[Path]:
    """Move current file (if any) into snapshot_dir/<stem>.snap.<TS><suffix>.
    Returns the snapshot path or None if no original file existed.
    """
    if not path.exists():
        return None
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snap_name = f"{path.stem}.snap.{_ts_for_snapshot()}{path.suffix}"
    snap_path = snapshot_dir / snap_name
    # Use copy (not move) so the live file still exists for the os.replace step
    shutil.copy2(path, snap_path)
    return snap_path


def _rotate_snapshots(snapshot_dir: Path, base_stem: str, keep: int) -> None:
    if keep < 0 or not snapshot_dir.exists():
        return
    matches = sorted(
        snapshot_dir.glob(f"{base_stem}.snap.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in matches[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def atomic_write_bytes(
    path: PathLike,
    data: bytes,
    *,
    snapshot_dir: Optional[PathLike] = None,
    snapshot_keep: int = 10,
) -> Path:
    """Atomically write bytes to `path`. Returns the resolved Path."""
    p = Path(path)
    _ensure_parent(p)

    if snapshot_dir is not None:
        _snapshot_existing(p, Path(snapshot_dir))

    # Write to tmp in same directory (so os.replace is atomic across the same FS)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=p.name + ".tmp.",
        suffix=f".{os.getpid()}.{random.randint(0, 0xFFFF):04x}",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may fail on some FS / platforms; best-effort.
                pass
        os.replace(tmp_name, p)
    except Exception:
        # Clean up tmp on failure
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    if snapshot_dir is not None:
        _rotate_snapshots(Path(snapshot_dir), p.stem, snapshot_keep)

    return p


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    snapshot_dir: Optional[PathLike] = None,
    snapshot_keep: int = 10,
) -> Path:
    return atomic_write_bytes(
        path,
        text.encode(encoding),
        snapshot_dir=snapshot_dir,
        snapshot_keep=snapshot_keep,
    )


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    indent: int = 2,
    snapshot_dir: Optional[PathLike] = None,
    snapshot_keep: int = 10,
) -> Path:
    payload = json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False)
    return atomic_write_text(
        path,
        payload + "\n",
        snapshot_dir=snapshot_dir,
        snapshot_keep=snapshot_keep,
    )


def read_json(path: PathLike) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Convenience: simple ISO-8601 timestamp helper used by emitters.
def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
