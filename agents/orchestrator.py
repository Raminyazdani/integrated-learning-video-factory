"""orchestrator — minimal Phase 2 orchestrator.

Single state writer. Atomic writes only. Stages are integer-indexed per
docs/PIPELINE_SPEC.md; the user-facing names are kept in
`completed_stage_names` per schemas/run_state.schema.json.

Phase 2 scope:
  - Stage 0: input_intake
  - Stage 1: pdf_extraction
  - --stop-after extraction halts cleanly after Stage 1.

Later phases extend this orchestrator; the public entry point `Orchestrator.run`
will stay stable.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.extraction_agent import run_extraction
from agents.input_intake import IntakeResult, intake
from integrations.atomic_io import atomic_write_json, iso_now, read_json

RUN_STATE_SCHEMA_VERSION = "1.0.0"

# Stage table — name <-> integer index, per pipeline spec.
STAGES = [
    (0, "input_intake"),
    (1, "pdf_extraction"),
]


@dataclass
class OrchestratorResult:
    intake: IntakeResult
    extraction: object  # ExtractionResult; loose-typed to avoid circular imports
    run_state_path: Path
    project_state_path: Path


def _now_iso() -> str:
    return iso_now()


def _stage_int_for(name: str) -> int:
    for i, n in STAGES:
        if n == name:
            return i
    raise ValueError(f"unknown stage name: {name!r}")


class Orchestrator:
    """Phase 2 orchestrator. The only component that writes run_state.json."""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root).resolve()
        self.runs_dir = self.project_root / "runs"
        self.root_state = self.project_root / "state" / "run_state.json"

    # ------------------------------------------------------------------ state
    def _initial_run_state(self, ir: IntakeResult) -> dict:
        now = _now_iso()
        return {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "run_id": ir.run_id,
            "source_file": str(ir.source_path.as_posix()),
            "source_type": ir.source_type,
            "created_at": now,
            "updated_at": now,
            "current_stage": 0,
            "completed_stages": [],
            "completed_stage_names": [],
            "status": "in_progress",
            "assumptions": [],
            "errors": [],
            "snapshots": [],
        }

    def _project_pointer(self, ir: IntakeResult, run_state: dict) -> dict:
        """Top-level state/run_state.json — pointer to the latest run."""
        return {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "run_id": ir.run_id,
            "latest_run_id": ir.run_id,
            "source_file": str(ir.source_path.as_posix()),
            "source_type": ir.source_type,
            "created_at": run_state["created_at"],
            "updated_at": run_state["updated_at"],
            "current_stage": run_state["current_stage"],
            "completed_stages": list(run_state["completed_stages"]),
            "completed_stage_names": list(run_state["completed_stage_names"]),
            "status": run_state["status"],
        }

    def _write_run_state(self, run_dir: Path, run_state: dict) -> Path:
        path = run_dir / "state" / "run_state.json"
        snap_dir = run_dir / "state" / "snapshots"
        atomic_write_json(path, run_state, snapshot_dir=snap_dir, snapshot_keep=10)
        return path

    def _write_project_pointer(self, pointer: dict) -> Path:
        snap_dir = self.project_root / "state" / "snapshots"
        atomic_write_json(
            self.root_state, pointer, snapshot_dir=snap_dir, snapshot_keep=10
        )
        return self.root_state

    def _mark_stage_complete(self, run_state: dict, stage_name: str) -> None:
        idx = _stage_int_for(stage_name)
        if idx not in run_state["completed_stages"]:
            run_state["completed_stages"].append(idx)
            run_state["completed_stage_names"].append(stage_name)
        # next stage cursor
        run_state["current_stage"] = max(run_state["current_stage"], idx + 1)
        run_state["updated_at"] = _now_iso()

    # -------------------------------------------------------------------- run
    def run(
        self,
        file_arg: str,
        *,
        stop_after: Optional[str] = None,
        dpi: int = 200,
    ) -> OrchestratorResult:
        """Execute Phase 2 stages and persist state after each.

        Args:
          file_arg:   the user's argument (e.g. "1" -> input/1.pdf).
          stop_after: if "extraction" (or "pdf_extraction"), stop after Stage 1.
                      For Phase 2 this is effectively the only sensible value.
          dpi:        render DPI.
        """
        normalize_stop = {
            None: None,
            "intake": "input_intake",
            "input_intake": "input_intake",
            "extraction": "pdf_extraction",
            "pdf_extraction": "pdf_extraction",
        }
        if stop_after not in normalize_stop:
            raise ValueError(
                f"Unsupported --stop-after value for Phase 2: {stop_after!r}. "
                "Use one of: intake, extraction."
            )
        stop_after = normalize_stop[stop_after]

        # ---- Stage 0: intake ----
        print(f"[Stage 0] Input intake: file_arg={file_arg!r}")
        ir = intake(file_arg, self.project_root)
        print(f"[Stage 0] run_id = {ir.run_id}")
        print(f"[Stage 0] source = {ir.source_path}")
        print(f"[Stage 0] copy   = {ir.source_copy}")

        run_state = self._initial_run_state(ir)
        self._mark_stage_complete(run_state, "input_intake")
        run_state_path = self._write_run_state(ir.run_dir, run_state)
        self._write_project_pointer(self._project_pointer(ir, run_state))

        if stop_after == "input_intake":
            print("[Stop] Halt after Stage 0 as requested.")
            return OrchestratorResult(
                intake=ir,
                extraction=None,
                run_state_path=run_state_path,
                project_state_path=self.root_state,
            )

        # ---- Stage 1: pdf_extraction ----
        print(f"[Stage 1] PDF extraction (dpi={dpi}) ...")

        def _progress(i: int, total: int) -> None:
            # Print on multiples of 5 or on first/last to keep output quiet.
            if i == 1 or i == total or i % 5 == 0:
                print(f"  page {i}/{total}", file=sys.stderr)

        try:
            er = run_extraction(
                run_dir=ir.run_dir,
                run_id=ir.run_id,
                source_path=ir.source_path,
                source_copy=ir.source_copy,
                source_sha256=ir.source_sha256,
                source_type=ir.source_type,
                dpi=dpi,
                on_page=_progress,
            )
        except Exception as e:
            # Record failure in state and rethrow.
            run_state["errors"].append(
                {
                    "stage": 1,
                    "message": f"pdf_extraction failed: {e!r}",
                    "timestamp": _now_iso(),
                }
            )
            run_state["status"] = "failed"
            run_state["updated_at"] = _now_iso()
            self._write_run_state(ir.run_dir, run_state)
            self._write_project_pointer(self._project_pointer(ir, run_state))
            raise

        self._mark_stage_complete(run_state, "pdf_extraction")
        run_state["status"] = "in_progress"  # Phase 2 doesn't finish the run
        if stop_after == "pdf_extraction":
            run_state["status"] = "halted"  # explicit stop-after = clean halt
        run_state_path = self._write_run_state(ir.run_dir, run_state)
        self._write_project_pointer(self._project_pointer(ir, run_state))

        print(
            f"[Stage 1] Done. pages={er.page_count} "
            f"with_text={er.pages_with_text} empty={er.pages_empty} "
            f"failed={er.pages_failed}"
        )
        print(f"[Stage 1] manifest: {er.manifest_path}")
        print(f"[Stage 1] report  : {er.report_path}")

        return OrchestratorResult(
            intake=ir,
            extraction=er,
            run_state_path=run_state_path,
            project_state_path=self.root_state,
        )
