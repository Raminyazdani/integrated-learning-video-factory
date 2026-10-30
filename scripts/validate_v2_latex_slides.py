"""validate_v2_latex_slides — sanity checks for the v2 LaTeX smoke deck.

Checks: PDF exists + page count; PNG dimensions ~16:9; the mixed
Persian/English/math test sentence is present in the rendered .tex with the
English term LTR-wrapped and the formula in math mode. No TTS / no video.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_TERM = "Decision Boundary"
TEST_MATH = "f(x)=0"


def _png_size(p: Path):
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="validate_v2_latex_slides")
    ap.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    run_dir = Path(a.run) if Path(a.run).is_absolute() else (ROOT / a.run)
    run_dir = run_dir.resolve()
    smoke = run_dir / "v2_latex_slides" / "smoke"
    tex = smoke / "v2_smoke.tex"
    pdf = smoke / "v2_smoke.pdf"
    images = smoke / "images"

    checks = []
    # PDF exists + page count
    checks.append(("pdf_exists", pdf.is_file() and pdf.stat().st_size > 0))
    pages = None
    pinfo = shutil.which("pdfinfo")
    if pinfo and pdf.is_file():
        try:
            out = subprocess.run([pinfo, str(pdf)], capture_output=True, text=True, timeout=30).stdout
            for ln in out.splitlines():
                if ln.lower().startswith("pages:"):
                    pages = int(ln.split(":")[1].strip())
        except Exception:
            pass
    checks.append(("pdf_pages==3", pages == 3))

    # PNG dimensions ~16:9
    pngs = sorted(images.glob("v2_smoke-*.png")) if images.is_dir() else []
    ratio_ok = False
    dims = None
    if pngs:
        dims = _png_size(pngs[0])
        if dims:
            r = dims[0] / dims[1]
            ratio_ok = abs(r - 16 / 9) < 0.06
    checks.append(("png_count==3", len(pngs) == 3))
    checks.append(("png_ratio~16:9", ratio_ok))

    # mixed-text sentence rendered correctly in the .tex (LTR term + math mode)
    tex_src = tex.read_text(encoding="utf-8") if tex.is_file() else ""
    checks.append(("ltr_term_wrapped", ("\\lrterm{" + TEST_TERM + "}") in tex_src))
    checks.append(("formula_in_math_mode", ("$" + TEST_MATH + "$") in tex_src))
    checks.append(("rtl_engine_polyglossia", "\\setmainlanguage{persian}" in tex_src))

    print(f"[validate] pdf_pages={pages} png={len(pngs)} first_png_dims={dims}")
    allok = True
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("VERDICT:", "PASS" if allok else "FAIL")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
