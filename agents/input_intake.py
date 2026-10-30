"""input_intake - Stage 0.

Resolves the user's `convert file N` request to a concrete PDF or PPTX in
input/, allocates a run_id, builds the run folder skeleton, and copies the
source into runs/<run_id>/source/ for reproducibility.

Per docs/PIPELINE_SPEC.md Stage 0 and docs/OUTPUT_ARTIFACT_SPEC.md sections
1-2.

Public API:
  intake(file_arg, project_root) -> IntakeResult

Phase 2 supported PDF only; Phase 10 added PPTX. The two are routed
downstream by `agents.extraction_agent` via `integrations.extraction_adapter`.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Phase 2 + Phase 10. Other formats remain unsupported (raise ValueError).
SUPPORTED_EXTS_PHASE2 = (".pdf", ".pptx")
SUPPORTED_INPUT_EXTS = SUPPORTED_EXTS_PHASE2  # alias for clarity

RUN_ID_BASENAME_MAX = 40


@dataclass(frozen=True)
class IntakeResult:
    file_arg: str
    source_path: Path
    source_copy: Path
    source_sha256: str
    source_type: str
    run_id: str
    run_dir: Path


def _sanitize_basename(name: str) -> str:
    """Lowercase, keep [a-z0-9_-], collapse other chars to '_', trim to 40."""
    lowered = name.lower()
    safe = re.sub(r"[^a-z0-9_-]+", "_", lowered)
    safe = re.sub(r"_+", "_", safe).strip("_-")
    if not safe:
        safe = "input"
    return safe[:RUN_ID_BASENAME_MAX]


def _next_run_index(runs_dir: Path) -> int:
    """Find the next NNN prefix not yet in use under runs/."""
    if not runs_dir.exists():
        return 1
    max_n = 0
    pat = re.compile(r"^(\d{3})_")
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        m = pat.match(child.name)
        if m:
            try:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return max_n + 1


def _resolve_input_file(input_dir: Path, file_arg: str) -> Path:
    """Locate the user's file by argument.

    Resolution order:
      1. Exact path match if file_arg looks like one (e.g., '1.pdf', '1.pptx').
      2. Glob input/<arg>.* (any extension).
    """
    if "." in file_arg:
        direct = input_dir / file_arg
        if direct.exists() and direct.is_file():
            return direct

    candidates = sorted(input_dir.glob(f"{file_arg}.*"))
    if not candidates:
        raise FileNotFoundError(
            f"No file matching input/{file_arg}.* found. "
            f"Available in input/: "
            + ", ".join(sorted(p.name for p in input_dir.iterdir() if p.is_file()))
            or "(empty)"
        )

    # Per AUTOPILOT_COMMAND_SPEC.md section 3: PDF > PPTX > others.
    def priority(p: Path) -> int:
        order = {".pdf": 0, ".pptx": 1}
        return order.get(p.suffix.lower(), 99)

    candidates.sort(key=priority)
    return candidates[0]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def intake(
    file_arg: str,
    project_root: Path,
    *,
    now: Optional[datetime] = None,
) -> IntakeResult:
    """Run Stage 0 and return a structured IntakeResult.

    Side effects:
      - Creates runs/<run_id>/ with subfolders.
      - Copies source into runs/<run_id>/source/.

    Raises:
      FileNotFoundError if input file is missing.
      ValueError if file type is unsupported.
    """
    project_root = Path(project_root)
    input_dir = project_root / "input"
    runs_dir = project_root / "runs"

    if not input_dir.exists():
        raise FileNotFoundError(f"input/ folder does not exist: {input_dir}")

    src = _resolve_input_file(input_dir, file_arg)
    ext = src.suffix.lower()
    if ext not in SUPPORTED_INPUT_EXTS:
        raise ValueError(
            f"Unsupported input type {ext!r}. "
            f"Supported: {SUPPORTED_INPUT_EXTS}."
        )
    source_type = ext.lstrip(".")  # 'pdf' or 'pptx'

    now = now or datetime.now()
    ts = now.strftime("%Y%m%d_%H%M")
    n = _next_run_index(runs_dir)
    basename = _sanitize_basename(src.stem)
    run_id = f"{n:03d}_{basename}_{ts}"

    run_dir = runs_dir / run_id
    if run_dir.exists():
        raise FileExistsError(f"run_dir already exists: {run_dir}")

    (run_dir / "source").mkdir(parents=True, exist_ok=False)
    (run_dir / "extracted" / "pages").mkdir(parents=True, exist_ok=False)
    (run_dir / "extracted" / "page_text").mkdir(parents=True, exist_ok=False)
    (run_dir / "QA_REPORTS").mkdir(parents=True, exist_ok=False)
    (run_dir / "state").mkdir(parents=True, exist_ok=False)
    (run_dir / "state" / "snapshots").mkdir(parents=True, exist_ok=False)
    (run_dir / "logs").mkdir(parents=True, exist_ok=False)

    dest_copy = run_dir / "source" / src.name
    shutil.copy2(src, dest_copy)

    sha = _sha256(src)

    return IntakeResult(
        file_arg=file_arg,
        source_path=src,
        source_copy=dest_copy,
        source_sha256=sha,
        source_type=source_type,
        run_id=run_id,
        run_dir=run_dir,
    )
