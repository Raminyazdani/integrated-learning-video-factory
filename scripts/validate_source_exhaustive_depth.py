"""validate_source_exhaustive_depth — strict depth check for taught elements (read-only).

Validates source_exhaustive_depth_review artifact(s): for every critical/teachable element it confirms the
narrative actually teaches it at the required depth (formula deep mode, visual deep mode, table
reconstruction, proof walkthrough, worked-example steps) and that no element is still flagged needs_fix.
Optionally inspects the chapter narrative JSON structurally (opening intuition + recap, examples have steps).
No render, no content, no paid/TTS/video.

Usage:
  python scripts/validate_source_exhaustive_depth.py --review <file-or-dir> [--narrative <file-or-dir>]
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "source_exhaustive_depth_review.schema.json"

VISUAL_TYPES = {"figure", "graph", "diagram"}
PROOF_TYPES = {"proof_step", "derivation_step"}


def _truthy(dc: dict, key: str) -> bool:
    return bool(dc.get(key))


def _required_depth_missing(el: dict) -> list[str]:
    """Return the list of required depth_check keys that are False/absent for this element's type+treatment."""
    et = el.get("element_type")
    rt = el.get("required_treatment")
    dc = el.get("depth_checks") or {}
    missing: list[str] = []

    if et == "formula" or rt == "formula_deep_mode":
        for k in ("has_motivation", "has_symbol_explanation", "has_intuition"):
            if not _truthy(dc, k):
                missing.append(k)
        if not (_truthy(dc, "has_common_mistake") or _truthy(dc, "has_exam_or_practical_note")):
            missing.append("has_common_mistake|has_exam_or_practical_note")
    elif et in VISUAL_TYPES or rt == "visual_deep_mode":
        for k in ("has_visual_support", "has_intuition"):
            if not _truthy(dc, k):
                missing.append(k)
    elif et == "table" or rt == "table_reconstruction":
        if not _truthy(dc, "has_intuition"):
            missing.append("has_intuition")
        if not (_truthy(dc, "has_visual_support") or _truthy(dc, "has_symbol_explanation")):
            missing.append("has_visual_support|has_symbol_explanation")
    elif et in PROOF_TYPES or rt == "proof_walkthrough":
        if not (_truthy(dc, "has_solution_steps") or _truthy(dc, "has_intuition")):
            missing.append("has_solution_steps|has_intuition")
    elif et == "example":
        if not _truthy(dc, "has_solution_steps"):
            missing.append("has_solution_steps")
    return missing


def _collect(path: Path, pattern: str) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob(pattern))
    return [path] if path.is_file() else []


def _narrative_struct_warnings(nar_paths: list[Path]) -> list[str]:
    out: list[str] = []
    for np_ in nar_paths:
        try:
            nar = json.loads(np_.read_text(encoding="utf-8"))
        except Exception:
            continue
        beats = nar.get("beats", []) or []
        blob = json.dumps(beats, ensure_ascii=False).lower()
        if "intuition" not in blob:
            out.append(f"{np_.name}: no opening-intuition beat detected")
        if "recap" not in blob and "جمع‌بندی" not in json.dumps(beats, ensure_ascii=False):
            out.append(f"{np_.name}: no recap beat detected")
        for ex in nar.get("examples", []) or []:
            # accept steps_fa (the narrative schema's field) as well as the generic keys
            if not (ex.get("solution_steps") or ex.get("steps") or ex.get("steps_fa") or ex.get("solution") or ex.get("answer_fa")):
                out.append(f"{np_.name}: example {ex.get('id') or ex.get('example_id') or ex.get('after_concept') or '?'} has no solution steps")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate source-exhaustive depth review(s) (read-only).")
    ap.add_argument("--review", required=True, help="a *depth_review.json file or a directory containing them")
    ap.add_argument("--narrative", default=None, help="optional chapter_*_narrative.json file or directory for structural inspection")
    args = ap.parse_args()

    review_files = _collect(Path(args.review), "*depth_review.json")
    if not review_files:
        print(f"[FAIL] no depth review artifact found at: {args.review}")
        print("VERDICT: FAIL (author source_exhaustive_depth_review.json per chapter)")
        return 3

    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
    except ImportError:
        validator = None

    checks: list[tuple[str, bool, str]] = []
    warns: list[tuple[str, str]] = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))
    def warn(n, d=""): warns.append((n, d))

    total_elems = 0
    bad_schema, needs_fix, pass_inconsistent, struct_fail = [], [], [], []
    crit_pass_no_loc = []  # critical elements marked pass but with no lesson_location -> evidence nudge
    for rf in review_files:
        review = json.loads(rf.read_text(encoding="utf-8"))
        cid = review.get("chapter_id", rf.stem)
        if validator is not None:
            errs = sorted(validator.iter_errors(review), key=lambda e: list(e.path))
            if errs:
                bad_schema.append(f"{cid}: {errs[0].message}")
        for el in review.get("reviewed_elements", []) or []:
            total_elems += 1
            eid = f"{cid}/{el.get('element_id')}"
            verdict = el.get("verdict")
            if verdict == "not_applicable":
                continue
            if verdict == "needs_fix":
                needs_fix.append(eid)
            miss = _required_depth_missing(el)
            if verdict == "pass" and miss:
                pass_inconsistent.append(f"{eid} missing {','.join(miss)}")
            if el.get("criticality") == "critical" and verdict == "pass" and not (el.get("lesson_location") or "").strip():
                crit_pass_no_loc.append(eid)
        cs = review.get("chapter_structure_checks")
        if isinstance(cs, dict):
            if cs.get("has_opening_intuition") is False or cs.get("has_recap") is False or cs.get("summary_only") is True:
                struct_fail.append(cid)

    chk("all_reviews_schema_valid", not bad_schema, "; ".join(bad_schema[:3]))
    chk("no_elements_need_fix", not needs_fix, ", ".join(needs_fix[:6]))
    chk("pass_verdicts_meet_required_depth", not pass_inconsistent, "; ".join(pass_inconsistent[:4]))
    chk("chapters_have_intro_and_recap_not_summary_only", not struct_fail, ", ".join(struct_fail[:6]))

    if crit_pass_no_loc:
        warn("critical_pass_without_lesson_location", ", ".join(crit_pass_no_loc[:6]) + " (a pass critical element should cite where it is taught)")

    if args.narrative:
        nw = _narrative_struct_warnings(_collect(Path(args.narrative), "chapter_*_narrative.json"))
        for w in nw[:8]:
            warn("narrative_structure", w)

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== source-exhaustive depth validation ===")
    print(f"reviews: {len(review_files)}  reviewed_elements: {total_elems}")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    for n, d in warns:
        print(f"  [WARN] {n}" + (f"  ({d})" if d else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'}  (needs_fix={len(needs_fix)}, pass_inconsistent={len(pass_inconsistent)})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
