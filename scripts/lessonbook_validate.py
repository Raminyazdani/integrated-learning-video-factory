"""lessonbook_validate — aggregate QA for a lessonbook run (read-only).

Runs the lessonbook validators for a run and reports a combined verdict.

Always:
  - validate_lessonbook_pdf.py        (glyphs / overfull / manifest / readability: no_scrambled_english_lists)
  - validate_source_slide_coverage.py (legacy v1 coverage matrix, if present)
  - validate_visual_asset_manifest.py (all planned assets exist, if present)

Strict source-exhaustive layer (v2; auto-enabled when v2 artifacts exist, or with --strict):
  - validate_source_page_inventory.py   (inventory vs extraction metadata + structural sanity)
  - validate_coverage_matrix_v2.py      (every inventory element represented; criticality default rule)
  - validate_source_exhaustive_depth.py (per-element depth review; no needs_fix outstanding)

Mode:
  - default = auto: strict when v2 artifacts (source_page_inventory / *coverage_matrix_v2 / *depth_review)
    are present, else legacy (clearly reported -- e.g. runs 004/005).
  - --strict forces strict (missing v2 artifacts -> FAIL).  --legacy forces legacy.
The future /convert-lecture-pdf run authors the v2 artifacts, so it is validated in strict mode by default.
Run this AFTER render (it adds the PDF/readability checks); run_lessonbook_pipeline.py --render separately
enforces the strict CONTENT gates (inventory + coverage_matrix_v2 + depth) BEFORE rendering.

Authors nothing; renders nothing; no paid/TTS/video. Generic for ANY run.

Usage:
  python scripts/lessonbook_validate.py --run runs/<id> [--strict | --legacy]
Exit: 0 = all PASS, 3 = at least one FAIL (or nothing to validate).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lessonbook_status import find_book_dir  # noqa: E402

PY = sys.executable


def _first(book: Path, *pats: str):
    for p in pats:
        hits = sorted(book.glob(p))
        if hits:
            return hits[0]
    return None


def _pick_tex(book: Path):
    texs = sorted(book.glob("final_lessonbook*.tex"))
    if not texs:
        return None
    def rank(p: Path):
        s = p.stem
        tier = 2 if "polished_v2" in s else 1 if "polished" in s else 0
        return (tier, p.stat().st_mtime)
    return max(texs, key=rank)


def _run(cmd: list) -> tuple[int, str]:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def detect_v2(book: Path, run_dir: Path):
    """Locate the strict v2 artifacts (book dir, falling back to run dir for the inventory)."""
    inv = _first(book, "source_page_inventory.json", "*source_page_inventory.json") \
        or _first(run_dir, "source_page_inventory.json", "*source_page_inventory.json")
    cov2 = _first(book, "*coverage_matrix_v2.json")
    depth = sorted(book.glob("*depth_review.json"))
    return inv, cov2, depth


def validate_run(run_dir: Path, strict: bool | None = None) -> int:
    run_dir = Path(run_dir).resolve()
    book = find_book_dir(run_dir)
    if book is None:
        print(f"[lessonbook-validate] no book dir under {run_dir}; nothing to validate.")
        return 3

    tex = _pick_tex(book)
    stem = tex.stem if tex else None
    manifest = (book / ("lessonbook_pdf_manifest" + stem[len("final_lessonbook"):] + ".json")) if stem else None
    coverage_v1 = _first(book, "*source_slide_coverage_matrix.json")
    vmanifest = _first(book, "*_visual_asset_manifest.json")
    vplan = _first(book, "*_visual_asset_plan.json")
    extraction = run_dir / "extracted" / "extraction_manifest.json"

    inv, cov2, depth_files = detect_v2(book, run_dir)
    v2_present = bool(inv or cov2 or depth_files)
    if strict is True:
        mode = "strict"
    elif strict is False:
        mode = "legacy"
    else:
        mode = "strict" if v2_present else "legacy"

    checks: list[tuple[str, int, str]] = []

    # --- always-on checks ---
    if tex and manifest and manifest.is_file():
        rc, out = _run([PY, ROOT / "scripts" / "validate_lessonbook_pdf.py", "--tex", tex, "--pdf-manifest", manifest])
        checks.append(("pdf+readability", rc, out))
    else:
        checks.append(("pdf+readability", 3, "no rendered tex / pdf manifest found (run --render first)"))

    if coverage_v1:
        rc, out = _run([PY, ROOT / "scripts" / "validate_source_slide_coverage.py", "--matrix", coverage_v1])
        checks.append(("coverage(v1)", rc, out))
    if vmanifest and vplan:
        rc, out = _run([PY, ROOT / "scripts" / "validate_visual_asset_manifest.py", "--manifest", vmanifest, "--plan", vplan])
        checks.append(("visual-assets", rc, out))

    # --- strict v2 layer ---
    if mode == "strict":
        if inv:
            cmd = [PY, ROOT / "scripts" / "validate_source_page_inventory.py", "--inventory", inv]
            if extraction.is_file():
                cmd += ["--extraction", extraction]
            rc, out = _run(cmd)
            checks.append(("source-page-inventory", rc, out))
        else:
            checks.append(("source-page-inventory", 3, "strict: source_page_inventory.json missing — author it before package planning"))

        if cov2:
            cmd = [PY, ROOT / "scripts" / "validate_coverage_matrix_v2.py", "--matrix", cov2]
            if inv:
                cmd += ["--inventory", inv]
            rc, out = _run(cmd)
            checks.append(("coverage-matrix-v2", rc, out))
        else:
            checks.append(("coverage-matrix-v2", 3, "strict: *coverage_matrix_v2.json missing — author it after criticality assignment"))

        if depth_files:
            rc, out = _run([PY, ROOT / "scripts" / "validate_source_exhaustive_depth.py", "--review", book, "--narrative", book])
            checks.append(("depth-review", rc, out))
        else:
            checks.append(("depth-review", 3, "strict: *depth_review.json missing — author per-chapter depth review"))

    # --- report ---
    print(f"=== lessonbook aggregate validation: {run_dir.name} ===")
    print(f"book dir: {book}")
    if mode == "strict":
        print("MODE: strict (source-exhaustive v2 gates: inventory + coverage_matrix_v2 + depth_review)")
    else:
        print("MODE: legacy (v2 strict artifacts absent; full source-exhaustive enforcement needs the "
              "inventory / coverage_matrix_v2 / depth_review gates - run with --strict once authored)")
    n_fail = 0
    for name, rc, out in checks:
        verdict = next((ln.strip() for ln in reversed(out.splitlines()) if "VERDICT" in ln), out.strip().splitlines()[-1] if out.strip() else f"rc={rc}")
        ok = (rc == 0)
        n_fail += (0 if ok else 1)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<22} {verdict}")
    overall = "PASS" if n_fail == 0 else "FAIL"
    print(f"OVERALL: {overall}  ({len(checks) - n_fail}/{len(checks)} validators passed)  [mode={mode}]")
    print("note: draft only — promotion to output/ needs explicit user approval; video/TTS/paid OFF.")
    return 0 if n_fail == 0 else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate lessonbook validation for a run (read-only).")
    ap.add_argument("--run", required=True, help="run dir, e.g. runs/<id>")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--strict", action="store_true", help="force strict v2 mode (missing v2 artifacts -> FAIL)")
    g.add_argument("--legacy", action="store_true", help="force legacy mode (skip the v2 strict layer)")
    args = ap.parse_args()
    strict = True if args.strict else (False if args.legacy else None)
    return validate_run(Path(args.run), strict=strict)


if __name__ == "__main__":
    sys.exit(main())
