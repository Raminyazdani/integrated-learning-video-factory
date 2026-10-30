"""build_lessonbook_pdf — render the canonical Persian lessonbook PDF (XeLaTeX).

Modes:
  --smoke            tiny 2-page smoke test (templates + macros + RTL/LTR + one
                     screenshot + one mixed sentence + one formula box).
  --run <run_dir>    full build from lessonbook_full/ narrative artifacts (Prompt 8).

Python is executor/renderer ONLY: it never authors educational content.
No TTS / no ffmpeg / no video / no paid APIs. XeLaTeX is canonical.

Usage:
  python scripts/build_lessonbook_pdf.py --smoke
  python scripts/build_lessonbook_pdf.py --run runs/<id>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.lessonbook_latex_renderer import (  # noqa: E402
    render_chapter_tex, render_book, render_chapter_markdown,
    compile_xelatex, page_count_from_log, build_from_run,
)

SMOKE_DIR = ROOT / "runs" / "lessonbook_renderer_smoke_test"


def _find_sample_image() -> Path | None:
    """Pick ANY existing source page image to embed in the smoke (read-only), so the
    renderer smoke test is project-agnostic (works for any run, not a fixed one)."""
    runs = ROOT / "runs"
    for pat in ("*/extracted/pages/*.png", "*/v2_slide_images/slide_*.png", "*/slide_images/*.png"):
        for c in sorted(runs.glob(pat)):
            if c.is_file():
                return c
    return None


def _smoke_narrative(img_rel: str | None) -> dict:
    """A minimal schema-shaped chapter that exercises every macro. NOT real teaching
    content — a renderer smoke fixture only."""
    beats = [
        {"beat_id": "b1", "type": "opening_intuition",
         "prose_fa": "فرض کن می‌خواهی دو دستهٔ نقطه را روی صفحه از هم جدا کنی. ساده‌ترین ایده کشیدن یک خط مرزی است. "
                     "Decision Boundary یعنی مجموعه نقاطی که $f(x)=0$ است."},
    ]
    if img_rel:
        beats.append({"beat_id": "b2", "type": "source_anchor",
                      "source_anchor": {"image_path": img_rel,
                                        "what_to_notice_fa": "به محوری که دو کلاس را جدا می‌کند دقت کن."},
                      "prose_fa": "در این صفحهٔ اصلی، همان خط جداکننده را می‌بینیم؛ در ادامه آرام‌آرام معنایش را می‌سازیم."})
    beats += [
        {"beat_id": "b3", "type": "term_intro", "term_en": "Linear Discriminant Function",
         "gloss_fa": "تابعی که به هر ورودی یک عدد نسبت می‌دهد",
         "prose_fa": "همین تابع است که آن خط مرزی را می‌سازد."},
        {"beat_id": "b4", "type": "guided_explanation",
         "prose_fa": "هر جا خروجی این تابع صفر شود، دقیقاً روی مرز هستیم؛ یک طرف مثبت و طرف دیگر منفی می‌شود."},
        {"beat_id": "b5", "type": "formula",
         "prose_fa": "حالا رابطه را دقیق بنویسیم."},
        {"beat_id": "b6", "type": "recap",
         "prose_fa": "پس Decision Boundary همان جایی است که $f(x)=0$؛ و این مرز را یک تابع خطی ساده می‌سازد. در فصل بعد سراغ یادگیری وزن‌ها می‌رویم."},
    ]
    return {
        "schema_version": "1.0.0", "run_id": "lessonbook_renderer_smoke_test",
        "chapter_id": "chapter_001", "package_id": "package_002",
        "provenance": {"authored_by": "CLAUDE_CODE_MANUAL_AGENT"},
        "source_pages": [6], "title_fa": "مرز تصمیم و تابع تفکیک خطی",
        "layer": "A_to_B_bridge",
        "beats": beats,
        "term_intros": [{"term_en": "Linear Discriminant Function",
                         "gloss_fa": "تابعی که به هر ورودی یک عدد نسبت می‌دهد", "first_use_beat": "b3"}],
        "formulas": [{
            "formula_latex": r"f(x) = w^\top x + b", "motivation_fa": "می‌خواهیم برای هر نقطه یک عدد بسازیم که علامتش طرفِ مرز را بگوید.",
            "symbols": [{"symbol": "w", "meaning_fa": "بردار وزن‌ها که جهت مرز را تعیین می‌کند"},
                        {"symbol": "b", "meaning_fa": "مقدار ثابت که مرز را جابه‌جا می‌کند"}],
            "numeric_example_fa": "اگر $w=(1,0)$ و $b=0$ باشد، آنگاه $f(x)=x_1$ و مرز همان محور عمودی است.",
            "geometric_fa": "بردار $w$ عمود بر مرز است.",
            "common_mistake_fa": "خیلی‌ها مرز را با خودِ تابع اشتباه می‌گیرند؛ مرز فقط جایی است که تابع صفر می‌شود.",
            "attaches_to_beat": "b5"}],
        "exam_insights": [{"near_concept": "Decision Boundary", "text_fa": "اگر در امتحان فقط علامت $f(x)$ را خواستند، نیازی به محاسبهٔ فاصله نیست.",
                           "attaches_to_beat": "b4"}],
        "practice": [{"after_idea": "Decision Boundary", "question_fa": "اگر $f(x)=2x_1-4$ باشد، مرز کجاست؟",
                      "answer_fa": "آن‌جا که $f(x)=0$، یعنی $x_1=2$.", "attaches_to_beat": "b4"}],
        "recap_fa": "Decision Boundary همان $f(x)=0$ است.",
        "confidence": {"foundation": 0.9, "math": 0.95, "coherence": 0.9},
        "style_constraints": {"no_glossary_dump": True, "no_robotic_headings": True,
                              "terms_inline_first_use": True, "prose_first": True},
    }


def run_smoke() -> int:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    src = _find_sample_image()
    img_rel = None
    if src:
        dest = SMOKE_DIR / "sample_source.png"
        shutil.copy(str(src), str(dest))
        img_rel = "sample_source.png"
        print(f"[smoke] using sample image: {src.name} -> {dest.name}")
    else:
        print("[smoke] no sample image found; rendering without screenshot")

    nar = _smoke_narrative(img_rel)
    chapter_tex = render_chapter_tex(nar, 1, SMOKE_DIR)
    out_tex = SMOKE_DIR / "lessonbook_smoke.tex"
    render_book([chapter_tex], out_tex, book_title="نمونهٔ رندر کتابچهٔ درسی (Smoke Test)")
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=2)
    pages = page_count_from_log(out_tex.with_suffix(".log"))

    manifest = {
        "schema_version": "1.0.0", "run_id": "lessonbook_renderer_smoke_test",
        "source_file": "smoke", "renderer": "xelatex", "engine": "xelatex",
        "tex_path": str(out_tex.relative_to(ROOT)).replace("\\", "/"),
        "pdf_path": (str(Path(pdf).relative_to(ROOT)).replace("\\", "/") if pdf else None),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": pages or 0,
        "provenance": {"authored_by": "python_executor"},
        "chapters": [{"chapter_id": "chapter_001", "package_id": "package_002",
                      "title_fa": "مرز تصمیم و تابع تفکیک خطی", "source_pages": [6],
                      "screenshots": ([{"image_path": "sample_source.png", "page": 6}] if img_rel else [])}],
        "images": ([{"image_path": "sample_source.png", "exists": True, "page": 6}] if img_rel else []),
    }
    (SMOKE_DIR / "lessonbook_pdf_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[smoke] compiled_ok={ok} pages={pages} pdf={pdf}")
    if not ok:
        print("---- log tail ----")
        print(log_tail[-1500:])
        return 3
    return 0


def run_chapter(narrative_path_str: str) -> int:
    """Render ONE chapter from a lessonbook_chapter_narrative.json:
    -> <stem>.tex + <stem>.pdf + <name>.md (prose twin) + lessonbook_pdf_manifest.json.
    Python renders/serializes only; it authors no educational content."""
    np_ = Path(narrative_path_str).resolve()
    if not np_.is_file():
        print(f"[chapter] narrative not found: {np_}")
        return 3
    nar = json.loads(np_.read_text(encoding="utf-8"))
    run_dir = np_.parent.parent  # runs/<id>/  (narrative lives under lessonbook_sample|full/)
    cid = nar.get("chapter_id", "chapter_001")
    m = re.search(r"(\d+)", cid)
    number = int(m.group(1)) if m else 1
    chapter_tex = render_chapter_tex(nar, number, run_dir)

    stem = np_.name
    for suf in ("_narrative.json", ".json"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    tex_out = np_.parent / (stem + ".tex")
    render_book([chapter_tex], tex_out, book_title="")
    ok, pdf, log_tail = compile_xelatex(tex_out, runs=2)
    pages = page_count_from_log(tex_out.with_suffix(".log"))

    md_out = np_.with_suffix(".md")
    md_out.write_text(render_chapter_markdown(nar), encoding="utf-8")

    # Record EVERY visual actually placed in the chapter (visuals[] + legacy source_anchor),
    # enriched from the visual-asset manifest (source_page / asset_type / adapted / caption).
    _ADAPTED = {"annotated_crop", "masked_variant", "generated_teaching_diagram", "side_by_side"}
    vam_index = {}
    for cand in list(np_.parent.glob("*_visual_asset_manifest.json")) + list(np_.parent.glob("visual_asset_manifest.json")):
        try:
            for a in json.loads(cand.read_text(encoding="utf-8")).get("assets", []):
                if a.get("asset_id"):
                    vam_index[a["asset_id"]] = a
        except Exception:
            pass
    images, _seen = [], set()

    def _add_img(path, *, asset_id=None, source_page=None, asset_type=None, caption_fa=""):
        if not path or path in _seen:
            return
        _seen.add(path)
        meta = vam_index.get(asset_id, {}) if asset_id else {}
        atype = asset_type or meta.get("asset_type")
        fp = Path(path)
        images.append({
            "asset_id": asset_id,
            "image_path": path,
            "source_page": source_page if source_page is not None else meta.get("source_page"),
            "asset_type": atype,
            "caption_fa": caption_fa or meta.get("caption_fa", ""),
            "is_adapted_teaching_visual": bool(meta.get("is_adapted_teaching_visual", atype in _ADAPTED)),
            "used_in_pdf": True,
            "exists": (fp.is_file() if fp.is_absolute() else (run_dir / path).is_file()),
        })

    for v in nar.get("visuals", []):
        _add_img(v.get("path"), asset_id=v.get("asset_id"), asset_type=(v.get("kind") or v.get("asset_type")), caption_fa=v.get("caption_fa", ""))
    for b in nar.get("beats", []):
        sa = b.get("source_anchor") or {}
        if sa.get("image_path"):
            _add_img(sa["image_path"], source_page=sa.get("page"), asset_type="full_slide_anchor", caption_fa=sa.get("what_to_notice_fa", ""))

    screenshots = [{"image_path": im["image_path"], "page": im.get("source_page"),
                    "asset_id": im.get("asset_id"), "asset_type": im.get("asset_type")} for im in images]
    manifest = {
        "schema_version": "1.0.0", "run_id": nar.get("run_id", ""),
        "source_file": nar.get("source_file", "unknown"), "renderer": "xelatex", "engine": "xelatex",
        "tex_path": str(tex_out).replace("\\", "/"),
        "pdf_path": (str(pdf).replace("\\", "/") if pdf else None),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": pages or 0,
        "provenance": {"authored_by": "python_executor"},
        "chapters": [{"chapter_id": cid, "package_id": nar.get("package_id", ""),
                      "title_fa": nar.get("title_fa", ""), "source_pages": nar.get("source_pages", []),
                      "screenshots": screenshots}],
        "images": images,
    }
    (np_.parent / "lessonbook_pdf_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[chapter] compiled_ok={ok} pages={pages} pdf={pdf}")
    print(f"[chapter] md={md_out}")
    if not ok:
        print("---- log tail ----"); print((log_tail or "")[-1500:])
        return 3
    return 0


def run_book(book_dir_str: str, stem: str = "final_lessonbook") -> int:
    """Assemble all chapters in a book folder (lessonbook_outline.json + chapter_*_narrative.json)
    into ONE XeLaTeX book -> final_lessonbook.{tex,pdf} + an aggregated lessonbook_pdf_manifest.json
    that records EVERY visual across chapters. Python renders/serializes only; authors nothing."""
    _ADAPTED = {"annotated_crop", "masked_variant", "generated_teaching_diagram", "side_by_side"}
    book_dir = Path(book_dir_str).resolve()
    run_dir = book_dir.parent
    outline_p = book_dir / "lessonbook_outline.json"
    if not outline_p.is_file():
        print(f"[book] outline not found: {outline_p}")
        return 3
    outline = json.loads(outline_p.read_text(encoding="utf-8"))

    vam_index = {}
    for cand in list(book_dir.glob("*_visual_asset_manifest.json")) + list(book_dir.glob("visual_asset_manifest.json")):
        try:
            for a in json.loads(cand.read_text(encoding="utf-8")).get("assets", []):
                if a.get("asset_id"):
                    vam_index[a["asset_id"]] = a
        except Exception:
            pass

    images, seen = [], set()

    def add_img(path, *, asset_id=None, source_page=None, asset_type=None, caption_fa=""):
        if not path or path in seen:
            return next((im for im in images if im["image_path"] == path), None)
        seen.add(path)
        meta = vam_index.get(asset_id, {}) if asset_id else {}
        atype = asset_type or meta.get("asset_type")
        fp = Path(path)
        entry = {
            "asset_id": asset_id, "image_path": path,
            "source_page": source_page if source_page is not None else meta.get("source_page"),
            "asset_type": atype, "caption_fa": caption_fa or meta.get("caption_fa", ""),
            "is_adapted_teaching_visual": bool(meta.get("is_adapted_teaching_visual", atype in _ADAPTED)),
            "used_in_pdf": True,
            "exists": (fp.is_file() if fp.is_absolute() else (run_dir / path).is_file()),
        }
        images.append(entry)
        return entry

    chapters_tex, pm_chapters = [], []
    for ch in sorted(outline.get("chapters", []), key=lambda c: c.get("order", 0)):
        cid = ch.get("chapter_id")
        nar_p = book_dir / f"{cid}_narrative.json"
        if not nar_p.is_file():
            print(f"[book] missing narrative: {nar_p}")
            return 3
        nar = json.loads(nar_p.read_text(encoding="utf-8"))
        chapters_tex.append(render_chapter_tex(nar, ch.get("order", len(chapters_tex) + 1), run_dir))
        shots = []
        for v in nar.get("visuals", []):
            e = add_img(v.get("path"), asset_id=v.get("asset_id"), asset_type=(v.get("kind") or v.get("asset_type")), caption_fa=v.get("caption_fa", ""))
            shots.append({"image_path": v.get("path"), "page": (e or {}).get("source_page"), "asset_id": v.get("asset_id"), "asset_type": v.get("kind")})
        for b in nar.get("beats", []):
            sa = b.get("source_anchor") or {}
            if sa.get("image_path"):
                add_img(sa["image_path"], source_page=sa.get("page"), asset_type="full_slide_anchor", caption_fa=sa.get("what_to_notice_fa", ""))
                shots.append({"image_path": sa["image_path"], "page": sa.get("page")})
        pm_chapters.append({"chapter_id": cid, "package_id": nar.get("package_id", ""),
                            "title_fa": nar.get("title_fa", ""), "source_pages": nar.get("source_pages", []),
                            "screenshots": shots})

    out_tex = book_dir / f"{stem}.tex"
    render_book(chapters_tex, out_tex, book_title=outline.get("book_title_fa", ""))
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=2)
    pages = page_count_from_log(out_tex.with_suffix(".log"))
    manifest = {
        "schema_version": "1.0.0", "run_id": outline.get("run_id", ""),
        "source_file": outline.get("source_file", "unknown"),
        "renderer": "xelatex", "engine": "xelatex",
        "tex_path": str(out_tex).replace("\\", "/"),
        "pdf_path": (str(pdf).replace("\\", "/") if pdf else None),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": pages or 0,
        "provenance": {"authored_by": "python_executor"},
        "chapters": pm_chapters, "images": images,
    }
    manifest_name = "lessonbook_pdf_manifest" + stem[len("final_lessonbook"):] + ".json"
    (book_dir / manifest_name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[book] compiled_ok={ok} pages={pages} chapters={len(chapters_tex)} images={len(images)} manifest={manifest_name} pdf={pdf}")
    if not ok:
        print("---- log tail ----"); print((log_tail or "")[-1800:])
        return 3
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Persian lessonbook PDF (XeLaTeX).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--smoke", action="store_true", help="tiny 2-page renderer smoke test")
    g.add_argument("--chapter", type=str, help="render ONE chapter from a lessonbook_chapter_narrative.json")
    g.add_argument("--book", type=str, help="render a full book folder (outline + chapter_*_narrative.json)")
    g.add_argument("--run", type=str, help="run dir for a full lessonbook build (lessonbook_full/)")
    ap.add_argument("--stem", default="final_lessonbook", help="output stem for --book (e.g. final_lessonbook_polished)")
    args = ap.parse_args()
    if args.smoke:
        return run_smoke()
    if args.chapter:
        return run_chapter(args.chapter)
    if args.book:
        return run_book(args.book, args.stem)
    res = build_from_run(args.run)
    print(json.dumps({k: v for k, v in res.items() if k != "log_tail"}, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        print("---- log tail ----"); print((res.get("log_tail") or "")[-1500:])
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
