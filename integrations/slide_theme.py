"""slide_theme - Q3 professional teaching-slide style system (reusable).

Restrained palette + card/title/footer helpers for a clean 16:9 RTL educational
template. Persian paragraphs are right-aligned and prefixed with RLM (U+200F) so
trailing punctuation and mixed Latin/math tokens sit in the correct bidi order.
No content is hardcoded here — callers pass run-specific text.
"""
from __future__ import annotations

from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn


def set_run_fonts(run, family):
    """Set the Latin, complex-script (cs) AND east-asian (ea) typefaces on a run.

    python-pptx's run.font.name only sets the Latin face (a:latin). Persian is
    *complex script*; without an explicit a:cs face, LibreOffice falls back to a
    non-Persian font for the CTL run and renders tofu. Setting a:cs to the
    Persian font fixes mixed Persian+Latin runs.
    """
    run.font.name = family
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:cs", "a:ea"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", family)

# ---- palette ----
NAVY = RGBColor(0x1F, 0x2D, 0x5A)        # titles
TEAL = RGBColor(0x0E, 0x7C, 0x66)        # key ideas
ORANGE = RGBColor(0xC0, 0x3A, 0x2B)      # warnings / traps / highlights
GRAY = RGBColor(0x5F, 0x66, 0x70)        # context / footer
BG = RGBColor(0xF7, 0xF8, 0xFA)          # off-white background
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
TEAL_FILL = RGBColor(0xE7, 0xF3, 0xF0)
GRAY_FILL = RGBColor(0xEE, 0xF0, 0xF3)
ORANGE_FILL = RGBColor(0xFB, 0xEC, 0xE8)
NAVY_FILL = RGBColor(0xEA, 0xED, 0xF4)

# ---- geometry (10in x 5.625in = 16:9) ----
SLIDE_W, SLIDE_H = 10.0, 5.625
MARGIN = 0.5
TITLE_TOP, TITLE_H = 0.28, 0.92
BODY_TOP = 1.45
FOOTER_TOP = 5.28

RLM = "‏"
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(n):
    return str(n).translate(_FA)


# Display-safe substitutions: replace typographic/math symbols that the installed
# Persian fallback fonts (Tahoma/Arial) lack a glyph for — and the bidi shaping
# triggers that caused tofu in LibreOffice — with universally-rendered ASCII.
# Applied to ON-SLIDE text only; speaker notes keep the original (for narration).
_DISPLAY_SUBST = {
    "⟨": "<", "⟩": ">",     # ⟨ ⟩ math angle brackets -> ASCII
    "〈": "<", "〉": ">",
    "−": "-",                      # − minus sign -> hyphen-minus
    "²": "^2", "³": "^3",     # ² ³ superscripts
    "ₐ": "a", "ₓ": "x",       # ₐ ₓ subscripts -> base letter
    "₀": "0", "₁": "1", "₂": "2", "₃": "3",
    "؛": "،",                 # ؛ Arabic semicolon -> ، Arabic comma (avoids the CTL shaping bug)
    "⟂": "⊥",                 # ⟂ -> ⊥ (if used)
}
# Persian digits adjacent to inline Latin formulas mis-shape; for safety, the
# AUTHOR should write ASCII digits inside formula strings (handled at authoring).


def display_safe(text):
    if not text:
        return text
    for a, b in _DISPLAY_SUBST.items():
        if a in text:
            text = text.replace(a, b)
    return text


def _to_back(slide, shape):
    sp = shape._element
    sp.getparent().remove(sp)
    slide.shapes._spTree.insert(2, sp)


def set_bg(slide, color=BG):
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(SLIDE_W), Inches(SLIDE_H))
    sh.fill.solid(); sh.fill.fore_color.rgb = color
    sh.line.fill.background()
    try:
        sh.shadow.inherit = False
    except Exception:
        pass
    _to_back(slide, sh)
    return sh


def add_title_bar(slide, text, font, *, color=NAVY, size=26):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(TITLE_TOP), Inches(SLIDE_W), Inches(TITLE_H))
    bar.fill.solid(); bar.fill.fore_color.rgb = color
    bar.line.fill.background()
    try:
        bar.shadow.inherit = False
    except Exception:
        pass
    tf = bar.text_frame; tf.word_wrap = True
    tf.margin_left = Inches(0.4); tf.margin_right = Inches(0.4)
    p = tf.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
    r = p.add_run(); r.text = RLM + display_safe(text)
    set_run_fonts(r, font); r.font.size = Pt(size); r.font.bold = True; r.font.color.rgb = WHITE
    return bar


def add_footer(slide, label, font):
    tx = slide.shapes.add_textbox(Inches(MARGIN), Inches(FOOTER_TOP), Inches(SLIDE_W - 2 * MARGIN), Inches(0.3))
    p = tx.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.LEFT
    r = p.add_run(); r.text = label
    set_run_fonts(r, font); r.font.size = Pt(11); r.font.color.rgb = GRAY
    return tx


def add_card(slide, x, y, w, h, *, fill=WHITE, line=NAVY, line_w=1.25):
    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    card.fill.solid(); card.fill.fore_color.rgb = fill
    if line is None:
        card.line.fill.background()
    else:
        card.line.color.rgb = line; card.line.width = Pt(line_w)
    try:
        card.shadow.inherit = False
    except Exception:
        pass
    card.text_frame.word_wrap = True
    card.text_frame.margin_left = Inches(0.18); card.text_frame.margin_right = Inches(0.18)
    card.text_frame.margin_top = Inches(0.12); card.text_frame.margin_bottom = Inches(0.12)
    return card


def fill_lines(text_frame, lines, font, *, size=16, color=NAVY, align=PP_ALIGN.RIGHT, rtl=True, bold_first=False):
    """Write lines into an existing text_frame (card or textbox). A line may be a
    (text, color, size, bold) tuple for per-line styling."""
    text_frame.word_wrap = True
    first = True
    for line in lines:
        if isinstance(line, tuple):
            txt, c, sz, bold = (list(line) + [color, size, False])[:4]
        else:
            txt, c, sz, bold = line, color, size, (bold_first and first)
        p = text_frame.paragraphs[0] if first else text_frame.add_paragraph()
        p.alignment = align
        txt = display_safe(txt)
        r = p.add_run(); r.text = (RLM + txt if (rtl and txt) else txt)
        set_run_fonts(r, font); r.font.size = Pt(sz); r.font.color.rgb = c
        if bold:
            r.font.bold = True
        first = False
    return text_frame


def add_textbox(slide, x, y, w, h):
    tx = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tx.text_frame.word_wrap = True
    return tx
