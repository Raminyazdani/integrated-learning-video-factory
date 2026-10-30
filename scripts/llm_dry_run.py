"""llm_dry_run - plan LLM/Vision calls for a run WITHOUT making any paid call.

Iterates the run's real pages/packages, asks the (dry-run) LLMClient to plan each
understanding-stage call, and writes runs/<RUN_ID>/QA_REPORTS/llm_dry_run_report.md.

Usage:
  python scripts/llm_dry_run.py --run runs/004_test_20260522_1844 \
      --stages page_review,package_metadata,content [--provider anthropic] [--model NAME]

NEVER makes a network/paid call (pipeline_mode=llm_dry_run forces plan-only).
Exit: 0 always (planning), 2 if run not found.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.llm_client import load_llm_config, LLMClient  # noqa: E402

ALL_STAGES = ["page_review", "package_metadata", "content", "leakage", "qa"]


def _pages(run_dir: Path):
    for name in ("page_classification.json", "page_classification_enriched.json"):
        p = run_dir / "classification" / name
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            return [pg["page_id"] for pg in d.get("pages", [])]
    return []


def _packages(run_dir: Path):
    p = run_dir / "packages" / "package_manifest.json"
    if p.is_file():
        d = json.loads(p.read_text(encoding="utf-8"))
        return [pk["package_id"] for pk in d.get("packages", [])]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="llm_dry_run")
    ap.add_argument("--run", required=True)
    ap.add_argument("--stages", default=",".join(ALL_STAGES))
    ap.add_argument("--provider", default=None, help="preview provider (default from config). No call made.")
    ap.add_argument("--model", default=None, help="preview model (default from config).")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run not found: {run_dir}", file=sys.stderr)
        return 2

    cfg = dict(load_llm_config())
    if args.provider:
        cfg["provider"] = args.provider
    if args.model:
        cfg["model"] = args.model
    cfg["mode"] = "dry_run"

    client = LLMClient(cfg, run_dir=run_dir, pipeline_mode="llm_dry_run")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    pages = _pages(run_dir)
    pkgs = _packages(run_dir)

    if "page_review" in stages:
        for pid in pages:
            client.vision_page_review(image_path=run_dir / "extracted" / "pages" / f"{pid}.png",
                                      page_text="", page_id=pid)
    if "package_metadata" in stages:
        for pk in pkgs:
            client.package_metadata(package_id=pk, evidence={"pages": pk})
    if "content" in stages:
        for pk in pkgs:
            client.persian_package_content(package_id=pk, package_evidence={"pages": pk})
    if "leakage" in stages:
        for pk in pkgs:
            client.content_leakage_check(text="<package text>", banned_terms=["Taxonomy"], target_id=pk)
    if "qa" in stages:
        for pk in pkgs:
            client.optional_qa_review(artifact=pk, target_id=pk)

    report = client.write_dry_run_report(run_dir / "QA_REPORTS" / "llm_dry_run_report.md")
    pl = client.dry_run_plan()
    print(f"[llm_dry_run] run={pl['run_id']} provider={pl['provider']} model={pl['model']}")
    print(f"[llm_dry_run] planned_calls={pl['planned_calls']} would_call(MISS)={pl['cache_misses_would_call']} "
          f"est_cost=${pl['est_total_cost_usd']}")
    print(f"[llm_dry_run] paid_allowed={pl['paid_allowed']}  no_paid_calls_made={pl['no_paid_calls_made']}")
    print(f"[llm_dry_run] wrote {report}")
    # hard assert no paid call happened
    assert client.spent_usd == 0.0, "dry-run must not spend"
    return 0


if __name__ == "__main__":
    sys.exit(main())
