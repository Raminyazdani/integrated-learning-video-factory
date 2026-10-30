"""latex_slide_renderer — v2 canonical slide render (XeLaTeX Beamer).

Fixes the v1 mixed RTL/LTR defect by rendering with polyglossia (RTL Persian
body) while wrapping every English technical term LTR (\\lrterm) and keeping
all math in math mode. Executor only: consumes Claude-authored deep content /
slide specs; makes no teaching decisions.

Helper functions (LaTeX string builders):
  render_persian_text, render_ltr_term, render_math, render_bilingual_line,
  render_source_slide, render_source_crop, render_formula_card,
  render_symbol_table, render_question_card
plus render_deck(...) (fills the Jinja2 .j2) and compile_xelatex(...).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.latex_builder import detect_persian_font  # reuse proven font detect

TEMPLATE_DIR = ROOT / "templates" / "latex_slides"
J2_NAME = "v2_deep_tutor_beamer.tex.j2"
MACROS_NAME = "v2_slide_macros.tex"

# ---------------------------------------------------------------------------
# text escaping + bilingual rendering (the core RTL/LTR fix)
# ---------------------------------------------------------------------------
_SPECIALS = {"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}",
             "^": r"\textasciicircum{}"}
# A maximal Latin/technical run (starts with a letter, optionally a leading "("
# directly before it, optional trailing ")"); internal spaces between Latin
# tokens are kept so multi-word terms ("Decision Boundary") stay one LTR box.
# Pulling adjacent parens into the LTR box prevents RTL paren-mirroring.
_LATIN_RUN = re.compile(r"\(?[A-Za-z][A-Za-z0-9.\-+()/'’,:]*(?:\s+[A-Za-z0-9.\-+()/'’,:]+)*\)?")


def _escape_tex(text: str) -> str:
    """Escape LaTeX specials in NON-math Persian/text. (Backslashes/braces are
    not expected outside math in our content; we escape the risky punctuation.)"""
    out = []
    for ch in text:
        out.append(_SPECIALS.get(ch, ch))
    return "".join(out)


def render_ltr_term(term: str) -> str:
    """Wrap an English/Latin technical term so it renders LEFT-TO-RIGHT."""
    return r"\lrterm{" + _escape_tex(term) + "}"


def render_math(latex: str, *, display: bool = False) -> str:
    """Wrap a LaTeX math string in math mode (LTR island; bidi-safe)."""
    latex = latex.strip()
    return ("\\[ " + latex + " \\]") if display else ("$" + latex + "$")


def _wrap_latin(text_segment: str) -> str:
    """Escape a non-math text segment and wrap Latin runs in \\lrterm{}."""
    pieces = []
    last = 0
    for m in _LATIN_RUN.finditer(text_segment):
        pieces.append(_escape_tex(text_segment[last:m.start()]))
        run = m.group(0)
        # keep leading/trailing spaces outside the LTR box
        lead = run[:len(run) - len(run.lstrip())]
        trail = run[len(run.rstrip()):]
        core = run.strip()
        pieces.append(lead + r"\lrterm{" + _escape_tex(core) + "}" + trail)
        last = m.end()
    pieces.append(_escape_tex(text_segment[last:]))
    return "".join(pieces)


def render_bilingual_line(s: str) -> str:
    """Render a Persian line that may contain inline $math$ and English terms.
    Math islands ($...$) are preserved; Latin runs are wrapped LTR; Persian is
    left as-is. This is what produces correct mixed RTL/LTR output."""
    if not s:
        return ""
    out = []
    # split on $...$ math (non-greedy), keeping the delimiters
    for i, part in enumerate(re.split(r"(\$[^$]*\$)", s)):
        if i % 2 == 1:           # a $...$ math island
            out.append(part)
        else:
            out.append(_wrap_latin(part))
    return "".join(out)


def render_persian_text(s: str) -> str:
    """General Persian text (may include English/math) -> safe LaTeX."""
    return render_bilingual_line(s)


# ---------------------------------------------------------------------------
# frame builders (each returns a full \begin{frame}...\end{frame})
# ---------------------------------------------------------------------------
def _abs(p) -> str:
    return str(Path(p).resolve()).replace("\\", "/")


def _frame(title_fa: str, body: str) -> str:
    return "\\begin{frame}{" + render_bilingual_line(title_fa) + "}\n" + body + "\n\\end{frame}\n"


def render_source_slide(image_path, title_fa: str, preview_text_fa: str,
                        badge_fa: str = "نگاه اول به اسلاید اصلی") -> str:
    """Full-width original lecture slide + a short Persian preview card."""
    body = ("\\srcbadge{" + render_bilingual_line(badge_fa) + "}\n"
            "\\sourceimgsmall{" + _abs(image_path) + "}\n"
            "\\begin{conceptcard}\n" + render_bilingual_line(preview_text_fa) + "\n\\end{conceptcard}")
    return _frame(title_fa, body)


def render_source_crop(image_path, title_fa: str, explanation_fa: str) -> str:
    """Side-by-side: original slide/crop image + Persian guided-reading card."""
    body = ("\\begin{columns}[T]\n"
            "\\begin{column}{0.52\\textwidth}\n"
            "\\includegraphics[width=\\linewidth,height=0.72\\textheight,keepaspectratio]{" + _abs(image_path) + "}\n"
            "\\end{column}\n"
            "\\begin{column}{0.44\\textwidth}\n"
            "\\begin{conceptcard}\n" + render_bilingual_line(explanation_fa) + "\n\\end{conceptcard}\n"
            "\\end{column}\n\\end{columns}")
    return _frame(title_fa, body)


def render_formula_card(title_fa: str, formula_latex: str, note_fa: str = "") -> str:
    """A formula card (display math) + an optional Persian one-line note.
    A leading ``\\text{Label: }`` is pulled OUT of math and rendered LTR (so the
    label words don't bidi-reorder inside the RTL math display)."""
    m = re.match(r"^\s*\\text\{([^}]*)\}\s*(.*)$", formula_latex, re.S)
    if m and m.group(2).strip():
        head = "\\begin{center}" + render_ltr_term(m.group(1).strip()) + "\\end{center}\n"
        inner = head + render_math(m.group(2).strip(), display=True)
    else:
        inner = render_math(formula_latex, display=True)
    body = "\\begin{formulacard}\n" + inner + "\n\\end{formulacard}"
    if note_fa:
        body += "\n\\vspace{1mm}\n" + render_bilingual_line(note_fa)
    return _frame(title_fa, body)


def render_symbol_table(title_fa: str, rows: List[dict]) -> str:
    """Notation/symbol table (symbol · spoken · meaning · type)."""
    lines = [r"\small", r"\begin{center}", r"\begin{tabular}{cccc}", r"\toprule",
             r"نماد & خواندن & معنی & نوع \\", r"\midrule"]
    for r in rows:
        sym = render_math(r.get("symbol", ""))
        sp = render_bilingual_line(r.get("spoken_form_fa", ""))
        mean = render_bilingual_line(r.get("meaning_fa", ""))
        typ = render_ltr_term(r.get("type", ""))
        lines.append(f"{sym} & {sp} & {mean} & {typ} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{center}"]
    return _frame(title_fa, "\n".join(lines))


def render_question_card(title_fa: str, question_fa: str, answer_fa: str) -> str:
    """Checkpoint / practice Q&A: a question card then an answer card."""
    body = ("\\begin{questioncard}[title=پرسش]\n" + render_bilingual_line(question_fa) + "\n\\end{questioncard}\n"
            "\\vspace{2mm}\n"
            "\\begin{answercard}[title=پاسخ]\n" + render_bilingual_line(answer_fa) + "\n\\end{answercard}")
    return _frame(title_fa, body)


def render_concept(title_fa: str, body_fa: str, badge_fa: str = "") -> str:
    """A teal concept card (enriched teaching / prerequisite / geometric intuition)."""
    inner = (("\\srcbadge{" + render_bilingual_line(badge_fa) + "}\n") if badge_fa else "")
    inner += "\\begin{conceptcard}\n" + render_bilingual_line(body_fa) + "\n\\end{conceptcard}"
    return _frame(title_fa, inner)


def render_example(title_fa: str, example_fa: str, steps: Optional[List[str]] = None) -> str:
    """A worked tiny-example card + optional numbered step list."""
    body = "\\begin{formulacard}[title=مثال عددی]\n" + render_bilingual_line(example_fa) + "\n\\end{formulacard}"
    if steps:
        body += "\n\\vspace{1mm}\n\\begin{enumerate}\n" + "\n".join(
            "\\item " + render_bilingual_line(s) for s in steps) + "\n\\end{enumerate}"
    return _frame(title_fa, body)


def render_bullets(title_fa: str, items: List[str], badge_fa: str = "") -> str:
    """A gray card with a bullet list (common traps / beginner mistakes)."""
    inner = (("\\srcbadge{" + render_bilingual_line(badge_fa) + "}\n") if badge_fa else "")
    inner += "\\begin{graycard}\n\\begin{itemize}\n" + "\n".join(
        "\\item " + render_bilingual_line(it) for it in items) + "\n\\end{itemize}\n\\end{graycard}"
    return _frame(title_fa, inner)


def render_recap(image_path, title_fa: str, recap_fa: str) -> str:
    """A small original-slide thumbnail + a Persian recap card."""
    body = "\\sourceimgsmall[0.7\\linewidth]{" + _abs(image_path) + "}\n"
    body += "\\begin{conceptcard}\n" + render_bilingual_line(recap_fa) + "\n\\end{conceptcard}"
    return _frame(title_fa, body)


def render_divider(title_fa: str, subtitle_fa: str = "") -> str:
    body = ("\\vfill\\begin{center}\n{\\Large\\bfseries " + render_bilingual_line(title_fa) + "}\\par\\vspace{2mm}\n"
            + ("{\\color{v2teal}" + render_bilingual_line(subtitle_fa) + "}\n" if subtitle_fa else "")
            + "\\end{center}\\vfill")
    return "\\begin{frame}\n" + body + "\n\\end{frame}\n"


def render_slide_dict(slide: dict, run_dir: Path) -> str:
    """Dispatch one Claude-authored deep slide_spec entry to a LaTeX frame."""
    t = slide.get("type"); title = slide.get("title_fa") or ""
    body = slide.get("body_fa") or ""

    def _img(key):
        p = slide.get(key)
        return (run_dir / p) if p else None
    if t == "divider":
        return render_divider(title, body)
    if t == "original_preview":
        return render_source_slide(_img("original_slide_image_path"), title, body,
                                   badge_fa=slide.get("badge_fa", "نگاه اول به اسلاید اصلی"))
    if t == "original_guided_reading":
        img = _img("source_crop_path") or _img("original_slide_image_path")
        return render_source_crop(img, title, body)
    if t == "original_recap":
        return render_recap(_img("original_slide_image_path"), title, body)
    if t == "formula_card":
        fl = slide.get("formula_latex") or [""]
        return render_formula_card(title, fl[0] if fl else "", body)
    if t == "symbol_table":
        return render_symbol_table(title, slide.get("symbol_rows") or [])
    if t == "example":
        return render_example(title, body, slide.get("steps"))
    if t == "checkpoint":
        return render_question_card(title, slide.get("question_fa", body), slide.get("answer_fa", ""))
    if t in ("exam_tips", "other") and slide.get("items"):
        return render_bullets(title, slide["items"], badge_fa=slide.get("badge_fa", ""))
    # enriched_teaching + fallback
    return render_concept(title, body, badge_fa=slide.get("badge_fa", ""))


# ---------------------------------------------------------------------------
# deck assembly + compile
# ---------------------------------------------------------------------------
def _resolve_fonts():
    cfg_path = ROOT / "configs" / "persian_fonts.json"
    cascade = ["Vazirmatn", "Tahoma", "Arial"]
    eng = "Arial"
    mono = "Consolas"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        prim = (cfg.get("primary") or {}).get("family")
        chain = [c.get("family") for c in (cfg.get("fallback_chain") or []) if c.get("family")]
        cascade = [f for f in ([prim] + chain) if f] or cascade
        eng = (cfg.get("english_inline") or {}).get("family", eng)
    except Exception:
        pass
    main = detect_persian_font(cascade, final_fallback="DejaVu Sans")
    english = detect_persian_font([eng, "Arial"], final_fallback=main)
    return main, english, mono


def render_deck(frames: List[str], out_tex: Path, *, deck_title: str = "v2 Deep Tutor") -> Path:
    """Fill the Jinja2 Beamer template with pre-rendered frames; write the .tex."""
    from jinja2 import Environment, FileSystemLoader
    main, english, _ = _resolve_fonts()
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False,
                      keep_trailing_newline=True)
    tmpl = env.get_template(J2_NAME)
    tex = tmpl.render(main_font=main, english_font=english,
                      macros_path=_abs(TEMPLATE_DIR / MACROS_NAME),
                      deck_title=render_bilingual_line(deck_title), frames=frames)
    out_tex = Path(out_tex)
    out_tex.parent.mkdir(parents=True, exist_ok=True)
    out_tex.write_text(tex, encoding="utf-8")
    return out_tex


def compile_xelatex(tex_path, *, runs: int = 1, timeout: int = 180):
    """Compile a .tex with XeLaTeX (nonstopmode). Returns (ok, pdf_path, log_tail)."""
    tex_path = Path(tex_path).resolve()
    xelatex = shutil.which("xelatex")
    if not xelatex:
        return False, None, "xelatex not found on PATH"
    log_tail = ""
    for _ in range(max(1, runs)):
        try:
            proc = subprocess.run(
                [xelatex, "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", str(tex_path.parent), tex_path.name],
                cwd=str(tex_path.parent), capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace")
            log_tail = (proc.stdout or "")[-2500:]
        except Exception as e:
            return False, None, f"xelatex invocation failed: {e!r}"
    pdf = tex_path.with_suffix(".pdf")
    return (pdf.exists() and pdf.stat().st_size > 0), (pdf if pdf.exists() else None), log_tail
