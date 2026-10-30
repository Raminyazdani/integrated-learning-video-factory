"""cost_tracker - lightweight per-stage wall-time + unit ledger.

Phase 11B introduces this module so every long-running stage (TTS,
ffmpeg, LibreOffice export, pdflatex) can record:
  * wall_seconds      - how long it took
  * units_kind        - "tts_chars" | "video_seconds" | "pdf_pages" |
                        "slide_count" | "subprocess_seconds" | "other"
  * units             - how many of those units were produced
  * provider / engine - optional string (e.g. "edge", "libx264", "xelatex")

The ledger lives at:
  state/cost.json

Schema:
  {
    "schema_version": "1.0.0",
    "generated_at": "...",
    "runs": {
      "<run_id>": {
        "stages": [
          {
            "stage_id":          "stage_7b_audio_all",
            "timestamp":         "<iso>",
            "wall_seconds":      234.5,
            "units_kind":        "tts_chars",
            "units":             49166,
            "provider":          "edge",
            "engine":            null,
            "notes":             []
          },
          ...
        ],
        "totals": {
            "wall_seconds":      ...,
            "tts_chars":         ...,
            "video_seconds":     ...
        }
      }
    }
  }

Cost estimates (rough USD-equivalent only, for transparency - the TTS chain
is anonymous and free as of writing):

    tts_chars     -> $0   (Edge TTS is free)
    video_seconds -> compute-only; not billable
    pdf_pages     -> compute-only

The function ``estimate_cost_usd`` returns ``0.0`` for the MVP chain. The
field exists so future paid providers (OpenAI, ElevenLabs) can plug in.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations.atomic_io import atomic_write_json, iso_now, read_json


SCHEMA_VERSION = "1.0.0"
COST_JSON_REL = "state/cost.json"


@dataclass
class StageCost:
    stage_id: str
    timestamp: str = ""
    wall_seconds: float = 0.0
    units_kind: str = "other"
    units: float = 0.0
    provider: Optional[str] = None
    engine: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    estimated_usd: float = 0.0


def estimate_cost_usd(*, units_kind: str, units: float,
                      provider: Optional[str] = None) -> float:
    """Map (units_kind, units, provider) to a rough USD figure.

    Free providers return 0.0. Paid providers (OpenAI, ElevenLabs, Azure)
    use rough public-rate estimates. Always intended as a transparency
    figure, not a billing source of truth.
    """
    if units <= 0:
        return 0.0
    if units_kind == "tts_chars":
        if provider in (None, "", "edge", "gtts", "pyttsx3"):
            return 0.0
        # Rough public rates per 1M chars (2026):
        per_1m = {
            "openai": 15.0,
            "azure": 16.0,
            "elevenlabs": 22.0,
            "google_cloud": 16.0,
            "gemini_tts": 15.0,
        }.get(provider, 0.0)
        return round(units / 1_000_000.0 * per_1m, 4)
    # video/pdf/slide stages have no per-unit USD cost in this pipeline.
    return 0.0


def _cost_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / COST_JSON_REL


def _load_ledger(project_root: Path) -> Dict[str, Any]:
    p = _cost_path(project_root)
    if not p.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_now(),
            "runs": {},
        }
    try:
        data = read_json(p)
        if not isinstance(data, dict):
            return {"schema_version": SCHEMA_VERSION, "generated_at": iso_now(), "runs": {}}
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("runs", {})
        return data
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "generated_at": iso_now(), "runs": {}}


def _save_ledger(project_root: Path, ledger: Dict[str, Any]) -> Path:
    p = _cost_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["generated_at"] = iso_now()
    atomic_write_json(p, ledger)
    return p


def _recompute_totals(stages: List[Dict[str, Any]]) -> Dict[str, float]:
    totals = {"wall_seconds": 0.0, "tts_chars": 0.0,
              "video_seconds": 0.0, "estimated_usd": 0.0}
    for s in stages:
        totals["wall_seconds"] += float(s.get("wall_seconds") or 0.0)
        kind = s.get("units_kind") or "other"
        if kind == "tts_chars":
            totals["tts_chars"] += float(s.get("units") or 0.0)
        elif kind == "video_seconds":
            totals["video_seconds"] += float(s.get("units") or 0.0)
        totals["estimated_usd"] += float(s.get("estimated_usd") or 0.0)
    return {k: round(v, 4) for k, v in totals.items()}


def record_stage(
    *,
    project_root: Path,
    run_id: str,
    stage_id: str,
    wall_seconds: float,
    units_kind: str = "other",
    units: float = 0.0,
    provider: Optional[str] = None,
    engine: Optional[str] = None,
    notes: Optional[List[str]] = None,
) -> StageCost:
    """Append one stage entry to state/cost.json and refresh totals."""
    cost = StageCost(
        stage_id=stage_id,
        timestamp=iso_now(),
        wall_seconds=round(float(wall_seconds), 3),
        units_kind=units_kind,
        units=float(units),
        provider=provider,
        engine=engine,
        notes=list(notes or []),
        estimated_usd=estimate_cost_usd(
            units_kind=units_kind, units=units, provider=provider
        ),
    )
    ledger = _load_ledger(project_root)
    run_block = ledger["runs"].setdefault(run_id, {"stages": [], "totals": {}})
    run_block.setdefault("stages", []).append(asdict(cost))
    run_block["totals"] = _recompute_totals(run_block["stages"])
    _save_ledger(project_root, ledger)
    return cost


def get_summary(*, project_root: Path, run_id: str) -> Dict[str, Any]:
    """Return the run's stages list + totals (empty if no entries yet)."""
    ledger = _load_ledger(project_root)
    return ledger.get("runs", {}).get(run_id, {"stages": [], "totals": {}})
