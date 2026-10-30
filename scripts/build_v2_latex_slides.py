"""build_v2_latex_slides — build v2 XeLaTeX deep-tutor slides (no TTS/ffmpeg/video).

Phase E (this prompt): --smoke builds a 3-slide smoke deck (incl. the mixed
Persian/English/math test sentence) and compiles it to PDF. Full-deck rendering
from v2_deep_content is deferred to a later phase.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from integrations.latex_slide_renderer import (  # noqa: E402
    render_deck, compile_xelatex, render_slide_dict,
    render_source_slide, render_formula_card, render_question_card,
)

TEST_SENTENCE = "Decision Boundary یعنی مجموعه نقاطی که $f(x)=0$ است."


def build_smoke(run_dir: Path):
    page6 = run_dir / "extracted" / "pages" / "page_006.png"
    frames = [
        render_source_slide(
            page6,
            "نگاه اول به اسلاید اصلی — صفحهٔ ۶: سطح تصمیم",
            TEST_SENTENCE + " اینجا $f(x)=\\langle w,x\\rangle+b$ یک Linear Discriminant Function است.",
        ),
        render_formula_card(
            "تابع تفکیک خطی (Linear Discriminant Function)",
            r"f(x)=\langle w,x\rangle + b,\quad x,w\in\mathbb{R}^d,\ b\in\mathbb{R}",
            "در این رابطه $w$ همان weight و $b$ همان bias است؛ علامتِ $f(x)$ کلاس را تعیین می‌کند.",
        ),
        render_question_card(
            "ایست بازرسی",
            "چرا بردار weight $w$ بر Decision Boundary عمود است؟",
            "چون برای هر دو نقطهٔ روی مرز $\\langle w,\\,x_a-x_b\\rangle=0$ است، پس $w$ بر هر بردارِ روی مرز عمود می‌ماند.",
        ),
    ]
    out_tex = run_dir / "v2_latex_slides" / "smoke" / "v2_smoke.tex"
    render_deck(frames, out_tex, deck_title="v2 Deep Tutor — Smoke")
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=1, timeout=180)
    print(f"[build_v2_latex_slides] tex={out_tex}")
    print(f"[build_v2_latex_slides] compiled={ok} pdf={pdf}")
    if not ok:
        print("---- xelatex log tail ----")
        print(log_tail)
        return 1
    print(f"[build_v2_latex_slides] PDF bytes={pdf.stat().st_size}")
    return 0


def build_package(run_dir: Path, package_id: str):
    spec_path = run_dir / "v2_slide_specs" / f"{package_id}_v2_slide_spec.json"
    if not spec_path.is_file():
        print(f"[ERROR] slide spec not found: {spec_path}", file=sys.stderr); return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    slides = sorted(spec["slides"], key=lambda s: s.get("order", 0))
    frames = [render_slide_dict(s, run_dir) for s in slides]
    out_tex = run_dir / "v2_latex_slides" / f"{package_id}_sample.tex"
    render_deck(frames, out_tex, deck_title=f"v2 Deep Tutor — {package_id}")
    # two passes so beamer frame numbers settle
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=2, timeout=300)
    print(f"[build_v2_latex_slides] {package_id}: slides={len(frames)} tex={out_tex}")
    print(f"[build_v2_latex_slides] compiled={ok} pdf={pdf}")
    if not ok:
        print("---- xelatex log tail ----"); print(log_tail); return 1
    print(f"[build_v2_latex_slides] PDF bytes={pdf.stat().st_size}")
    return 0


def build_full(run_dir: Path):
    import shutil
    import subprocess
    pkgs = ["package_001", "package_002", "package_003"]
    all_slides = []          # (package_id, slide_dict)
    frames = []
    for pid in pkgs:
        spec_path = run_dir / "v2_slide_specs" / f"{pid}_v2_slide_spec.json"
        if not spec_path.is_file():
            print(f"[ERROR] missing spec: {spec_path}", file=sys.stderr); return 2
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        for s in sorted(spec["slides"], key=lambda x: x.get("order", 0)):
            frames.append(render_slide_dict(s, run_dir))
            all_slides.append((pid, s))
    out_tex = run_dir / "v2_latex_slides" / "v2_full_deep_tutor_deck.tex"
    render_deck(frames, out_tex, deck_title="درس ۶: دسته‌بندی خطی — v2 Deep Tutor")
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=2, timeout=600)
    print(f"[build_v2_latex_slides] FULL: slides={len(frames)} tex={out_tex} compiled={ok} pdf={pdf}")
    if not ok:
        print("---- xelatex log tail ----"); print(log_tail); return 1

    # export PNGs as slide_NNN.png
    img_dir = run_dir / "v2_slide_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    for old in img_dir.glob("slide_*.png"):
        old.unlink()
    ppm = shutil.which("pdftoppm")
    tmp_prefix = str(img_dir / "_full")
    subprocess.run([ppm, "-png", "-r", "150", str(pdf), tmp_prefix],
                   capture_output=True, text=True, timeout=300, check=True)
    raw = sorted(img_dir.glob("_full-*.png"))
    manifest_slides = []
    for i, (p, (pid, s)) in enumerate(zip(raw, all_slides), start=1):
        dest = img_dir / f"slide_{i:03d}.png"
        p.rename(dest)
        manifest_slides.append({
            "index": i, "image": f"v2_slide_images/slide_{i:03d}.png",
            "package_id": pid, "slide_id": s.get("slide_id"), "type": s.get("type"),
            "title_fa": s.get("title_fa"), "source_page": s.get("source_page"),
        })
    manifest = {"run_id": run_dir.name, "deck_pdf": "v2_latex_slides/v2_full_deep_tutor_deck.pdf",
                "render_engine": "xelatex_beamer", "slide_count": len(manifest_slides),
                "packages": pkgs, "slides": manifest_slides}
    (run_dir / "v2_slide_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build_v2_latex_slides] PDF bytes={pdf.stat().st_size} pngs={len(manifest_slides)} manifest=v2_slide_manifest.json")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_v2_latex_slides")
    ap.add_argument("--run", required=True)
    ap.add_argument("--smoke", action="store_true", help="build the 3-slide smoke deck")
    ap.add_argument("--package", default=None, help="build ONE package sample from its v2 slide spec")
    ap.add_argument("--full", action="store_true", help="build the FULL deck (all 3 packages) + PNGs + manifest")
    a = ap.parse_args(argv)
    run_dir = Path(a.run) if Path(a.run).is_absolute() else (ROOT / a.run)
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run not found: {run_dir}", file=sys.stderr); return 2
    if a.full:
        return build_full(run_dir)
    if a.package:
        return build_package(run_dir, a.package)
    if a.smoke:
        return build_smoke(run_dir)
    print("[build_v2_latex_slides] specify --smoke | --package <id> | --full."); return 2


if __name__ == "__main__":
    sys.exit(main())
