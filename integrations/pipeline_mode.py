"""pipeline_mode - explicit pipeline execution modes + fail-closed policy.

Modes: manual_agent | llm_dry_run | llm_paid | mechanical_fallback.

This module is the single gate that prevents weak Python heuristics from silently
acting as LLM/manual "understanding". Understanding stages (visual_review,
content_generation, package_titles) must either:
  - have authored files (manual_agent / llm_dry_run), or
  - run a real LLM with approval+budget+key (llm_paid), or
  - be explicitly marked low-quality (mechanical_fallback, smoke tests only).

No paid call is ever permitted unless mode == llm_paid AND approval/key/budget
are present. Missing/unknown mode fails clearly (or defaults safely to manual_agent).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = PROJECT_ROOT / "configs" / "pipeline_modes.yaml"
_ENV_VAR = "ILVF_PIPELINE_MODE"
_VALID_MODES = ("manual_agent", "llm_dry_run", "llm_paid", "mechanical_fallback")


class PipelineModeError(RuntimeError):
    """Raised when a stage is not allowed under the active mode (fail-closed)."""


def load_mode_config() -> dict:
    """Load configs/pipeline_modes.yaml. Raises a clear error if missing/unparseable."""
    if not _CONFIG_PATH.is_file():
        raise PipelineModeError(f"pipeline_modes.yaml not found at {_CONFIG_PATH}")
    try:
        import yaml  # PyYAML (same as pipeline_config.yaml)
    except Exception as exc:  # pragma: no cover
        raise PipelineModeError(f"PyYAML required to read pipeline_modes.yaml: {exc!r}")
    try:
        cfg = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise PipelineModeError(f"pipeline_modes.yaml is not valid YAML: {exc!r}")
    if "modes" not in cfg:
        raise PipelineModeError("pipeline_modes.yaml missing 'modes' section")
    return cfg


def resolve_mode(explicit: Optional[str] = None, *, config: Optional[dict] = None) -> str:
    """explicit --mode > env ILVF_PIPELINE_MODE > config default_mode > manual_agent.
    Unknown value raises a clear error (does NOT silently fall back)."""
    cfg = config or load_mode_config()
    candidate = explicit or os.environ.get(_ENV_VAR) or cfg.get("default_mode") or "manual_agent"
    candidate = str(candidate).strip()
    if candidate not in _VALID_MODES:
        raise PipelineModeError(
            f"Unknown pipeline mode {candidate!r}. Valid: {', '.join(_VALID_MODES)}."
        )
    if candidate not in cfg.get("modes", {}):
        raise PipelineModeError(f"Mode {candidate!r} not defined in pipeline_modes.yaml.")
    return candidate


def mode_rules(mode: str, *, config: Optional[dict] = None) -> dict:
    cfg = config or load_mode_config()
    return dict(cfg.get("modes", {}).get(mode, {}))


def mode_allows_paid(mode: str, *, config: Optional[dict] = None) -> bool:
    return bool(mode_rules(mode, config=config).get("allow_paid_calls", False))


def mode_marks_low_quality(mode: str, *, config: Optional[dict] = None) -> bool:
    return bool(mode_rules(mode, config=config).get("marks_output_low_quality", False))


def _stage_needs_understanding(stage: str, cfg: dict) -> bool:
    return stage in (cfg.get("llm_understanding_stages") or [])


def assert_stage_allowed(stage: str, mode: str, *, config: Optional[dict] = None) -> None:
    """Raise PipelineModeError if `stage` may not run under `mode`.
    - mechanical_fallback: only smoke-test/deterministic stages; understanding stages
      are allowed but their output is low-quality (caller must mark it).
    - paid-capable stages are allowed to ATTEMPT paid work only in llm_paid (the
      actual paid guard is fail_closed_if_stage_needs_llm)."""
    cfg = config or load_mode_config()
    if mode not in _VALID_MODES:
        raise PipelineModeError(f"Unknown mode {mode!r}.")
    # Deterministic stages are always allowed.
    if stage in (cfg.get("deterministic_stages") or []):
        return
    if mode == "mechanical_fallback" and _stage_needs_understanding(stage, cfg):
        # allowed, but caller MUST mark output low-quality (smoke only)
        return
    # otherwise allowed; the LLM/authoring guard is enforced separately
    return


def fail_closed_if_stage_needs_llm(
    stage: str,
    mode: str,
    *,
    has_authored: bool = False,
    llm_client_available: bool = False,
    approved: bool = False,
    config: Optional[dict] = None,
) -> dict:
    """Central fail-closed policy for understanding stages.

    Returns a policy dict: {allow_paid, require_authored, mark_low_quality, source}.
    Raises PipelineModeError when the stage cannot proceed safely.

    - manual_agent / llm_dry_run: require authored files; NEVER paid; if no authored
      input, the CALLER must refuse (no fabrication).
    - llm_paid: requires a real LLM client + approval (and key/budget checked by the
      client). If not available -> fail-closed.
    - mechanical_fallback: allowed for smoke only; output marked low-quality.
    """
    cfg = config or load_mode_config()
    needs = _stage_needs_understanding(stage, cfg)
    rules = mode_rules(mode, config=cfg)

    if not needs:
        return {"allow_paid": False, "require_authored": False,
                "mark_low_quality": False, "source": "deterministic"}

    if mode == "mechanical_fallback":
        return {"allow_paid": False, "require_authored": False,
                "mark_low_quality": True, "source": "mechanical_fallback"}

    if mode in ("manual_agent", "llm_dry_run"):
        # Require run-specific authored content.
        return {"allow_paid": False, "require_authored": True,
                "mark_low_quality": False,
                "source": "authored" if has_authored else "authored_required"}

    if mode == "llm_paid":
        if not llm_client_available:
            raise PipelineModeError(
                f"Stage {stage!r} in mode 'llm_paid' requires integrations/llm_client.py "
                f"(not yet integrated). Fail-closed: no paid call attempted."
            )
        if rules.get("requires_approval", True) and not approved:
            raise PipelineModeError(
                f"Stage {stage!r} in mode 'llm_paid' requires explicit approval "
                f"(approved=False). Fail-closed: no paid call attempted."
            )
        return {"allow_paid": True, "require_authored": False,
                "mark_low_quality": False, "source": "llm_paid"}

    raise PipelineModeError(f"Unhandled mode {mode!r} for stage {stage!r}.")
