"""validate_coverage_matrix_v2 — strict coverage check against the source-page inventory (read-only).

Validates a source_element_coverage_matrix_v2 against its schema AND against the independent
source_page_inventory + the objective criticality default rules (docs/SOURCE_ELEMENT_CRITICALITY_POLICY.md).
This is what stops a lenient/self-reported matrix from passing: every inventory element must be represented
or excluded with a reason, formulas/figures/graphs/tables default to critical unless justified, no critical
element is mentioned_only, etc. No render, no content, no paid/TTS/video.

Usage:
  python scripts/validate_coverage_matrix_v2.py --matrix <path> [--inventory <source_page_inventory.json>]
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "source_element_coverage_matrix_v2.schema.json"

VISUAL_TYPES = {"figure", "graph", "diagram"}
PROOF_TYPES = {"proof_step", "derivation_step"}
TEACH_COVERED = {"taught_deeply", "taught_briefly", "visually_supported"}
VISUAL_SUPPORT_KINDS = {"crop", "annotated_crop", "masked_variant", "generated_diagram", "full_anchor"}
# A criticality downgrade must be PEDAGOGICAL (trivial restatement / duplicate of a taught element).
# Extraction/OCR difficulty is NOT a valid reason: the slide IMAGE is still teachable. (Run-007 audit lesson:
# p13's first-model formula had been downgraded critical->useful citing "OCR-corrupt extracted text".)
_EXTRACTION_DOWNGRADE_PATTERNS = ("ocr", "extract", "corrupt", "unreadable", "garbled",
                                  "encode", "mangle", "traceback", "charmap", "استخراج", "خراب")


def _flat(matrix):
    for pg in matrix.get("pages", []):
        for el in pg.get("elements", []):
            yield pg.get("source_page"), el


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a coverage_matrix_v2 against the inventory + criticality rules.")
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--inventory", default=None, help="source_page_inventory.json (enables representation/page cross-checks)")
    args = ap.parse_args()

    mp = Path(args.matrix)
    if not mp.is_file():
        print(f"[FAIL] coverage matrix v2 not found: {mp}")
        print("VERDICT: FAIL (matrix missing)")
        return 3
    matrix = json.loads(mp.read_text(encoding="utf-8"))
    inventory = None
    if args.inventory and Path(args.inventory).is_file():
        inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))

    checks: list[tuple[str, bool, str]] = []
    warns: list[tuple[str, str]] = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))
    def warn(n, d=""): warns.append((n, d))

    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errs = sorted(Draft202012Validator(schema).iter_errors(matrix), key=lambda e: list(e.path))
        chk("schema_valid", not errs, "; ".join(e.message for e in errs[:3]))
    except ImportError:
        chk("schema_valid", True, "jsonschema unavailable (parse-only)")

    els = list(_flat(matrix))
    total = len(els)

    # ---- cross-checks vs the inventory (the anti-leniency core) ----
    if inventory is not None:
        inv_ids = {e.get("element_id") for pg in inventory.get("pages", []) for e in (pg.get("elements") or [])}
        linked: dict = {}
        for _, el in els:
            linked.setdefault(el.get("linked_inventory_element_id"), []).append(el)
        unrepresented = []
        for iid in inv_ids:
            live = [m for m in linked.get(iid, []) if m.get("coverage_status") != "missing"]
            if not live:
                unrepresented.append(iid)
        chk("every_inventory_element_represented", not unrepresented,
            (f"{len(unrepresented)} unrepresented: " + ", ".join(map(str, unrepresented[:6]))) if unrepresented else "")

        excl_no_reason = [el.get("element_id") for _, el in els
                          if el.get("coverage_status") == "excluded_with_reason"
                          and not (el.get("downgrade_reason") or el.get("required_fix") or el.get("evidence_reference") or el.get("final_status"))]
        chk("excluded_elements_have_reason", not excl_no_reason, ", ".join(map(str, excl_no_reason[:6])))

        inv_pages = {pg.get("source_page") for pg in inventory.get("pages", [])}
        mat_pages = {sp for sp, _ in els}
        chk("matrix_pages_equal_inventory_pages", inv_pages == mat_pages,
            f"inventory={sorted(p for p in inv_pages if p is not None)[:8]} matrix={sorted(p for p in mat_pages if p is not None)[:8]}")
    else:
        warn("inventory_cross_checks_skipped", "no --inventory given; representation + page-equality checks skipped (strict mode requires inventory)")

    # ---- criticality default rule + coverage discipline ----
    formula_bad, table_bad, visual_bad, proof_bad = [], [], [], []
    crit_mentioned, crit_no_loc, deep_no_evidence, summary_bad = [], [], [], []
    vis_support_missing = []
    extraction_downgrade_bad = []
    pages_loc: dict = {}
    for sp, el in els:
        et = el.get("element_type"); cr = el.get("criticality"); rt = el.get("required_treatment")
        rv = el.get("required_visual_support"); cov = el.get("coverage_status")
        dr = (el.get("downgrade_reason") or "").strip()
        aloc = (el.get("actual_lesson_location") or "").strip()
        ploc = (el.get("planned_lesson_location") or "").strip()
        ev = (el.get("evidence_reference") or "").strip()
        vids = el.get("visual_asset_ids") or []
        eid = el.get("element_id")

        if et == "formula" and cr != "critical" and not dr:
            formula_bad.append(eid)
        if et == "table" and cr not in ("critical", "useful") and not dr:
            table_bad.append(eid)
        if et in VISUAL_TYPES and cr != "critical" and not dr:
            visual_bad.append(eid)
        # a teachable-type element downgraded for an EXTRACTION/OCR reason is invalid (image still teaches it)
        if et in (VISUAL_TYPES | {"formula", "table"}) and cr != "critical" and dr:
            low = dr.lower()
            if any(p in low for p in _EXTRACTION_DOWNGRADE_PATTERNS):
                extraction_downgrade_bad.append(eid)
        if et in PROOF_TYPES and rt != "proof_walkthrough" and not (rt in ("brief_context", "optional_extension_brief") and dr):
            proof_bad.append(eid)
        if et in ("summary_item", "extra_reading"):
            ok = (cov in TEACH_COVERED or cr == "optional_extension"
                  or rt == "optional_extension_brief" or cov == "excluded_with_reason")
            if not ok and cov in ("missing", "mentioned_only"):
                summary_bad.append(eid)
        if cr == "critical" and cov == "mentioned_only":
            crit_mentioned.append(eid)
        if cr == "critical" and cov in TEACH_COVERED and not aloc:
            crit_no_loc.append(eid)
        if et in (VISUAL_TYPES | {"table"}) and cr == "critical" and rv == "none":
            vis_support_missing.append(f"{eid}(critical-visual-no-support)")
        if rv in VISUAL_SUPPORT_KINDS and cov in TEACH_COVERED and not vids:
            vis_support_missing.append(f"{eid}(claims-support-no-asset)")
        if cov == "taught_deeply" and not (aloc or ev):
            deep_no_evidence.append(eid)

        info = pages_loc.setdefault(sp, {"teachable": False, "has_loc": False})
        if cr not in ("decorative", "structural"):
            info["teachable"] = True
        if aloc or ploc:
            info["has_loc"] = True

    page_no_loc = [sp for sp, info in pages_loc.items() if info["teachable"] and not info["has_loc"]]

    chk("formula_default_critical", not formula_bad, ", ".join(map(str, formula_bad[:6])))
    chk("table_default_critical_or_useful", not table_bad, ", ".join(map(str, table_bad[:6])))
    chk("visual_default_critical", not visual_bad, ", ".join(map(str, visual_bad[:6])))
    chk("downgrade_reason_not_extraction_difficulty", not extraction_downgrade_bad,
        ("extraction/OCR is not a valid downgrade reason (teach from the image): " + ", ".join(map(str, extraction_downgrade_bad[:6]))) if extraction_downgrade_bad else "")
    chk("proof_requires_walkthrough", not proof_bad, ", ".join(map(str, proof_bad[:6])))
    chk("summary_extra_covered_or_optional", not summary_bad, ", ".join(map(str, summary_bad[:6])))
    chk("no_critical_mentioned_only", not crit_mentioned, ", ".join(map(str, crit_mentioned[:6])))
    chk("critical_has_actual_location", not crit_no_loc, ", ".join(map(str, crit_no_loc[:6])))
    chk("required_visual_support_present", not vis_support_missing, ", ".join(map(str, vis_support_missing[:6])))
    chk("taught_deeply_has_evidence", not deep_no_evidence, ", ".join(map(str, deep_no_evidence[:6])))
    chk("pages_have_lesson_location", not page_no_loc, ", ".join(map(str, page_no_loc[:8])))

    n_crit = sum(1 for _, el in els if el.get("criticality") == "critical")
    n_use = sum(1 for _, el in els if el.get("criticality") == "useful")
    n_decor = sum(1 for _, el in els if el.get("criticality") == "decorative")
    if total >= 6 and n_decor / total > 0.5:
        warn("high_decorative_fraction", f"{n_decor}/{total} elements decorative - verify the lecture is really that sparse")
    if total >= 20 and (n_crit + n_use) < 3:
        warn("too_few_critical_useful_for_dense_lecture", f"{n_crit + n_use} critical/useful of {total} - likely under-coverage")

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== coverage matrix v2 validation ===")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    for n, d in warns:
        print(f"  [WARN] {n}" + (f"  ({d})" if d else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'}  (elements={total}, critical={n_crit}, useful={n_use}, decorative={n_decor})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
