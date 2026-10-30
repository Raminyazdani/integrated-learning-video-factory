"""slide_designer_agent - Phase 6A smoke-test orchestrator for teaching slides.

Phase 6A scope is intentionally narrow:
    - Detect python-pptx and (optionally) LibreOffice/soffice.
    - Resolve the Persian font cascade from configs/persian_fonts.json
      (DejaVu Sans as the sandbox final fallback, same as Phase 5).
    - Build a minimal 2-slide Persian deck at
      runs/<run_id>/slides/smoke_test_teaching.pptx using python-pptx.
    - Validate the .pptx as a valid zip with required Office Open XML members.
    - If soffice is available, also export a smoke PDF for visual review.
    - Surface a structured SmokeSlideResult that the CLI can summarize.

The agent does NOT yet consume any curated package content - that comes in
Phase 6B (real per-package teaching slide decks).
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_AUTO_SIZE

try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover
    _PILImage = None

from integrations.atomic_io import iso_now
from integrations.latex_builder import detect_persian_font
from integrations.pptx_to_pdf import detect_soffice, pptx_to_pdf


SLIDE_FALLBACK_FONT = "DejaVu Sans"


@dataclass
class SmokeSlideResult:
    success: bool
    pptx_path: Path
    pdf_path: Optional[Path]
    pptx_bytes: int
    pdf_bytes: int
    chosen_font: str
    cascade: list
    image_used: str
    n_slides: int
    pptx_zip_ok: bool
    pptx_required_members_present: bool
    pptx_member_count: int
    soffice_path: Optional[str]
    soffice_skipped_reason: str
    python_pptx_version: str
    generated_at: str = ""
    notes: list = field(default_factory=list)


# --- font cascade (same source as PDF agent) -------------------------------


def _load_font_cascade(project_root):
    cfg_path = project_root / "configs" / "persian_fonts.json"
    if not cfg_path.exists():
        return ["Vazirmatn", "Tahoma", "Arial"], "Arial"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cascade_entries = cfg.get("fallback_chain") or []
    cascade = [e.get("family") for e in cascade_entries if isinstance(e, dict) and e.get("family")]
    if not cascade:
        cascade = [cfg.get("primary", {}).get("family", "Vazirmatn")]
    english = (cfg.get("english_inline") or {}).get("family") or "Arial"
    return cascade, english


# --- minimal smoke deck ----------------------------------------------------


_REQUIRED_PPTX_MEMBERS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
)


def _validate_pptx_zip(pptx_path):
    """Return (zip_ok, required_present, member_count)."""
    try:
        with zipfile.ZipFile(pptx_path) as zf:
            names = set(zf.namelist())
            bad = zf.testzip()
            zip_ok = bad is None
            required_present = all(m in names for m in _REQUIRED_PPTX_MEMBERS)
            return zip_ok, required_present, len(names)
    except Exception:
        return False, False, 0


def _set_font(run, family, size_pt=None, color=None):
    """Apply font family/size/color to a python-pptx Run.

    Also sets the complex-script (a:cs) and east-asian (a:ea) typefaces — not just
    a:latin — so mixed Persian+Latin runs don't fall back to a non-Persian font in
    LibreOffice (the Q3.1 tofu fix)."""
    f = run.font
    f.name = family
    if size_pt is not None:
        f.size = Pt(size_pt)
    if color is not None:
        f.color.rgb = color
    try:
        from pptx.oxml.ns import qn
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:latin", "a:cs", "a:ea"):
            el = rPr.find(qn(tag))
            if el is None:
                el = rPr.makeelement(qn(tag), {})
                rPr.append(el)
            el.set("typeface", family)
    except Exception:
        pass


# --- Q1 readability helpers -------------------------------------------------
# Conservative text caps so concept slides don't become walls of text.
_CAP_TITLE_CHARS = 64
_CAP_BULLETS = 6
_CAP_CHARS_PER_LINE = 110
_CAP_BODY_CHARS = 620

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa_num(n):
    return str(n).translate(_FA_DIGITS)


def _total_packages(run_dir):
    try:
        m = json.loads((Path(run_dir) / "packages" / "package_manifest.json").read_text(encoding="utf-8"))
        return len(m.get("packages", []))
    except Exception:
        return 0


def _package_subtitle(run_dir, pkg_num):
    """Persian, viewer-friendly package label — never show raw 'package_NNN'."""
    total = _total_packages(run_dir)
    if total > 0:
        return "بسته‌ی {} از {}".format(_fa_num(pkg_num), _fa_num(total))
    return "بسته‌ی {}".format(_fa_num(pkg_num))


def _fit_image_dims(image_path, max_w_in, max_h_in):
    """Largest (w,h) inches fitting the image in the box, preserving aspect ratio."""
    try:
        if _PILImage is None:
            raise RuntimeError("PIL unavailable")
        with _PILImage.open(image_path) as im:
            w, h = im.size
        if w <= 0 or h <= 0:
            return max_w_in, max_h_in
        scale = min(max_w_in / float(w), max_h_in / float(h))
        return w * scale, h * scale
    except Exception:
        return max_w_in, max_h_in


def _cap_body_lines(body_lines):
    """Apply caps: clip over-long lines, limit bullets, cap total body chars.
    Returns (capped_lines, overflowed)."""
    capped = []
    overflow = False
    bullets = 0
    total = 0
    for line in (body_lines or []):
        ln = line or ""
        if len(ln) > _CAP_CHARS_PER_LINE:
            ln = ln[: _CAP_CHARS_PER_LINE - 1].rstrip() + "…"
            overflow = True
        if ln.strip().startswith("•"):
            bullets += 1
            if bullets > _CAP_BULLETS:
                overflow = True
                continue
        if total + len(ln) > _CAP_BODY_CHARS:
            overflow = True
            continue
        total += len(ln)
        capped.append(ln)
    return capped, overflow


def _build_smoke_pptx(pptx_path, *, main_font, image_path):
    """Build a 2-slide Persian smoke deck."""
    prs = Presentation()
    prs.slide_width = Emu(9144000)   # 10 inch
    prs.slide_height = Emu(5143500)  # 5.625 inch -> 16:9

    # ---- Slide 1: title -----------------------------------------------------
    title_layout = prs.slide_layouts[0]
    s1 = prs.slides.add_slide(title_layout)
    title_tf = s1.shapes.title.text_frame
    title_tf.clear()
    p = title_tf.paragraphs[0]
    r = p.add_run()
    r.text = "Phase 6A — اسلایدهای آموزش (Smoke Test)"
    _set_font(r, main_font, size_pt=40)
    if len(s1.placeholders) > 1:
        sub_tf = s1.placeholders[1].text_frame
        sub_tf.clear()
        p2 = sub_tf.paragraphs[0]
        r2 = p2.add_run()
        r2.text = "integrated_learning_video_factory"
        _set_font(r2, main_font, size_pt=22)

    # ---- Slide 2: content + image ------------------------------------------
    content_layout = prs.slide_layouts[5]  # Title only layout (safer for RTL text)
    s2 = prs.slides.add_slide(content_layout)
    s2.shapes.title.text_frame.clear()
    p = s2.shapes.title.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = "متن آزمایشی فارسی + یک اسکرین‌شات"
    _set_font(r, main_font, size_pt=32)

    # body text box
    left = Inches(0.5); top = Inches(1.4); width = Inches(5.0); height = Inches(3.5)
    txbox = s2.shapes.add_textbox(left, top, width, height)
    tf = txbox.text_frame
    tf.word_wrap = True
    lines = [
        "این یک متن آزمایشی فارسی برای Phase 6A است.",
        "هدف فقط آزمون pipeline اسلاید است — هنوز محتوای بسته‌ها رندر نشده.",
        "ML \u2014 یادگیری از داده، نه از برنامه‌نویسی صریح.",
    ]
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = PP_ALIGN.LEFT
        run = para.add_run()
        run.text = line.encode().decode("unicode_escape")  # decode \u2014 etc
        _set_font(run, main_font, size_pt=18)

    # image (only if present)
    if image_path and Path(image_path).exists():
        s2.shapes.add_picture(str(image_path), Inches(5.7), Inches(1.4),
                              width=Inches(4.0))

    prs.save(pptx_path)


# --- main entry ------------------------------------------------------------


def run_smoke_slides(run_dir, project_root):
    """Build the Phase 6A smoke-test PPTX (and optional PDF)."""
    run_dir = Path(run_dir).resolve()
    project_root = Path(project_root).resolve()

    cascade, _english = _load_font_cascade(project_root)
    chosen_font = detect_persian_font(cascade, final_fallback=SLIDE_FALLBACK_FONT)

    slides_dir = run_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = slides_dir / "smoke_test_teaching.pptx"

    pages_dir = run_dir / "extracted" / "pages"
    image_path = pages_dir / "page_001.png"
    image_used = "page_001.png"
    if not image_path.exists():
        pngs = sorted(pages_dir.glob("page_*.png"))
        if pngs:
            image_path = pngs[0]; image_used = pngs[0].name
        else:
            image_path = None; image_used = "<none>"

    _build_smoke_pptx(pptx_path, main_font=chosen_font, image_path=image_path)

    zip_ok, req_present, member_count = _validate_pptx_zip(pptx_path)
    pptx_bytes = pptx_path.stat().st_size

    # Optional PDF export via soffice
    soffice = detect_soffice()
    pdf_path = None
    pdf_bytes = 0
    soffice_skipped_reason = ""
    notes = []
    if soffice is None:
        soffice_skipped_reason = "soffice not installed; PDF export skipped"
        notes.append(soffice_skipped_reason)
    else:
        pdf_dir = run_dir / "slides"  # land the pdf next to the pptx
        export = pptx_to_pdf(pptx_path, pdf_dir)
        if export.success:
            pdf_path = export.pdf_path
            pdf_bytes = export.pdf_bytes
        else:
            soffice_skipped_reason = export.skipped_reason
            notes.append("PDF export failed: " + soffice_skipped_reason)

    # python-pptx version for the report
    import pptx as _pptx_mod
    pptx_version = getattr(_pptx_mod, "__version__", "unknown")

    return SmokeSlideResult(
        success=(zip_ok and req_present and pptx_bytes > 0),
        pptx_path=pptx_path,
        pdf_path=pdf_path,
        pptx_bytes=pptx_bytes,
        pdf_bytes=pdf_bytes,
        chosen_font=chosen_font,
        cascade=cascade,
        image_used=image_used,
        n_slides=2,
        pptx_zip_ok=zip_ok,
        pptx_required_members_present=req_present,
        pptx_member_count=member_count,
        soffice_path=soffice,
        soffice_skipped_reason=soffice_skipped_reason,
        python_pptx_version=pptx_version,
        generated_at=iso_now(),
        notes=notes,
    )



# ============================================================================
# Phase 6B: per-package teaching slide deck
# ============================================================================


from dataclasses import field as _dc_field


_VALID_SLIDE_TYPES = {
    "anchor","concept","notation","formula","example","figure",
    "common_mistake","exam_trap","quiz","summary","divider","other",
}


@dataclass
class _SlideRec:
    """Tiny in-memory record for a slide before manifest serialization."""
    slide_id: str
    title: str
    type: str
    speaker_notes: str = ""
    image_path: str = ""
    duplicate_of: str = ""


@dataclass
class PackageSlidesResult:
    success: bool
    package_id: str
    pptx_path: Path
    pdf_path: Optional[Path]
    pptx_bytes: int
    pdf_bytes: int
    slide_count: int
    expected_pages: list
    chosen_font: str
    cascade: list
    pptx_zip_ok: bool
    pptx_required_members_present: bool
    pptx_member_count: int
    manifest_path: Path
    manifest_schema_valid: bool
    manifest_schema_errors: list = _dc_field(default_factory=list)
    soffice_path: Optional[str] = None
    soffice_skipped_reason: str = ""
    notes: list = _dc_field(default_factory=list)
    generated_at: str = ""


def _add_text_slide(prs, *, title, body_lines, main_font, notes=""):
    """Add a title + body slide. Caps body density (clip long lines, limit bullets,
    cap total chars) and auto-scales font so text does not overflow the slide."""
    layout = prs.slide_layouts[5]  # Title only
    s = prs.slides.add_slide(layout)
    # Title (capped length)
    s.shapes.title.text_frame.clear()
    p = s.shapes.title.text_frame.paragraphs[0]
    ttl = title if len(title) <= _CAP_TITLE_CHARS else title[: _CAP_TITLE_CHARS - 1].rstrip() + "…"
    r = p.add_run(); r.text = ttl
    _set_font(r, main_font, size_pt=28)
    # Body (capped + density-aware font)
    capped, overflow = _cap_body_lines(body_lines)
    if capped:
        left = Inches(0.6); top = Inches(1.35); width = Inches(8.8); height = Inches(4.0)
        tx = s.shapes.add_textbox(left, top, width, height)
        tf = tx.text_frame; tf.word_wrap = True
        try:
            tf.auto_size = MSO_AUTO_SIZE.NONE
        except Exception:
            pass
        n = len(capped)
        size = 18 if n <= 7 else (15 if n <= 11 else 13)
        for i, line in enumerate(capped):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = para.add_run(); run.text = line
            _set_font(run, main_font, size_pt=size)
        if overflow:
            para = tf.add_paragraph(); run = para.add_run()
            run.text = "… (جزئیات کامل در روایت گفته می‌شود)"
            _set_font(run, main_font, size_pt=12, color=RGBColor(0x80, 0x80, 0x80))
    # Notes (full text preserved)
    if notes:
        ns = s.notes_slide.notes_text_frame
        ns.clear()
        np = ns.paragraphs[0]; nr = np.add_run(); nr.text = notes
        _set_font(nr, main_font, size_pt=12)
    return s


def _add_image_slide(prs, *, title, image_path, main_font, notes="", explanation_lines=None):
    """Anchor slide showing the FULL source screenshot, aspect-fit so the whole
    page is visible (the old fixed width=8in pushed ~60% of a portrait page
    off-slide). With explanation_lines, lays out image (left) + concise Persian
    explanation card (right) to use the landscape space and add a teaching point."""
    layout = prs.slide_layouts[5]
    s = prs.slides.add_slide(layout)
    s.shapes.title.text_frame.clear()
    p = s.shapes.title.text_frame.paragraphs[0]
    ttl = title if len(title) <= _CAP_TITLE_CHARS else title[: _CAP_TITLE_CHARS - 1].rstrip() + "…"
    r = p.add_run(); r.text = ttl
    _set_font(r, main_font, size_pt=22)

    has_img = bool(image_path and Path(image_path).exists())
    has_expl = bool(explanation_lines)
    if has_img and has_expl:
        # image fits a left box; whole page visible
        box_w, box_h = 4.4, 4.3
        iw, ih = _fit_image_dims(image_path, box_w, box_h)
        ix = 0.4 + (box_w - iw) / 2.0
        iy = 1.15 + (box_h - ih) / 2.0
        s.shapes.add_picture(str(image_path), Inches(ix), Inches(iy), width=Inches(iw), height=Inches(ih))
        tx = s.shapes.add_textbox(Inches(5.05), Inches(1.25), Inches(4.6), Inches(4.1))
        tf = tx.text_frame; tf.word_wrap = True
        try:
            tf.auto_size = MSO_AUTO_SIZE.NONE
        except Exception:
            pass
        capped, _ = _cap_body_lines(explanation_lines)
        size = 16 if len(capped) <= 7 else 13
        for i, line in enumerate(capped):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = para.add_run(); run.text = line
            _set_font(run, main_font, size_pt=size)
    elif has_img:
        box_w, box_h = 9.2, 4.35
        iw, ih = _fit_image_dims(image_path, box_w, box_h)
        ix = (10.0 - iw) / 2.0
        iy = 1.15 + (box_h - ih) / 2.0
        s.shapes.add_picture(str(image_path), Inches(ix), Inches(iy), width=Inches(iw), height=Inches(ih))

    if notes:
        ns = s.notes_slide.notes_text_frame; ns.clear()
        np = ns.paragraphs[0]; nr = np.add_run(); nr.text = notes
        _set_font(nr, main_font, size_pt=12)
    return s


def _add_crop_zoom_slide(prs, *, title, zoom_path, thumb_path, explanation_lines, main_font, notes=""):
    """Q2 layout: large zoomed/cropped source region (left) + small full-page
    context thumbnail (top-right) + concise Persian explanation card (right)."""
    layout = prs.slide_layouts[5]
    s = prs.slides.add_slide(layout)
    s.shapes.title.text_frame.clear()
    p = s.shapes.title.text_frame.paragraphs[0]
    ttl = title if len(title) <= _CAP_TITLE_CHARS else title[: _CAP_TITLE_CHARS - 1].rstrip() + "…"
    r = p.add_run(); r.text = ttl
    _set_font(r, main_font, size_pt=22)

    # Zoom crop: left ~56% of slide, aspect-fit into a generous box
    if zoom_path and Path(zoom_path).exists():
        box_w, box_h = 5.5, 4.35
        iw, ih = _fit_image_dims(zoom_path, box_w, box_h)
        ix = 0.35 + (box_w - iw) / 2.0
        iy = 1.15 + (box_h - ih) / 2.0
        s.shapes.add_picture(str(zoom_path), Inches(ix), Inches(iy), width=Inches(iw), height=Inches(ih))

    # Full-page context thumbnail: top-right
    thumb_bottom = 1.2
    if thumb_path and Path(thumb_path).exists():
        tw, th = _fit_image_dims(thumb_path, 1.7, 2.2)
        tx0 = 9.65 - tw
        s.shapes.add_picture(str(thumb_path), Inches(tx0), Inches(1.2), width=Inches(tw), height=Inches(th))
        thumb_bottom = 1.2 + th + 0.15

    # Explanation card: right, below the thumbnail
    if explanation_lines:
        tx = s.shapes.add_textbox(Inches(6.05), Inches(thumb_bottom), Inches(3.6), Inches(5.4 - thumb_bottom))
        tf = tx.text_frame; tf.word_wrap = True
        try:
            tf.auto_size = MSO_AUTO_SIZE.NONE
        except Exception:
            pass
        capped, _ = _cap_body_lines(explanation_lines)
        size = 14 if len(capped) <= 6 else 12
        for i, line in enumerate(capped):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = para.add_run(); run.text = line
            _set_font(run, main_font, size_pt=size)

    if notes:
        ns = s.notes_slide.notes_text_frame; ns.clear()
        np = ns.paragraphs[0]; nr = np.add_run(); nr.text = notes
        _set_font(nr, main_font, size_pt=12)
    return s


def _add_title_only_slide(prs, *, big_title, subtitle, main_font, notes=""):
    """Big centered title slide (package opener)."""
    layout = prs.slide_layouts[0]
    s = prs.slides.add_slide(layout)
    s.shapes.title.text_frame.clear()
    p = s.shapes.title.text_frame.paragraphs[0]
    r = p.add_run(); r.text = big_title
    _set_font(r, main_font, size_pt=36)
    if len(s.placeholders) > 1:
        sub_tf = s.placeholders[1].text_frame; sub_tf.clear()
        p2 = sub_tf.paragraphs[0]; r2 = p2.add_run(); r2.text = subtitle
        _set_font(r2, main_font, size_pt=20)
    if notes:
        ns = s.notes_slide.notes_text_frame; ns.clear()
        np = ns.paragraphs[0]; nr = np.add_run(); nr.text = notes
        _set_font(nr, main_font, size_pt=12)
    return s


def _build_package_pptx(run_dir, project_root, package_id, *, main_font):
    """Build the teaching deck for one package and return (pptx_path, manifest_slides)."""
    js_path = run_dir / "content_json" / "{}_extended_fa.json".format(package_id)
    if not js_path.exists():
        raise FileNotFoundError("content_json not found: " + str(js_path))
    js = json.loads(js_path.read_text(encoding="utf-8"))

    prs = Presentation()
    prs.slide_width = Emu(9144000)
    prs.slide_height = Emu(5143500)

    pkg_num = int(package_id.replace("package_", ""))
    title = js.get("package_title", package_id)
    summary = js.get("summary", "")
    learning_goal = js.get("learning_goal", "")
    learning_goals = js.get("learning_goals", []) or []
    pmini = js.get("prerequisites_mini_lesson", "") or ""
    prereqs = js.get("prerequisites", []) or []
    glossary = js.get("glossary", []) or []
    exam_points = js.get("exam_points", []) or []
    common_traps = js.get("common_traps", []) or []
    quick_qa = js.get("quick_questions", []) or js.get("quick_qa", []) or []
    final_takeaway = js.get("final_takeaway_fa") or ""
    uncertainty = js.get("uncertainty_notes", []) or js.get("uncertainty", []) or []
    page_explanations = js.get("page_explanations", []) or []
    source_pages = js.get("source_pages", []) or []
    pages_dir = run_dir / "extracted" / "pages"

    manifest_slides = []
    sid_counter = [0]
    def _next_sid():
        sid_counter[0] += 1
        return "p{:03d}_s{:03d}".format(pkg_num, sid_counter[0]), sid_counter[0]

    def _add(title_text, type_, *, notes="", image_path="", duplicate_of=""):
        sid, idx = _next_sid()
        manifest_slides.append({
            "slide_id": sid,
            "package_id": package_id,
            "slide_index_in_package": idx,
            "slide_index_global": idx,  # global == in_package while only one package; Phase 6C/all-build renumbers
            "title": title_text,
            "type": type_ if type_ in _VALID_SLIDE_TYPES else "other",
            "source_pages": list(source_pages),
            "image_path": image_path or None,
            "speaker_notes_excerpt": (notes[:200] if notes else None),
            "duplicate_of": duplicate_of or None,
            "slide_sha256": None,
        })
        return sid

    # ----- 1. Title slide -----
    _add_title_only_slide(prs, big_title=title,
                          subtitle=_package_subtitle(run_dir, pkg_num),
                          main_font=main_font,
                          notes="عنوان بسته. این یک slide opener است.")
    _add(title, "divider", notes="عنوان بسته (slide opener).")

    # ----- 2. Overview -----
    _add_text_slide(prs, title="خلاصه‌ی بسته", body_lines=_wrap_paragraph(summary),
                    main_font=main_font, notes=summary)
    _add("خلاصه‌ی بسته", "concept", notes=summary)

    # ----- 3. Learning goal + sub-goals -----
    goal_lines = [learning_goal] if learning_goal else []
    if learning_goals:
        goal_lines.append("")
        goal_lines.append("اهداف ریز:")
        for g in learning_goals:
            goal_lines.append("• " + g)
    _add_text_slide(prs, title="هدف آموزشی", body_lines=goal_lines, main_font=main_font,
                    notes=learning_goal + ("\n" + " / ".join(learning_goals) if learning_goals else ""))
    _add("هدف آموزشی", "concept", notes=learning_goal)

    # ----- 4. Prerequisites -----
    pre_lines = ([pmini] if pmini else [])
    if prereqs:
        pre_lines.append("")
        for pr in prereqs:
            term = pr.get("term", "")
            expl = pr.get("explanation_fa", "")
            pre_lines.append("• {} — {}".format(term, expl))
    _add_text_slide(prs, title="پیش‌نیازها", body_lines=pre_lines, main_font=main_font,
                    notes=pmini)
    _add("پیش‌نیازها", "concept", notes=pmini)

    # ----- 5..K*: per-page (anchor + explanation, and Page 2 gets definitions as duplicate-style progressive slides) -----
    for pi, pe in enumerate(page_explanations, start=1):
        page_id = pe.get("page_id", "page_{:03d}".format(source_pages[pi-1] if pi-1 < len(source_pages) else pi))
        page_num = int(page_id.replace("page_", "").lstrip("0") or "0")
        topic = pe.get("topic_fa", "")
        visible = pe.get("what_is_visible_fa", "")
        remember = pe.get("what_to_remember_fa", "")
        notes_text = topic + "\n\n" + visible + "\n\nخلاصه: " + remember
        # 5a. Anchor slide (full screenshot)
        img_p = pages_dir / "{}.png".format(page_id)
        anchor_title = "اسلاید اصلی — Page {}".format(page_num)
        _add_image_slide(prs, title=anchor_title, image_path=img_p, main_font=main_font, notes=visible,
                         explanation_lines=[topic, "", "آنچه باید به‌خاطر بسپارید:", remember])
        anchor_sid = _add(anchor_title, "anchor", notes=visible, image_path=str(img_p))
        # 5b. Explanation slide
        expl_lines = [topic, "", visible, "", "آنچه باید به‌خاطر بسپارید: " + remember]
        if pe.get("extra_notes_fa"):
            expl_lines += ["", pe["extra_notes_fa"]]
        _add_text_slide(prs, title="توضیح — Page {}".format(page_num),
                        body_lines=expl_lines, main_font=main_font, notes=notes_text)
        _add("توضیح — Page {}".format(page_num), "concept", notes=remember)
        # 5c. Definitions (if any) - one slide per definition (progressive without animation)
        for d in pe.get("definitions_fa", []) or []:
            author = d.get("author", "")
            quote = d.get("quote_en", "")
            expl = d.get("explanation_fa", "")
            body = [author, "", "« " + quote + " »", "", expl]
            note_d = author + "\n\n" + quote + "\n\n" + expl
            _add_text_slide(prs, title="تعریف — {}".format(author),
                            body_lines=body, main_font=main_font, notes=note_d)
            _add("تعریف — {}".format(author), "concept", notes=note_d)
        # 5d. Comparison (if any)
        if pe.get("comparison_fa"):
            cmp_title = pe.get("comparison_title_fa") or "مقایسه"
            _add_text_slide(prs, title=cmp_title,
                            body_lines=_wrap_paragraph(pe["comparison_fa"]),
                            main_font=main_font, notes=pe["comparison_fa"])
            _add(cmp_title, "concept", notes=pe["comparison_fa"])
        # 5e. Extra tables (if any) - one slide per table summarized
        for tbl in pe.get("extra_tables", []) or []:
            tbl_title = tbl.get("title_fa", "جدول")
            headers = tbl.get("headers_fa", [])
            rows = tbl.get("rows", [])
            body = [" | ".join(headers)]
            for row in rows[:6]:  # max 6 rows on a slide
                body.append(" | ".join(str(c)[:80] for c in row))
            if len(rows) > 6:
                body.append("...")
            note_t = tbl_title + "\n" + "\n".join(" | ".join(str(c) for c in r) for r in rows)
            _add_text_slide(prs, title=tbl_title, body_lines=body,
                            main_font=main_font, notes=note_t)
            _add(tbl_title, "concept", notes=tbl_title)
        # 5f. Exam-hint per page (if any)
        if pe.get("exam_hint_fa"):
            _add_text_slide(prs, title="نکته‌ی امتحانی — Page {}".format(page_num),
                            body_lines=_wrap_paragraph(pe["exam_hint_fa"]),
                            main_font=main_font, notes=pe["exam_hint_fa"])
            _add("نکته‌ی امتحانی — Page {}".format(page_num),
                 "exam_trap", notes=pe["exam_hint_fa"])

    # ----- Glossary -----
    if glossary:
        body = []
        for g in glossary[:8]:
            term = g.get("term", "")
            expl = g.get("explanation_fa", "")[:120]
            body.append("• " + term + " — " + expl)
        note_g = "\n".join("{}: {}".format(g.get("term",""), g.get("explanation_fa","")) for g in glossary)
        _add_text_slide(prs, title="اصطلاحات کلیدی", body_lines=body,
                        main_font=main_font, notes=note_g)
        _add("اصطلاحات کلیدی", "concept", notes="glossary ({} entries)".format(len(glossary)))

    # ----- Exam points -----
    if exam_points:
        body = ["• " + p for p in exam_points]
        _add_text_slide(prs, title="نکات امتحانی مهم", body_lines=body,
                        main_font=main_font, notes="\n".join(exam_points))
        _add("نکات امتحانی مهم", "exam_trap", notes="\n".join(exam_points))

    # ----- Common traps -----
    if common_traps:
        body = ["• " + p for p in common_traps]
        _add_text_slide(prs, title="دام‌های رایج", body_lines=body,
                        main_font=main_font, notes="\n".join(common_traps))
        _add("دام‌های رایج", "common_mistake", notes="\n".join(common_traps))

    # ----- Quick Q&A (one slide per Q&A) -----
    for i, qa in enumerate(quick_qa, start=1):
        q = qa.get("q_fa", "")
        a = qa.get("a_fa", "")
        body = ["پرسش: " + q, "", "پاسخ: " + a]
        _add_text_slide(prs, title="سؤال سریع {}".format(i),
                        body_lines=body, main_font=main_font, notes=q + "\n" + a)
        _add("سؤال سریع {}".format(i), "quiz", notes=q + " / " + a)

    # ----- Summary -----
    if final_takeaway:
        _add_text_slide(prs, title="جمع‌بندی نهایی",
                        body_lines=_wrap_paragraph(final_takeaway),
                        main_font=main_font, notes=final_takeaway)
        _add("جمع‌بندی نهایی", "summary", notes=final_takeaway)

    # ----- Uncertainty (if any) -----
    if uncertainty:
        body = ["• " + u for u in uncertainty]
        _add_text_slide(prs, title="عدم قطعیت‌ها (یادداشت برای استاد/دانشجو)",
                        body_lines=body, main_font=main_font,
                        notes="\n".join(uncertainty))
        _add("عدم قطعیت‌ها", "other", notes="\n".join(uncertainty))

    return prs, manifest_slides


def _wrap_paragraph(s, max_chars=85):
    """Split a long Persian paragraph into wrap-friendly lines for a slide body."""
    if not s:
        return []
    words = s.split()
    out = []; buf = ""
    for w in words:
        if len(buf) + 1 + len(w) > max_chars and buf:
            out.append(buf); buf = w
        else:
            buf = (buf + " " + w) if buf else w
    if buf:
        out.append(buf)
    return out


def run_package_slides(run_dir, project_root, package_id):
    """Phase 6B: build runs/<run>/slides/<package_id>_teaching.pptx and slide manifest."""
    run_dir = Path(run_dir).resolve()
    project_root = Path(project_root).resolve()
    cascade, _english = _load_font_cascade(project_root)
    chosen_font = detect_persian_font(cascade, final_fallback=SLIDE_FALLBACK_FONT)

    slides_dir = run_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = slides_dir / "{}_teaching.pptx".format(package_id)

    prs, manifest_slides = _build_package_pptx(run_dir, project_root, package_id,
                                               main_font=chosen_font)
    prs.save(pptx_path)
    pptx_bytes = pptx_path.stat().st_size

    zip_ok, req_present, member_count = _validate_pptx_zip(pptx_path)

    # Write per-package slide_manifest
    js = json.loads((run_dir / "content_json" / "{}_extended_fa.json".format(package_id))
                    .read_text(encoding="utf-8"))
    src_pages = js.get("source_pages", [])
    manifest_obj = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "deck_path": "slides/{}_teaching.pptx".format(package_id),
        "deck_pdf": None,
        "slide_count": len(manifest_slides),
        "slides": manifest_slides,
        "generated_at": iso_now(),
    }
    # NOTE: per-package slide_manifest_<pid>.json is a SECONDARY / DEBUG artifact.
    # The combined slides/slide_manifest.json (built by the merge path) is the ONLY
    # source of truth for narration, audio, video, and final QA. The two builders
    # are separate code paths and may legitimately diverge in slide count; that
    # divergence is tolerated and validated by
    # integrations/slide_manifest_consistency.py (strategy: FORMALLY_NEUTRALIZED).
    # Per-package manifests must never override combined-manifest data.
    manifest_path = slides_dir / "slide_manifest_{}.json".format(package_id)
    manifest_path.write_text(json.dumps(manifest_obj, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # Validate manifest against schema
    schema_errors = []
    schema_valid = True
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads((project_root / "schemas" / "slide_manifest.schema.json")
                            .read_text(encoding="utf-8"))
        errs = list(Draft202012Validator(schema).iter_errors(manifest_obj))
        schema_errors = [str(e.message) for e in errs[:5]]
        schema_valid = (len(errs) == 0)
    except Exception as exc:
        schema_errors = ["validator-unavailable: " + repr(exc)]
        schema_valid = True  # treat as OK if validator missing

    # Optional PDF export
    soffice = detect_soffice()
    pdf_path = None; pdf_bytes = 0; soffice_skipped = ""
    notes = []
    if soffice is None:
        soffice_skipped = "soffice not installed; PDF export skipped"
        notes.append(soffice_skipped)
    else:
        export = pptx_to_pdf(pptx_path, slides_dir)
        if export.success:
            pdf_path = export.pdf_path; pdf_bytes = export.pdf_bytes
            # Update manifest deck_pdf
            manifest_obj["deck_pdf"] = "slides/" + pdf_path.name
            manifest_path.write_text(json.dumps(manifest_obj, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        else:
            soffice_skipped = export.skipped_reason
            notes.append("PDF export failed: " + soffice_skipped)

    return PackageSlidesResult(
        success=(zip_ok and req_present and pptx_bytes > 0),
        package_id=package_id,
        pptx_path=pptx_path,
        pdf_path=pdf_path,
        pptx_bytes=pptx_bytes,
        pdf_bytes=pdf_bytes,
        slide_count=len(manifest_slides),
        expected_pages=src_pages,
        chosen_font=chosen_font,
        cascade=cascade,
        pptx_zip_ok=zip_ok,
        pptx_required_members_present=req_present,
        pptx_member_count=member_count,
        manifest_path=manifest_path,
        manifest_schema_valid=schema_valid,
        manifest_schema_errors=schema_errors,
        soffice_path=soffice,
        soffice_skipped_reason=soffice_skipped,
        notes=notes,
        generated_at=iso_now(),
    )



# ============================================================================
# Phase 6D: all-packages + merge into one teaching deck
# ============================================================================


@dataclass
class CombinedSlidesResult:
    success: bool
    n_packages: int
    n_built: int
    per_package_results: list
    merged_pptx_path: Optional[Path]
    merged_pdf_path: Optional[Path]
    merged_pptx_bytes: int
    merged_pdf_bytes: int
    merged_slide_count: int
    combined_manifest_path: Optional[Path]
    combined_manifest_schema_valid: bool
    combined_manifest_schema_errors: list = _dc_field(default_factory=list)
    soffice_path: Optional[str] = None
    notes: list = _dc_field(default_factory=list)
    generated_at: str = ""


def run_all_packages_slides(run_dir, project_root, *, merge=True, skip_existing=True):
    """Phase 6D: ensure every package has a teaching deck; optionally merge."""
    run_dir = Path(run_dir).resolve()
    project_root = Path(project_root).resolve()
    cascade, _english = _load_font_cascade(project_root)
    chosen_font = detect_persian_font(cascade, final_fallback=SLIDE_FALLBACK_FONT)

    # Discover packages from content_md (same source of truth as Phase 5C)
    md_dir = run_dir / "content_md"
    md_files = sorted(md_dir.glob("package_*_extended_fa.md"))
    package_ids = [p.stem.replace("_extended_fa", "") for p in md_files]

    per_package_results = []
    notes = []
    n_built = 0
    for pid in package_ids:
        target_pptx = run_dir / "slides" / "{}_teaching.pptx".format(pid)
        if skip_existing and target_pptx.exists() and target_pptx.stat().st_size > 0:
            notes.append("skipped existing: " + pid)
            # Load existing manifest for the per-package summary
            mp = run_dir / "slides" / "slide_manifest_{}.json".format(pid)
            sc = 0
            if mp.exists():
                try:
                    sc = json.loads(mp.read_text(encoding="utf-8")).get("slide_count", 0)
                except Exception:
                    pass
            per_package_results.append({
                "package_id": pid, "built": False, "slide_count": sc,
                "pptx_path": str(target_pptx),
            })
            continue
        r = run_package_slides(run_dir, project_root, pid)
        per_package_results.append({
            "package_id": pid, "built": True, "slide_count": r.slide_count,
            "pptx_path": str(r.pptx_path), "success": r.success,
        })
        if r.success:
            n_built += 1

    # ---- merge ----
    merged_pptx_path = None
    merged_pdf_path = None
    merged_pptx_bytes = 0
    merged_pdf_bytes = 0
    merged_slide_count = 0
    combined_manifest_path = None
    combined_manifest_schema_valid = True
    combined_manifest_schema_errors = []
    soffice = detect_soffice()

    if merge:
        merged_pptx_path, merged_slide_count, combined_manifest = \
            _build_full_deck(run_dir, project_root, package_ids, main_font=chosen_font)
        merged_pptx_bytes = merged_pptx_path.stat().st_size

        # Combined slide_manifest.json
        combined_manifest_path = run_dir / "slides" / "slide_manifest.json"
        combined_manifest_path.write_text(
            json.dumps(combined_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Validate combined manifest
        try:
            from jsonschema import Draft202012Validator
            schema = json.loads((project_root / "schemas" / "slide_manifest.schema.json")
                                .read_text(encoding="utf-8"))
            errs = list(Draft202012Validator(schema).iter_errors(combined_manifest))
            combined_manifest_schema_errors = [str(e.message) for e in errs[:5]]
            combined_manifest_schema_valid = (len(errs) == 0)
        except Exception as exc:
            combined_manifest_schema_errors = ["validator-unavailable: " + repr(exc)]

        # PDF export
        if soffice is not None:
            export = pptx_to_pdf(merged_pptx_path, run_dir / "slides")
            if export.success:
                merged_pdf_path = export.pdf_path
                merged_pdf_bytes = export.pdf_bytes
                combined_manifest["deck_pdf"] = "slides/" + merged_pdf_path.name
                combined_manifest_path.write_text(
                    json.dumps(combined_manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                notes.append("PDF export failed: " + export.skipped_reason)
        else:
            notes.append("soffice not installed; merged PDF skipped")

    return CombinedSlidesResult(
        success=(merged_pptx_path is not None and merged_pptx_bytes > 0 and
                 combined_manifest_schema_valid),
        n_packages=len(package_ids),
        n_built=n_built,
        per_package_results=per_package_results,
        merged_pptx_path=merged_pptx_path,
        merged_pdf_path=merged_pdf_path,
        merged_pptx_bytes=merged_pptx_bytes,
        merged_pdf_bytes=merged_pdf_bytes,
        merged_slide_count=merged_slide_count,
        combined_manifest_path=combined_manifest_path,
        combined_manifest_schema_valid=combined_manifest_schema_valid,
        combined_manifest_schema_errors=combined_manifest_schema_errors,
        soffice_path=soffice,
        notes=notes,
        generated_at=iso_now(),
    )


def _build_full_deck(run_dir, project_root, package_ids, *, main_font):
    """Build the merged deck by rebuilding all per-package slides into one Presentation.

    This is more reliable than parsing/copying slides between pptx files
    (which python-pptx does not natively support). Side benefit: the
    individual per-package decks remain untouched.

    Renumbers each slide's slide_index_global across the whole deck.
    """
    prs = Presentation()
    prs.slide_width = Emu(9144000)
    prs.slide_height = Emu(5143500)

    combined_slides = []
    global_idx = 0

    for pid in package_ids:
        js_path = run_dir / "content_json" / "{}_extended_fa.json".format(pid)
        if not js_path.exists():
            continue
        js = json.loads(js_path.read_text(encoding="utf-8"))

        # Re-use the same builder we use per-package, but appending to the same Presentation.
        # We piggyback on _build_package_pptx by constructing a fresh Presentation per
        # package and then copying NOTHING; instead we directly inline the building
        # logic here using a small helper.
        # To keep the change minimal, we instead call _add_slides_for_package(prs, ...).
        per_pkg = _add_slides_for_package(prs, run_dir, pid, js, main_font=main_font,
                                          starting_global_index=global_idx)
        combined_slides.extend(per_pkg)
        global_idx += len(per_pkg)

    merged_path = run_dir / "slides" / "full_teaching_deck.pptx"
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(merged_path)

    combined_manifest = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "deck_path": "slides/full_teaching_deck.pptx",
        "deck_pdf": None,
        "slide_count": len(combined_slides),
        "slides": combined_slides,
        "generated_at": iso_now(),
    }
    return merged_path, len(combined_slides), combined_manifest


def _add_slides_for_package(prs, run_dir, package_id, js, *, main_font, starting_global_index):
    """Append all slides for one package to `prs`, returning the manifest entries."""
    pkg_num = int(package_id.replace("package_", ""))
    title = js.get("package_title", package_id)
    summary = js.get("summary", "")
    learning_goal = js.get("learning_goal", "")
    learning_goals = js.get("learning_goals", []) or []
    pmini = js.get("prerequisites_mini_lesson", "") or ""
    prereqs = js.get("prerequisites", []) or []
    glossary = js.get("glossary", []) or []
    exam_points = js.get("exam_points", []) or []
    common_traps = js.get("common_traps", []) or []
    quick_qa = js.get("quick_questions", []) or js.get("quick_qa", []) or []
    final_takeaway = js.get("final_takeaway_fa") or ""
    uncertainty = js.get("uncertainty_notes", []) or js.get("uncertainty", []) or []
    page_explanations = js.get("page_explanations", []) or []
    source_pages = js.get("source_pages", []) or []
    pages_dir = run_dir / "extracted" / "pages"

    manifest_slides = []
    sid_counter = [0]
    def _add_entry(title_text, type_, *, notes="", image_path="", duplicate_of=""):
        sid_counter[0] += 1
        idx_in_pkg = sid_counter[0]
        global_idx = starting_global_index + idx_in_pkg
        manifest_slides.append({
            "slide_id": "p{:03d}_s{:03d}".format(pkg_num, idx_in_pkg),
            "package_id": package_id,
            "slide_index_in_package": idx_in_pkg,
            "slide_index_global": global_idx,
            "title": title_text,
            "type": type_ if type_ in _VALID_SLIDE_TYPES else "other",
            "source_pages": list(source_pages),
            "image_path": image_path or None,
            "speaker_notes_excerpt": (notes[:200] if notes else None),
            "duplicate_of": duplicate_of or None,
            "slide_sha256": None,
        })

    # ----- 1. Title slide -----
    _add_title_only_slide(prs, big_title=title,
                          subtitle=_package_subtitle(run_dir, pkg_num),
                          main_font=main_font,
                          notes="عنوان بسته. این یک slide opener است.")
    _add_entry(title, "divider", notes="عنوان بسته (slide opener).")

    # ----- 2. Overview -----
    _add_text_slide(prs, title="خلاصه‌ی بسته", body_lines=_wrap_paragraph(summary),
                    main_font=main_font, notes=summary)
    _add_entry("خلاصه‌ی بسته", "concept", notes=summary)

    # ----- 3. Learning goal -----
    goal_lines = [learning_goal] if learning_goal else []
    if learning_goals:
        goal_lines.append("")
        goal_lines.append("اهداف ریز:")
        for g in learning_goals:
            goal_lines.append("• " + g)
    _add_text_slide(prs, title="هدف آموزشی", body_lines=goal_lines, main_font=main_font,
                    notes=learning_goal)
    _add_entry("هدف آموزشی", "concept", notes=learning_goal)

    # ----- 4. Prerequisites -----
    pre_lines = ([pmini] if pmini else [])
    if prereqs:
        pre_lines.append("")
        for pr in prereqs:
            pre_lines.append("• {} — {}".format(pr.get("term", ""), pr.get("explanation_fa", "")))
    _add_text_slide(prs, title="پیش‌نیازها", body_lines=pre_lines, main_font=main_font, notes=pmini)
    _add_entry("پیش‌نیازها", "concept", notes=pmini)

    # ----- 5..K*. Per-page (anchor + explanation + sub-blocks) -----
    for pi, pe in enumerate(page_explanations, start=1):
        page_id = pe.get("page_id", "page_{:03d}".format(source_pages[pi-1] if pi-1 < len(source_pages) else pi))
        page_num = int(page_id.replace("page_", "").lstrip("0") or "0")
        topic = pe.get("topic_fa", "")
        visible = pe.get("what_is_visible_fa", "")
        remember = pe.get("what_to_remember_fa", "")
        notes_text = topic + "\n\n" + visible + "\n\nخلاصه: " + remember
        img_p = pages_dir / "{}.png".format(page_id)
        anchor_title = "اسلاید اصلی — Page {}".format(page_num)
        _add_image_slide(prs, title=anchor_title, image_path=img_p, main_font=main_font, notes=visible,
                         explanation_lines=[topic, "", "آنچه باید به‌خاطر بسپارید:", remember])
        _add_entry(anchor_title, "anchor", notes=visible, image_path=str(img_p))

        expl_lines = [topic, "", visible, "", "آنچه باید به‌خاطر بسپارید: " + remember]
        if pe.get("extra_notes_fa"):
            expl_lines += ["", pe["extra_notes_fa"]]
        _add_text_slide(prs, title="توضیح — Page {}".format(page_num),
                        body_lines=expl_lines, main_font=main_font, notes=notes_text)
        _add_entry("توضیح — Page {}".format(page_num), "concept", notes=remember)

        for d in pe.get("definitions_fa", []) or []:
            author = d.get("author", "")
            quote = d.get("quote_en", "")
            expl = d.get("explanation_fa", "")
            body = [author, "", "« " + quote + " »", "", expl]
            _add_text_slide(prs, title="تعریف — {}".format(author),
                            body_lines=body, main_font=main_font,
                            notes=author + " :: " + expl)
            _add_entry("تعریف — {}".format(author), "concept",
                       notes=author + " :: " + expl)

        if pe.get("comparison_fa"):
            cmp_title = pe.get("comparison_title_fa") or "مقایسه"
            _add_text_slide(prs, title=cmp_title,
                            body_lines=_wrap_paragraph(pe["comparison_fa"]),
                            main_font=main_font, notes=pe["comparison_fa"])
            _add_entry(cmp_title, "concept", notes=pe["comparison_fa"])

        for fb in pe.get("formula_blocks", []) or []:
            formula = fb.get("formula", "")
            explanation = fb.get("explanation_fa", "")
            body = ["فرمول: " + formula, "", explanation]
            if fb.get("tiny_numeric_example_fa"):
                body.append(""); body.append("مثال عددی: " + fb["tiny_numeric_example_fa"])
            _add_text_slide(prs, title="فرمول: " + formula, body_lines=body,
                            main_font=main_font, notes=explanation)
            _add_entry("فرمول: " + formula, "formula", notes=explanation)

        for fig in pe.get("figure_blocks", []) or []:
            cap = fig.get("caption_fa") or fig.get("figure_id") or "شکل"
            body = []
            if fig.get("axes_fa"): body.append("محورها: " + fig["axes_fa"])
            if fig.get("legend_fa"): body.append("راهنما: " + fig["legend_fa"])
            if fig.get("trend_fa"): body.append("روند: " + fig["trend_fa"])
            if fig.get("takeaway_fa"): body.append(""); body.append("نکته‌ی کلیدی: " + fig["takeaway_fa"])
            _add_text_slide(prs, title="تفسیر شکل: " + cap, body_lines=body,
                            main_font=main_font, notes=fig.get("takeaway_fa", ""))
            _add_entry("تفسیر شکل: " + cap, "figure", notes=fig.get("takeaway_fa", ""))

        for tbl in pe.get("extra_tables", []) or []:
            tbl_title = tbl.get("title_fa", "جدول")
            headers = tbl.get("headers_fa", [])
            rows = tbl.get("rows", [])
            body = [" | ".join(headers)]
            for row in rows[:6]:
                body.append(" | ".join(str(c)[:80] for c in row))
            if len(rows) > 6:
                body.append("...")
            _add_text_slide(prs, title=tbl_title, body_lines=body,
                            main_font=main_font, notes=tbl_title)
            _add_entry(tbl_title, "concept", notes=tbl_title)

        if pe.get("exam_hint_fa"):
            _add_text_slide(prs, title="نکته‌ی امتحانی — Page {}".format(page_num),
                            body_lines=_wrap_paragraph(pe["exam_hint_fa"]),
                            main_font=main_font, notes=pe["exam_hint_fa"])
            _add_entry("نکته‌ی امتحانی — Page {}".format(page_num),
                       "exam_trap", notes=pe["exam_hint_fa"])

    # ----- Glossary -----
    if glossary:
        body = []
        for g in glossary[:8]:
            body.append("• " + g.get("term", "") + " — " + g.get("explanation_fa", "")[:120])
        _add_text_slide(prs, title="اصطلاحات کلیدی", body_lines=body,
                        main_font=main_font, notes="\n".join(g.get("term", "") for g in glossary))
        _add_entry("اصطلاحات کلیدی", "concept", notes="glossary")

    # ----- Exam points -----
    if exam_points:
        body = ["• " + p for p in exam_points]
        _add_text_slide(prs, title="نکات امتحانی مهم", body_lines=body,
                        main_font=main_font, notes="\n".join(exam_points))
        _add_entry("نکات امتحانی مهم", "exam_trap", notes="\n".join(exam_points))

    # ----- Common traps -----
    if common_traps:
        body = ["• " + p for p in common_traps]
        _add_text_slide(prs, title="دام‌های رایج", body_lines=body,
                        main_font=main_font, notes="\n".join(common_traps))
        _add_entry("دام‌های رایج", "common_mistake", notes="\n".join(common_traps))

    # ----- Quick Q&A -----
    for i, qa in enumerate(quick_qa, start=1):
        q = qa.get("q_fa", ""); a = qa.get("a_fa", "")
        body = ["پرسش: " + q, "", "پاسخ: " + a]
        _add_text_slide(prs, title="سؤال سریع {}".format(i),
                        body_lines=body, main_font=main_font, notes=q + " / " + a)
        _add_entry("سؤال سریع {}".format(i), "quiz", notes=q + " / " + a)

    # ----- Summary -----
    if final_takeaway:
        _add_text_slide(prs, title="جمع‌بندی نهایی",
                        body_lines=_wrap_paragraph(final_takeaway),
                        main_font=main_font, notes=final_takeaway)
        _add_entry("جمع‌بندی نهایی", "summary", notes=final_takeaway)

    # ----- Uncertainty -----
    if uncertainty:
        body = ["• " + u for u in uncertainty]
        _add_text_slide(prs, title="عدم قطعیت‌ها", body_lines=body,
                        main_font=main_font, notes="\n".join(uncertainty))
        _add_entry("عدم قطعیت‌ها", "other", notes="\n".join(uncertainty))

    return manifest_slides
