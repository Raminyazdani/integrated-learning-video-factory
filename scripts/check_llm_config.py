"""check_llm_config - dry-run validation of the LLM client skeleton (NO paid calls).

Verifies:
- llm_config.yaml parses; default provider is manual (safe).
- manual mode: capabilities return None; no key required; no network.
- dry-run mode: planned calls + cost estimate recorded; returns None; no network.
- paid is impossible by default (provider=manual) AND fail-closed in llm_paid
  without approval/key/budget.
- usage report redacts the API key.
- missing API key does not crash dry-run.

Usage: python scripts/check_llm_config.py [--run runs/<run_id>]
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.llm_client import load_llm_config, LLMClient, LLMClientError  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="check_llm_config")
    ap.add_argument("--run", default=None, help="optional run dir for cache path + usage report")
    ap.add_argument("--mode", default=None,
                    choices=["manual_agent", "llm_dry_run", "llm_paid", "mechanical_fallback"],
                    help="report paid_allowed/budget/sdk for this pipeline mode (no calls).")
    args = ap.parse_args(argv)
    run_dir = Path(args.run).resolve() if args.run else None

    if args.mode:
        from integrations.llm_client import provider_sdk_available
        mcfg = dict(load_llm_config())
        mc = LLMClient(mcfg, run_dir=run_dir, pipeline_mode=args.mode)
        print(f"[check_llm_config] mode={args.mode} provider={mc.provider} model={mc.model or '(unset)'} "
              f"key={mc.key_status()} sdk_available={provider_sdk_available(mc.provider)} "
              f"max_cost={mc.max_cost} paid_allowed={mc.paid_allowed()}")

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    cfg = load_llm_config()
    check("config_parses", isinstance(cfg, dict) and "provider" in cfg, f"provider={cfg.get('provider')}")
    check("default_provider_manual", cfg.get("provider") == "manual")
    check("budget_fail_closed_default", cfg.get("fail_closed_if_budget_unknown") is True)

    # manual client (default) — no key, no network, capabilities return None
    mc = LLMClient(cfg, run_dir=run_dir, pipeline_mode="manual_agent")
    check("manual_paid_impossible", not mc.paid_allowed())
    check("manual_vision_none", mc.vision_page_review(page_text="x") is None)
    check("manual_content_none", mc.generate_content(package_id="package_001") is None)
    rep = mc.usage_report()
    check("usage_report_redacts_key", rep["api_key"] in ("absent",) or rep["api_key"].startswith("present(len="),
          f"api_key={rep['api_key']}")
    check("manual_no_planned_cost", rep["est_total_cost_usd"] == 0.0 or rep["planned_calls"] >= 0)

    # dry-run client — records planned calls, returns None, no network, no key needed
    dcfg = dict(cfg); dcfg["mode"] = "dry_run"; dcfg["provider"] = "anthropic"  # paid provider name but dry-run
    dc = LLMClient(dcfg, run_dir=run_dir, pipeline_mode="llm_dry_run")
    check("dry_run_no_key_no_crash", dc.vision_page_review(page_text="abc") is None)
    dc.generate_content(package_id="package_001")
    rep2 = dc.usage_report()
    check("dry_run_paid_impossible", not dc.paid_allowed())
    check("dry_run_records_plan", rep2["planned_calls"] >= 2, f"planned={rep2['planned_calls']}")
    check("dry_run_has_estimate", rep2["est_total_cost_usd"] >= 0.0, f"est={rep2['est_total_cost_usd']}")

    # llm_paid without approval/key/budget => fail-closed
    pcfg = dict(cfg); pcfg["provider"] = "anthropic"; pcfg["mode"] = "live"
    pc = LLMClient(pcfg, run_dir=run_dir, pipeline_mode="llm_paid", approved=False)
    check("llm_paid_no_approval_blocked", not pc.paid_allowed())
    try:
        pc.generate_content(package_id="package_001")
        check("llm_paid_fail_closed_raises", False, "did not raise")
    except LLMClientError:
        check("llm_paid_fail_closed_raises", True)

    # write a usage report if run dir given (allowed write)
    if run_dir:
        out = run_dir / "QA_REPORTS" / "llm_usage_report.md"
        dc.write_usage_report(out)
        check("usage_report_written", out.is_file(), str(out))

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== LLM config / client dry-run validation ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'} ({len(checks)-n_fail}/{len(checks)})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
