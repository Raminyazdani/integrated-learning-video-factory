"""validate_lessonbook_pdf — read-only QA for a rendered lessonbook PDF.

Checks (no rendering, no TTS/video):
  - PDF exists and is non-empty.
  - XeLaTeX .log has no fatal '!' error.
  - no 'Missing character' lines (tofu / font-glyph gaps).
  - reports Overfull \\hbox count (target 0).
  - page count parsed from the log.
  - if a lessonbook_pdf_manifest.json is present, validate it against
    schemas/lessonbook_pdf_manifest.schema.json.

Usage:
  python scripts/validate_lessonbook_pdf.py --smoke
  python scripts/validate_lessonbook_pdf.py --tex runs/<id>/lessonbook_full/final_lessonbook.tex
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_TEX = ROOT / "runs" / "lessonbook_renderer_smoke_test" / "lessonbook_smoke.tex"
PDF_MANIFEST_SCHEMA = ROOT / "schemas" / "lessonbook_pdf_manifest.schema.json"


def _scan_log(log_path: Path) -> dict:
    txt = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    errors = [ln for ln in txt.splitlines() if ln.startswith("!")]
    missing = re.findall(r"Missing character:.*", txt)
    overfull = re.findall(r"Overfull \\hbox", txt)
    fontmiss = [ln for ln in txt.splitlines() if "could not be found" in ln or "not loadable" in ln]
    m = re.search(r"Output written on .*?\(\s*(\d+)\s+pages?\)", txt, re.S)
    pages = int(m.group(1)) if m else None
    return {"errors": errors, "missing_char": len(missing), "overfull": len(overfull),
            "font_missing": fontmiss, "pages": pages, "have_log": log_path.is_file()}


def _scan_tex(tex_path: Path) -> dict:
    """Readability scan of the generated .tex (mixed RTL/LTR quality):
      - scrambled_lists: comma-separated runs of 3+ \\lrterm islands that bidi would
        reorder (should be 0 — they must be merged into a single \\lrgroup);
      - lrgroup: count of grouped inline English enumerations;
      - minilist: count of aligned mini-lists (symbol tables / formula-answer blocks)."""
    txt = tex_path.read_text(encoding="utf-8", errors="replace") if tex_path.is_file() else ""
    scrambled = re.findall(r"\\lrterm\{[^{}]*\}(?:\s*[،,]\s*\\lrterm\{[^{}]*\}){2,}", txt)
    return {"scrambled_lists": len(scrambled),
            "lrgroup": len(re.findall(r"\\lrgroup\{", txt)),
            "minilist": len(re.findall(r"\\begin\{lbminilist\}", txt))}


def validate(tex_path: Path, manifest_path: Path | None) -> int:
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    pdf = tex_path.with_suffix(".pdf")
    log = tex_path.with_suffix(".log")
    chk("pdf_exists", pdf.is_file() and pdf.stat().st_size > 0,
        f"{pdf.name} ({pdf.stat().st_size if pdf.is_file() else 0} B)")

    s = _scan_log(log)
    chk("log_present", s["have_log"])
    chk("no_fatal_error", len(s["errors"]) == 0, "; ".join(s["errors"][:3]))
    chk("no_missing_glyphs", s["missing_char"] == 0, f"missing_char={s['missing_char']}")
    chk("no_font_missing", len(s["font_missing"]) == 0, "; ".join(s["font_missing"][:2]))
    chk("page_count_ok", (s["pages"] or 0) >= 1, f"pages={s['pages']}")
    # overfull is a warning (reported, not a hard fail)
    overfull_ok = s["overfull"] == 0
    chk("no_overfull (warn)", overfull_ok, f"overfull_hbox={s['overfull']}")

    # --- readability contract (mixed RTL/LTR quality on the generated .tex) ---
    ts = _scan_tex(tex_path)
    chk("no_scrambled_english_lists", ts["scrambled_lists"] == 0,
        f"ungrouped comma-runs of 3+ English terms = {ts['scrambled_lists']}")
    chk("english_lists_grouped (warn)", True, f"\\lrgroup count = {ts['lrgroup']}")
    chk("answer_comparisons_listed (warn)", True, f"\\begin{{lbminilist}} count = {ts['minilist']}")

    man_ok = True
    if manifest_path and manifest_path.is_file():
        try:
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
            try:
                from jsonschema import Draft202012Validator
                schema = json.loads(PDF_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
                errs = sorted(Draft202012Validator(schema).iter_errors(man), key=lambda e: e.path)
                man_ok = not errs
                chk("manifest_schema_valid", man_ok, "; ".join(e.message for e in errs[:3]))
            except ImportError:
                chk("manifest_schema_valid", True, "jsonschema unavailable (parse-only)")
        except Exception as e:
            man_ok = False
            chk("manifest_schema_valid", False, str(e))

    # --- cross-manifest consistency: PDF manifest vs visual-asset manifest vs coverage ---
    vdir = tex_path.parent
    pm = {}
    if manifest_path and manifest_path.is_file():
        try:
            pm = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pm = {}
    pm_images = pm.get("images", []) or []
    pm_ids = {im.get("asset_id") for im in pm_images if im.get("asset_id")}
    pm_paths = {im.get("image_path") for im in pm_images if im.get("image_path")}

    vcands = sorted(vdir.glob("*_visual_asset_manifest.json")) + sorted(vdir.glob("visual_asset_manifest.json"))
    vassets = []
    if vcands:
        try:
            vassets = json.loads(vcands[0].read_text(encoding="utf-8")).get("assets", [])
        except Exception:
            vassets = []
    used = [a for a in vassets if a.get("used_in_pdf")]
    if vassets:
        chk("pdf_manifest_images_nonempty", (len(pm_images) > 0) if used else True,
            f"pdf_images={len(pm_images)}, used_assets={len(used)}")
        missing_used = [a.get("asset_id") for a in used
                        if a.get("asset_id") not in pm_ids and a.get("path") not in pm_paths]
        chk("all_used_visuals_in_pdf_manifest", not missing_used, ", ".join(map(str, missing_used[:6])))

    ccands = sorted(vdir.glob("*_source_slide_coverage_matrix.json"))
    if ccands and vassets:
        try:
            cov = json.loads(ccands[0].read_text(encoding="utf-8"))
        except Exception:
            cov = {}
        non_anchor_pages = {a.get("source_page") for a in vassets
                            if a.get("asset_type") in ("crop", "annotated_crop", "masked_variant", "generated_teaching_diagram")}
        dense_bad = []
        for page in cov.get("pages", []):
            sp = page.get("source_page")
            dense = any(e.get("importance") == "critical" and e.get("element_type") in ("figure", "graph")
                        for e in page.get("elements", []))
            if dense and sp not in non_anchor_pages:
                dense_bad.append(sp)
        chk("dense_slides_have_nonanchor_visual", not dense_bad, "pages: " + ", ".join(map(str, dense_bad[:6])))

    # hard-fail set excludes warnings (overfull + informational readability counts)
    hard = [ok for name, ok, _ in checks if "(warn)" not in name]
    n_fail = sum(1 for ok in hard if not ok)

    print("=== lessonbook PDF validation ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    verdict = "PASS" if n_fail == 0 else "FAIL"
    if verdict == "PASS" and not overfull_ok:
        verdict = "PASS (with overfull warning)"
    print(f"VERDICT: {verdict}")
    return 0 if n_fail == 0 else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a rendered lessonbook PDF (read-only).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--tex", type=str, help="path to the lessonbook .tex (pdf/log inferred)")
    ap.add_argument("--pdf-manifest", default=None, help="explicit lessonbook_pdf_manifest path (else inferred in the tex dir)")
    args = ap.parse_args()
    if args.smoke:
        tex = SMOKE_TEX
        manifest = tex.parent / "lessonbook_pdf_manifest.json"
    else:
        tex = Path(args.tex)
        manifest = tex.parent / "lessonbook_pdf_manifest.json"
    if args.pdf_manifest:
        manifest = Path(args.pdf_manifest)
    if not tex.is_file():
        print(f"[FAIL] tex not found: {tex}")
        return 3
    return validate(tex, manifest)


if __name__ == "__main__":
    sys.exit(main())
