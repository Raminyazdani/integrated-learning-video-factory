"""export_v2_latex_slide_images — XeLaTeX-built PDF -> per-slide PNG (pdftoppm).

No ffmpeg / no video. Exports PNGs for visual QA of the v2 LaTeX deck.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def export(pdf_path: Path, out_dir: Path, dpi: int = 150):
    pdf_path = Path(pdf_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ppm = shutil.which("pdftoppm")
    if not ppm:
        print("[export] pdftoppm not found on PATH"); return None
    if not pdf_path.is_file():
        print(f"[export] PDF not found: {pdf_path}"); return None
    prefix = str(out_dir / pdf_path.stem)
    try:
        subprocess.run([ppm, "-png", "-r", str(dpi), str(pdf_path), prefix],
                       capture_output=True, text=True, timeout=120, check=True)
    except Exception as e:
        print(f"[export] pdftoppm failed: {e!r}"); return None
    pngs = sorted(out_dir.glob(pdf_path.stem + "-*.png"))
    print(f"[export] {len(pngs)} PNG(s) @ {dpi}dpi -> {out_dir}")
    for p in pngs:
        print("  ", p.name, p.stat().st_size, "bytes")
    return pngs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="export_v2_latex_slide_images")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args(argv)
    pdf = Path(a.pdf) if Path(a.pdf).is_absolute() else (ROOT / a.pdf)
    out = Path(a.out) if a.out else pdf.parent / "images"
    pngs = export(pdf, out, a.dpi)
    return 0 if pngs else 1


if __name__ == "__main__":
    sys.exit(main())
