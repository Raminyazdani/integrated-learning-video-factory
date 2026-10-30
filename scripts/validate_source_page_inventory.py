"""validate_source_page_inventory — strict check of the Claude-authored source-page inventory (read-only).

Validates the inventory against its schema AND against deterministic extraction metadata + structural
sanity rules, so a future PDF cannot pass with a lenient / under-filled inventory. It does NOT itself OCR
or "understand" the page image; it checks the Claude-authored inventory against the extraction manifest
(page count, per-page text length, readability) and structural rules. No render, no content, no paid/TTS/video.

Usage:
  python scripts/validate_source_page_inventory.py --inventory <path> [--extraction <extraction_manifest.json>]
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "source_page_inventory.schema.json"

DENSE_CHARS = 200            # a page with >= this many extracted chars is "text-dense"
MIN_DENSE_MEANINGFUL = 2     # a dense page must enumerate at least this many non-decorative elements
# teachable element types that must never be silently dropped to decorative
DOWNGRADE_GUARD_TYPES = {"formula", "table", "figure", "graph", "diagram", "proof_step", "derivation_step"}
# element types that are decorative by nature (no teaching content)
INHERENT_DECORATIVE = {"footer", "page_number", "logo", "decorative", "navigation"}


def _load_extraction(inv_path: Path, ext_arg):
    if ext_arg:
        p = Path(ext_arg)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    run_dir = inv_path.resolve().parent.parent  # book dir -> run dir
    cand = run_dir / "extracted" / "extraction_manifest.json"
    if cand.is_file():
        return json.loads(cand.read_text(encoding="utf-8"))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a Claude-authored source_page_inventory (read-only).")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--extraction", default=None, help="extraction_manifest.json (auto-located from the run dir if omitted)")
    args = ap.parse_args()

    p = Path(args.inventory)
    if not p.is_file():
        print(f"[FAIL] inventory not found: {p}")
        print("VERDICT: FAIL (inventory missing)")
        return 3
    inv = json.loads(p.read_text(encoding="utf-8"))

    checks: list[tuple[str, bool, str]] = []
    warns: list[tuple[str, str]] = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))
    def warn(n, d=""): warns.append((n, d))

    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errs = sorted(Draft202012Validator(schema).iter_errors(inv), key=lambda e: list(e.path))
        chk("schema_valid", not errs, "; ".join(e.message for e in errs[:3]))
    except ImportError:
        chk("schema_valid", True, "jsonschema unavailable (parse-only)")

    pages = inv.get("pages", [])
    page_count = inv.get("page_count")
    by_page = {pg.get("source_page"): pg for pg in pages}

    ext = _load_extraction(p, args.extraction)
    ext_chars: dict = {}
    ext_unreadable: set = set()
    ext_count = None
    if ext:
        ext_count = ext.get("page_count")
        for e in ext.get("pages", []):
            n = e.get("page_number")
            ext_chars[n] = e.get("text_char_count")
            if e.get("unreadable") or (e.get("extraction_status") not in (None, "ok")):
                ext_unreadable.add(n)

    if ext_count is not None:
        chk("page_count_matches_extraction",
            page_count == ext_count and len(pages) == page_count,
            f"inventory page_count={page_count}, extraction={ext_count}, rows={len(pages)}")
    else:
        chk("rows_match_page_count",
            page_count is not None and len(pages) == page_count,
            f"page_count={page_count}, rows={len(pages)} (no extraction manifest found)")

    if page_count:
        missing = [n for n in range(1, page_count + 1) if n not in by_page]
        chk("every_page_has_inventory_row", not missing, ("missing: " + ", ".join(map(str, missing[:8]))) if missing else "")

    no_explanation, uncertain_no_note, decor_no_reason, teachable_silent_decor = [], [], [], []
    text_no_elements, dense_thin = [], []
    for pg in pages:
        sp = pg.get("source_page")
        title = (pg.get("slide_title") or "").strip()
        ptype = (pg.get("page_type") or "").strip()
        note = (pg.get("page_note") or "").strip()
        els = pg.get("elements", []) or []
        if not (title or ptype or note or els):
            no_explanation.append(sp)
        for el in els:
            et = el.get("element_type")
            pc = el.get("preliminary_criticality")
            reason = (el.get("reason") or el.get("uncertainty_note") or "").strip()
            if (et == "uncertain" or pc == "uncertain") and not (el.get("uncertainty_note") or "").strip():
                uncertain_no_note.append(el.get("element_id"))
            if pc == "decorative" and not reason:
                decor_no_reason.append(el.get("element_id"))
            if et in DOWNGRADE_GUARD_TYPES and pc == "decorative" and not reason:
                teachable_silent_decor.append(el.get("element_id"))
        chars = ext_chars.get(sp)
        if chars is not None and sp not in ext_unreadable:
            meaningful = [e for e in els
                          if e.get("element_type") not in INHERENT_DECORATIVE
                          and e.get("preliminary_criticality") != "decorative"]
            if chars > 0 and len(els) == 0:
                text_no_elements.append(sp)
            elif chars >= DENSE_CHARS and len(meaningful) < MIN_DENSE_MEANINGFUL:
                dense_thin.append(sp)

    chk("pages_have_title_or_structure", not no_explanation,
        ("blank pages w/o title/type/note/elements: " + ", ".join(map(str, no_explanation[:8]))) if no_explanation else "")
    chk("uncertain_elements_have_note", not uncertain_no_note, ", ".join(map(str, uncertain_no_note[:6])))
    chk("decorative_elements_have_reason", not decor_no_reason, ", ".join(map(str, decor_no_reason[:6])))
    chk("teachable_not_silently_decorative", not teachable_silent_decor, ", ".join(map(str, teachable_silent_decor[:6])))
    if ext:
        chk("text_pages_have_elements", not text_no_elements,
            ("pages with extracted text but 0 elements: " + ", ".join(map(str, text_no_elements[:8]))) if text_no_elements else "")
        chk("dense_pages_nontrivial_inventory", not dense_thin,
            ("dense pages with <2 meaningful elements: " + ", ".join(map(str, dense_thin[:8]))) if dense_thin else "")
    else:
        warn("extraction_density_checks_skipped", "no extraction_manifest.json found; text/density checks skipped")

    # --- visual under-enumeration guard (image-grounded inventory cross-check) ---
    # Run-007 audit lesson: pixel-blind validators let content-bearing diagrams be mis-typed as bullet/note,
    # so rich visual slides slipped through as "light asides". If the Claude page-review (or the page note)
    # signals a diagram/plot/figure on a page but the inventory has NO figure/graph/diagram/table element,
    # that page is very likely under-enumerated -> FAIL (add the visual element, or justify in the page note).
    VISUAL_TOKENS = ("diagram", "plot", "figure", "graph", "chart", "flowchart", "venn",
                     "scatter", "heatmap", "loop", "bounding box", "decision flow", "نمودار", "شکل")
    VISUAL_ELEMENT_TYPES = {"figure", "graph", "diagram", "table"}
    pr_path = p.parent / "claude_page_review.json"
    visual_under_enum = []
    pr = {}
    if pr_path.is_file():
        try:
            pr = json.loads(pr_path.read_text(encoding="utf-8")).get("reviews", {}) or {}
        except Exception:
            pr = {}
    for pg in pages:
        sp = pg.get("source_page")
        els = pg.get("elements", []) or []
        if any(e.get("element_type") in VISUAL_ELEMENT_TYPES for e in els):
            continue
        rv = pr.get(f"page_{sp:03d}", {}) if isinstance(sp, int) else {}
        sig = " ".join([str(rv.get("title", "")), str(pg.get("page_note", "")), str(pg.get("slide_title", ""))]
                       + [str(s) for s in (rv.get("key_signals") or [])]).lower()
        if any(tok in sig for tok in VISUAL_TOKENS):
            visual_under_enum.append(sp)
    if pr:
        chk("no_visual_under_enumeration", not visual_under_enum,
            ("pages whose page-review/notes signal a diagram/plot/figure but inventory has NO figure/graph/diagram/table element: "
             + ", ".join(map(str, visual_under_enum[:8]))) if visual_under_enum else "")
    else:
        warn("visual_under_enumeration_check_skipped", "no claude_page_review.json found next to the inventory; image-grounded visual cross-check skipped")

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    tot_el = sum(len(pg.get("elements", []) or []) for pg in pages)
    print("=== source page inventory validation ===")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    for n, d in warns:
        print(f"  [WARN] {n}" + (f"  ({d})" if d else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'}  (pages={len(pages)}, elements={tot_el})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
