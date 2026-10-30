"""validate_source_slide_coverage — enforce source-slide-exhaustive coverage (read-only).

Checks a source_slide_coverage_matrix against its schema AND the doctrine gate:
- every `critical` element must be `taught_deeply`;
- no `critical`/`useful` element may remain `uncertain_needs_review`;
- every `excluded_decorative` needs a reason (uncertainty_note or importance=decorative);
- every taught element has a lesson_location.

Usage: python scripts/validate_source_slide_coverage.py --matrix <path>
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "source_slide_coverage_matrix.schema.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", required=True)
    args = ap.parse_args()
    p = Path(args.matrix)
    if not p.is_file():
        print(f"[FAIL] matrix not found: {p}"); return 3
    matrix = json.loads(p.read_text(encoding="utf-8"))

    checks = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))

    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errs = sorted(Draft202012Validator(schema).iter_errors(matrix), key=lambda e: list(e.path))
        chk("schema_valid", not errs, "; ".join(e.message for e in errs[:3]))
    except ImportError:
        chk("schema_valid", True, "jsonschema unavailable (parse-only)")

    crit_total = crit_deep = 0
    uncertain_open = []
    excl_no_reason = []
    taught_no_loc = []
    crit_visual_no_asset = []
    for page in matrix.get("pages", []):
        for el in page.get("elements", []):
            imp = el.get("importance"); cov = el.get("coverage_status"); eid = el.get("element_id")
            if imp == "critical":
                crit_total += 1
                if cov == "taught_deeply":
                    crit_deep += 1
            if cov == "uncertain_needs_review" and imp in ("critical", "useful"):
                uncertain_open.append(eid)
            if cov == "excluded_decorative" and imp != "decorative" and not el.get("uncertainty_note"):
                excl_no_reason.append(eid)
            if cov in ("taught_deeply", "taught_briefly") and not el.get("lesson_location"):
                taught_no_loc.append(eid)
            # a critical figure/graph/formula must be supported by a crop/annotation asset
            if imp == "critical" and el.get("element_type") in ("figure", "graph", "formula") and not el.get("visual_asset_ids"):
                crit_visual_no_asset.append(eid)

    chk("all_critical_taught_deeply", crit_total > 0 and crit_deep == crit_total, f"{crit_deep}/{crit_total}")
    chk("no_open_uncertain_critical_useful", not uncertain_open, ", ".join(map(str, uncertain_open[:6])))
    chk("excluded_have_reason", not excl_no_reason, ", ".join(map(str, excl_no_reason[:6])))
    chk("taught_have_location", not taught_no_loc, ", ".join(map(str, taught_no_loc[:6])))
    chk("critical_visuals_have_assets", not crit_visual_no_asset, ", ".join(map(str, crit_visual_no_asset[:6])))

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== source-slide coverage validation ===")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'}  (critical taught_deeply: {crit_deep}/{crit_total})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
