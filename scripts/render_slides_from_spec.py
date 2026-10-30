"""render_slides_from_spec - Python renders the Claude-authored slide_spec.

Executor only: it makes NO teaching decisions. It reads slide_specs/*.json (Claude
source of truth) + source page images, builds a themed PPTX, and writes
slides/slide_manifest.json. Image export + manifest relink are done separately by
slide_image_exporter. Backs up any existing slides/ deck first.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation
from pptx.util import Inches, Emu
from pptx.enum.text import PP_ALIGN
import integrations.slide_theme as T
from integrations.source_crop import render_zoom, render_thumbnail
from integrations.pptx_to_pdf import detect_soffice, pptx_to_pdf

# spec type -> slide_manifest type enum
_TYPE_MAP = {"divider": "divider", "concept": "concept", "formula_card": "formula",
             "source_crop": "anchor", "comparison": "concept", "example": "example",
             "checkpoint": "quiz", "quiz": "quiz", "takeaway": "summary", "summary": "summary",
             "original_preview": "anchor", "original_guided_reading": "anchor",
             "original_recap": "anchor", "enriched_teaching": "concept"}
# slide types that show the original lecture page image
_ORIG_TYPES = {"original_preview", "original_guided_reading", "original_recap"}
_CARD_FILL = {"formula_card": ("navy", T.NAVY_FILL, T.NAVY), "comparison": ("gray", T.GRAY_FILL, T.GRAY)}


def _lines(text):
    if not text:
        return []
    parts = re.split(r"(?<=[.؟!])\s+|؛\s*|\s*\|\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def _fit(path, mw, mh):
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    sc = min(mw / w, mh / h)
    return w * sc, h * sc


def _place_img(s, path, ax, ay, mw, mh):
    """Center-fit an image inside (mw,mh) at anchor (ax,ay). Returns (w,h) in inches."""
    iw, ih = _fit(path, mw, mh)
    s.shapes.add_picture(str(path), Inches(ax + (mw - iw) / 2), Inches(ay + (mh - ih) / 2),
                         width=Inches(iw), height=Inches(ih))
    return iw, ih


def render(run_dir: Path) -> dict:
    run_dir = Path(run_dir).resolve()
    specs_dir = run_dir / "slide_specs"
    specs = sorted(specs_dir.glob("package_*_slide_spec.json"))
    if not specs:
        raise SystemExit("no slide_specs found")

    # backup existing slides/ deck
    sld = run_dir / "slides"
    if (sld / "full_teaching_deck.pptx").is_file():
        bak = sld / "_backup_pre_spec"
        bak.mkdir(parents=True, exist_ok=True)
        for f in ("full_teaching_deck.pptx", "full_teaching_deck.pdf", "slide_manifest.json"):
            if (sld / f).is_file():
                shutil.copy2(sld / f, bak / f)

    prs = Presentation()
    prs.slide_width = Emu(9144000); prs.slide_height = Emu(5143500)
    blank = prs.slide_layouts[6]
    manifest_slides = []
    gidx = 0
    pages_dir = run_dir / "extracted" / "pages"

    for spec_path in specs:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        pkg = spec["package_id"]; pkg_num = int(pkg.split("_")[1])
        src_pages = spec.get("source_pages", [])
        foot = f"بسته {T.fa_num(pkg_num)} از ۳ — درس ۶: دسته‌بندی خطی"
        for i, sl in enumerate(spec["slides"], start=1):
            gidx += 1
            s = prs.slides.add_slide(blank); T.set_bg(s)
            stype = sl["type"]; title = sl["title"]
            disp = sl.get("display_text_fa") or sl.get("display_text") or ""
            if stype in _ORIG_TYPES:
                # show the ORIGINAL lecture page; Persian card alongside it
                T.add_title_bar(s, title, _font())
                img_rel = sl.get("original_slide_image_path")
                img_abs = (run_dir / img_rel) if img_rel else None
                if (not img_abs or not img_abs.is_file()) and sl.get("source_page"):
                    img_abs = pages_dir / f"page_{int(sl['source_page']):03d}.png"
                # guided_reading: prefer a zoomed crop for readability
                if stype == "original_guided_reading" and sl.get("source_crop"):
                    sc = sl["source_crop"]; pid = sc["page_id"]; box = sc.get("crop_box", [0, 0, 1, 1])
                    zoom = run_dir / "source_crops" / f"spec_{pid}_zoom.png"
                    thumb = run_dir / "source_crops" / f"spec_{pid}_thumb.png"
                    render_zoom(pages_dir / f"{pid}.png", box, zoom)
                    render_thumbnail(pages_dir / f"{pid}.png", box, thumb)
                    _place_img(s, zoom, 5.05, 1.55, 4.55, 3.45)
                    tw, th = _fit(thumb, 1.25, 1.7)
                    s.shapes.add_picture(str(thumb), Inches(9.62 - tw), Inches(1.5), width=Inches(tw), height=Inches(th))
                elif img_abs and img_abs.is_file():
                    mh = 2.6 if stype == "original_recap" else 3.5
                    _place_img(s, img_abs, 5.05, 1.55, 4.55, mh)
                badge = {"original_preview": "نگاه اول به اسلاید اصلی",
                         "original_guided_reading": "خواندن هدایت‌شده",
                         "original_recap": "مرور اسلاید اصلی"}.get(stype, "")
                card = T.add_card(s, 0.5, 1.6, 4.35, 3.2, fill=T.TEAL_FILL, line=T.TEAL)
                T.fill_lines(card.text_frame, [(badge, T.TEAL, 14, True)] + _lines(disp), _font(), size=14, color=T.NAVY)
            elif stype == "divider":
                tb = T.add_textbox(s, 1.0, 2.0, 8.0, 1.8)
                T.fill_lines(tb.text_frame, [(title, T.NAVY, 34, True), (disp, T.TEAL, 17, False)], T.FONT if hasattr(T, "FONT") else _font(), align=PP_ALIGN.CENTER)
            elif stype == "source_crop" and sl.get("source_crop"):
                T.add_title_bar(s, title, _font())
                sc = sl["source_crop"]; pid = sc["page_id"]; box = sc.get("crop_box", [0, 0, 1, 1])
                zoom = run_dir / "source_crops" / f"spec_{pid}_zoom.png"
                thumb = run_dir / "source_crops" / f"spec_{pid}_thumb.png"
                render_zoom(pages_dir / f"{pid}.png", box, zoom)
                render_thumbnail(pages_dir / f"{pid}.png", box, thumb)
                zw, zh = _fit(zoom, 4.7, 3.4)
                s.shapes.add_picture(str(zoom), Inches(0.45 + (4.7 - zw) / 2), Inches(1.6 + (3.4 - zh) / 2), width=Inches(zw), height=Inches(zh))
                tw, th = _fit(thumb, 1.5, 2.0)
                s.shapes.add_picture(str(thumb), Inches(9.55 - tw), Inches(1.55), width=Inches(tw), height=Inches(th))
                card = T.add_card(s, 5.5, 1.6 + th + 0.1, 4.0, 4.5 - (1.6 + th + 0.1), fill=T.GRAY_FILL, line=T.GRAY)
                T.fill_lines(card.text_frame, _lines(disp), _font(), size=13, color=T.NAVY)
            elif stype == "checkpoint":
                T.add_title_bar(s, title, _font())
                q, a = disp, ""
                if "پاسخ:" in disp:
                    q, a = disp.split("پاسخ:", 1)
                qc = T.add_card(s, 0.6, 1.55, 8.8, 1.5, fill=T.TEAL_FILL, line=T.TEAL)
                T.fill_lines(qc.text_frame, [("پرسش", T.TEAL, 15, True)] + _lines(q.replace("پرسش:", "")), _font(), size=15, color=T.NAVY)
                if a:
                    ac = T.add_card(s, 0.6, 3.2, 8.8, 1.5, fill=T.WHITE, line=T.GRAY)
                    T.fill_lines(ac.text_frame, [("پاسخ", T.GRAY, 15, True)] + _lines(a), _font(), size=14, color=T.NAVY)
            else:  # concept / formula_card / comparison / takeaway
                T.add_title_bar(s, title, _font())
                _, fill, line = _CARD_FILL.get(stype, ("teal", T.TEAL_FILL, T.TEAL))
                card = T.add_card(s, 0.6, 1.6, 8.8, 2.9, fill=fill, line=line)
                T.fill_lines(card.text_frame, _lines(disp), _font(), size=16, color=T.NAVY)
            T.add_footer(s, foot, _font())

            manifest_slides.append({
                "slide_id": sl["slide_id"], "package_id": pkg,
                "slide_index_in_package": i, "slide_index_global": gidx,
                "title": title, "type": _TYPE_MAP.get(stype, "other"),
                "source_pages": list(src_pages), "image_path": None,
                "speaker_notes_excerpt": None, "duplicate_of": None, "slide_sha256": None,
            })

    sld.mkdir(parents=True, exist_ok=True)
    pptx_path = sld / "full_teaching_deck.pptx"
    prs.save(str(pptx_path))
    manifest = {"schema_version": "1.0.0", "run_id": run_dir.name,
                "deck_path": "slides/full_teaching_deck.pptx", "deck_pdf": None,
                "slide_count": len(manifest_slides), "slides": manifest_slides}
    (sld / "slide_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # also render PDF (soffice) so the deck is reviewable
    if detect_soffice():
        conv = pptx_to_pdf(pptx_path, sld)
        if conv.success:
            manifest["deck_pdf"] = "slides/full_teaching_deck.pdf"
            (sld / "slide_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[render_slides_from_spec] slides={len(manifest_slides)} pptx={pptx_path.stat().st_size}B pdf={(sld/'full_teaching_deck.pdf').is_file()}")
    return manifest


_FONT_CACHE = []


def _font():
    if not _FONT_CACHE:
        from agents.slide_designer_agent import _load_font_cascade, detect_persian_font, SLIDE_FALLBACK_FONT
        casc, _ = _load_font_cascade(ROOT)
        _FONT_CACHE.append(detect_persian_font(casc, final_fallback=SLIDE_FALLBACK_FONT))
    return _FONT_CACHE[0]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    rd = Path(a.run) if Path(a.run).is_absolute() else (ROOT / a.run)
    render(rd.resolve())
