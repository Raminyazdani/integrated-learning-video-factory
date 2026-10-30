"""validate_pipeline_modes - sanity-check the pipeline mode system (read-only).

Checks:
- configs/pipeline_modes.yaml parses and defines all 4 modes.
- default_mode is valid; unknown mode raises clearly.
- llm_dry_run / manual_agent / mechanical_fallback NEVER allow paid calls.
- llm_paid is fail-closed without an llm_client (raises).
- understanding stages (content_generation) are not silently allowed to fabricate.

Usage: python scripts/validate_pipeline_modes.py
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.pipeline_mode import (  # noqa: E402
    load_mode_config, resolve_mode, mode_allows_paid,
    fail_closed_if_stage_needs_llm, PipelineModeError,
)

VALID = ("manual_agent", "llm_dry_run", "llm_paid", "mechanical_fallback")


def main() -> int:
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    cfg = load_mode_config()
    check("config_parses", "modes" in cfg, f"modes={list(cfg.get('modes', {}))}")
    for m in VALID:
        check(f"mode_defined::{m}", m in cfg.get("modes", {}))
    check("default_mode_valid", cfg.get("default_mode") in VALID, f"default={cfg.get('default_mode')}")

    # unknown mode raises
    try:
        resolve_mode("does_not_exist")
        check("unknown_mode_raises", False, "did not raise")
    except PipelineModeError:
        check("unknown_mode_raises", True)

    # paid only in llm_paid
    check("manual_agent_no_paid", not mode_allows_paid("manual_agent"))
    check("llm_dry_run_no_paid", not mode_allows_paid("llm_dry_run"))
    check("mechanical_no_paid", not mode_allows_paid("mechanical_fallback"))
    check("llm_paid_allows_paid", mode_allows_paid("llm_paid"))

    # understanding stage fail-closed behavior
    for m in ("manual_agent", "llm_dry_run"):
        pol = fail_closed_if_stage_needs_llm("content_generation", m,
                                             has_authored=False, llm_client_available=False)
        check(f"{m}_requires_authored_no_paid",
              pol["require_authored"] and not pol["allow_paid"], str(pol))

    pol = fail_closed_if_stage_needs_llm("content_generation", "mechanical_fallback")
    check("mechanical_marks_low_quality", pol["mark_low_quality"] and not pol["allow_paid"], str(pol))

    # llm_paid without client must fail-closed (raise)
    try:
        fail_closed_if_stage_needs_llm("content_generation", "llm_paid",
                                       llm_client_available=False, approved=True)
        check("llm_paid_fail_closed_without_client", False, "did not raise")
    except PipelineModeError:
        check("llm_paid_fail_closed_without_client", True)

    # llm_paid with client but no approval must fail-closed
    try:
        fail_closed_if_stage_needs_llm("content_generation", "llm_paid",
                                       llm_client_available=True, approved=False)
        check("llm_paid_requires_approval", False, "did not raise")
    except PipelineModeError:
        check("llm_paid_requires_approval", True)

    # deterministic stage allowed in any mode without paid
    pol = fail_closed_if_stage_needs_llm("extraction", "manual_agent")
    check("deterministic_stage_ok", not pol["allow_paid"] and not pol["require_authored"], str(pol))

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== pipeline mode validation ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'} ({len(checks)-n_fail}/{len(checks)})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
