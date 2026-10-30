"""lessonbook_latex_renderer — canonical PDF lessonbook render (XeLaTeX, document class).

Executor only: consumes Claude-authored lessonbook narrative artifacts
(`lessonbook_chapter_narrative` / `lessonbook_outline`) and produces a coherent,
prose-first lessonbook PDF per docs/LESSONBOOK_VISIBLE_STRUCTURE_POLICY.md.

Reuses the proven RTL/LTR mixed-text helpers from latex_slide_renderer
(render_bilingual_line, \\lrterm, math islands) so English technical terms render
LTR inside the RTL Persian body. Makes NO teaching decisions; never authors
content; no TTS/video. The .tex is assembled from templates/lessonbook/*.

Public API:
  render_chapter_body(narrative, run_dir) -> str
  render_chapter_tex(narrative, number, run_dir) -> str
  render_book(chapter_tex_list, out_tex, book_title="") -> Path
  build_from_run(run_dir, out_tex=None) -> dict     # Prompt 8 full build
  compile_xelatex(...)  (re-exported)               # XeLaTeX compile
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the proven RTL/LTR machinery + XeLaTeX compile from the slide renderer.
from integrations.latex_slide_renderer import (  # noqa: E402
    render_bilingual_line, render_ltr_term, render_math, _escape_tex,
    _resolve_fonts, compile_xelatex, _abs,
)

TEMPLATE_DIR = ROOT / "templates" / "lessonbook"
MAIN_J2 = "main_lessonbook.tex.j2"
CHAPTER_J2 = "chapter_template.tex.j2"
MACROS_NAME = "lessonbook_macros.tex"

# beat types that are plain prose paragraphs
_PROSE_TYPES = {"opening_intuition", "why_it_matters", "geometric_meaning", "transition"}


def _env():
    """Jinja2 env with LaTeX-friendly delimiters (no clash with { } or %)."""
    from jinja2 import Environment, FileSystemLoader
    return Environment(
        block_start_string="<%", block_end_string="%>",
        variable_start_string="<<", variable_end_string=">>",
        comment_start_string="<#", comment_end_string="#>",
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False, keep_trailing_newline=True,
        trim_blocks=True, lstrip_blocks=True,
    )


# A comma-separated run of >=2 LTR islands = an English enumeration that bidi would
# otherwise reverse term-by-term. Merge it into one breakable LTR \lrgroup{...}.
_LTR_TOKEN = r"\\lrterm\{[^{}]*\}"
_LTR_CORE_RE = re.compile(r"\\lrterm\{([^{}]*)\}")
_LTR_LIST_RE = re.compile(_LTR_TOKEN + r"(?:\s*[،,]\s*" + _LTR_TOKEN + r")+")


def _group_ltr_lists(tex: str) -> str:
    """Collapse 'lrterm، lrterm، …' into one left-to-right \\lrgroup so an English
    list embedded in RTL Persian keeps its source order and can wrap cleanly."""
    def _repl(m: "re.Match[str]") -> str:
        # each term stays intact (\mbox = no mid-term break); the list may break
        # only at the ", " between terms.
        cores = _LTR_CORE_RE.findall(m.group(0))
        return "\\lrgroup{" + ", ".join("\\mbox{" + c + "}" for c in cores) + "}"
    return _LTR_LIST_RE.sub(_repl, tex)


def _prose(s: Optional[str]) -> str:
    return _group_ltr_lists(render_bilingual_line(s or ""))


def _split_tail_outside_math(seg: str):
    """Peel a trailing concluding sentence (first '. ' that is OUTSIDE $...$) off a
    comparison segment so 'hinge $=…$. پس …' -> ('hinge $=…$.', 'پس …')."""
    in_math = False
    for i, c in enumerate(seg):
        if c == "$":
            in_math = not in_math
        elif not in_math and c == "." and i + 1 < len(seg) and seg[i + 1] == " ":
            return seg[: i + 1].strip(), seg[i + 2:].strip()
    return seg, None


def _answer_block(answer_fa: Optional[str]) -> str:
    """Render an exercise answer. A multi-item formula comparison (>=2 '؛'-separated
    segments, most carrying math) becomes an aligned mini-list (+ optional concluding
    line) instead of one crowded inline Persian-English-math sentence."""
    ans = (answer_fa or "").strip()
    segs = [x.strip() for x in ans.split("؛") if x.strip()]
    mathy = sum(1 for x in segs if "$" in x)
    if len(segs) >= 2 and mathy >= 2:
        tail = None
        head, t = _split_tail_outside_math(segs[-1])
        if t:
            segs[-1], tail = head, t
        items = "\n".join("\\item " + _prose(x) for x in segs)
        out = "\\textbf{پاسخ:}\n\\begin{lbminilist}\n" + items + "\n\\end{lbminilist}"
        if tail:
            out += "\n\\par\\vspace{0.15em}" + _prose(tail)
        return out
    return "\\textbf{پاسخ:} " + _prose(ans)


def _resolve_img(image_path: Optional[str], run_dir: Path) -> Optional[Path]:
    if not image_path:
        return None
    p = Path(image_path)
    return p if p.is_absolute() else (run_dir / p)


# ---------------------------------------------------------------------------
# element renderers (Layer-B woven; cues not headings)
# ---------------------------------------------------------------------------
def render_termintro(term_en: str, gloss_fa: str) -> str:
    return "\\termintro{" + _escape_tex(term_en) + "}{" + _prose(gloss_fa) + "}"


def render_sourcepage(anchor: Dict[str, Any], run_dir: Path) -> str:
    p = _resolve_img(anchor.get("image_path"), run_dir)
    if p is None:
        return ""
    notice = _prose(anchor.get("what_to_notice_fa", "")) if anchor.get("what_to_notice_fa") else ""
    return "\\sourcepage[" + notice + "]{" + _abs(p) + "}"


def render_visual(v: Dict[str, Any], run_dir: Path) -> str:
    """Place a teaching visual (full-slide anchor / crop / annotated / masked / generated)
    next to its prose, with a Persian caption. Adapted visuals are labelled as adapted."""
    p = _resolve_img(v.get("path") or v.get("image_path"), run_dir)
    if p is None:
        return ""
    cap = _prose(v.get("caption_fa", "") or "")
    kind = v.get("kind") or v.get("asset_type") or "crop"
    img = _abs(p)
    if kind == "full_slide_anchor":
        return "\\sourcepage[" + cap + "]{" + img + "}"
    if kind in ("annotated_crop", "masked_variant", "generated_teaching_diagram", "side_by_side"):
        return "\\annotatedfigure{" + img + "}{" + cap + "}"
    return "\\cropfigure{" + img + "}{" + cap + "}"


def render_formula_walkthrough(f: Dict[str, Any]) -> str:
    """Motivation -> display math -> symbol meanings in prose -> example/geometry/mistake.
    All inside ONE box (\\mathwalkthrough); no standalone Symbol Table."""
    parts: List[str] = []
    if f.get("motivation_fa"):
        parts.append(_prose(f["motivation_fa"]))
    if f.get("formula_latex"):
        parts.append(render_math(f["formula_latex"], display=True))
    syms = f.get("symbols") or []
    if syms:
        items = "\n".join(
            "\\item " + render_math(s.get("symbol", "")) + "~—~" + _prose(s.get("meaning_fa", ""))
            for s in syms)
        parts.append("در این رابطه:\n\\begin{lbminilist}\n" + items + "\n\\end{lbminilist}")
    for key in ("numeric_example_fa", "geometric_fa", "common_mistake_fa"):
        if f.get(key):
            parts.append(_prose(f[key]))
    inner = "\n\\par\\vspace{0.4em}\n".join(p for p in parts if p)
    return "\\mathwalkthrough{%\n" + inner + "\n}"


def render_example_box(ex: Dict[str, Any]) -> str:
    parts: List[str] = []
    if ex.get("statement_fa"):
        parts.append(_prose(ex["statement_fa"]))
    steps = ex.get("steps_fa") or []
    if steps:
        parts.append("\\begin{enumerate}\n" + "\n".join("\\item " + _prose(s) for s in steps) + "\n\\end{enumerate}")
    if ex.get("answer_fa"):
        parts.append(_answer_block(ex["answer_fa"]))
    return "\\examplebox{%\n" + "\n\\par\\vspace{0.3em}\n".join(p for p in parts if p) + "\n}"


def render_practice_box(p: Dict[str, Any]) -> str:
    body = _prose(p.get("question_fa", ""))
    if p.get("hint_fa"):
        body += "\\par\\vspace{0.2em}\\textit{راهنمایی: " + _prose(p["hint_fa"]) + "}"
    if p.get("answer_fa"):
        body += "\\par\\vspace{0.2em}" + _answer_block(p["answer_fa"])
    return "\\smallpractice{%\n" + body + "\n}"


def render_examtrap_box(e: Dict[str, Any]) -> str:
    return "\\examtrap{%\n" + _prose(e.get("text_fa", "")) + "\n}"


# ---------------------------------------------------------------------------
# beat + chapter assembly
# ---------------------------------------------------------------------------
def _term_for_beat(beat: Dict[str, Any], narrative: Dict[str, Any]):
    if beat.get("term_en"):
        return beat.get("term_en"), beat.get("gloss_fa", "")
    bid = beat.get("beat_id")
    for ti in (narrative.get("term_intros") or []):
        if ti.get("first_use_beat") == bid:
            return ti.get("term_en"), ti.get("gloss_fa", "")
    return None, None


def render_beat(beat: Dict[str, Any], narrative: Dict[str, Any], run_dir: Path) -> str:
    t = beat.get("type")
    bid = beat.get("beat_id")
    prose = beat.get("prose_fa", "")
    out: List[str] = []

    if t == "source_anchor":
        if beat.get("source_anchor"):
            out.append(render_sourcepage(beat["source_anchor"], run_dir))
        if prose:
            out.append("\\guidedreading{%\n" + _prose(prose) + "\n}")
    elif t == "term_intro":
        term_en, gloss = _term_for_beat(beat, narrative)
        lead = (render_termintro(term_en, gloss) + " ") if term_en else ""
        out.append(lead + _prose(prose))
    elif t == "recap":
        out.append("\\teacherrecap{%\n" + _prose(prose) + "\n}")
    elif t == "guided_explanation":
        # plain prose by default (boxing every explanation would look report-like)
        out.append(_prose(prose))
    else:  # opening_intuition / why_it_matters / geometric_meaning / transition / formula / example / ...
        if prose:
            out.append(_prose(prose))

    # attached elements (woven at this beat)
    for f in (narrative.get("formulas") or []):
        if f.get("attaches_to_beat") == bid:
            out.append(render_formula_walkthrough(f))
    for ex in (narrative.get("examples") or []):
        if ex.get("attaches_to_beat") == bid:
            out.append(render_example_box(ex))
    for e in (narrative.get("exam_insights") or []):
        if e.get("attaches_to_beat") == bid:
            out.append(render_examtrap_box(e))
    for pr in (narrative.get("practice") or []):
        if pr.get("attaches_to_beat") == bid:
            out.append(render_practice_box(pr))
    for fig in (narrative.get("figures") or []):
        if fig.get("anchor_beat") == bid:
            out.append(render_sourcepage(
                {"image_path": fig.get("image_path"), "what_to_notice_fa": fig.get("what_to_notice_fa")}, run_dir))
    for v in (narrative.get("visuals") or []):
        if v.get("attaches_to_beat") == bid:
            out.append(render_visual(v, run_dir))

    return "\n\\par\\vspace{0.35em}\n".join(x for x in out if x)


def render_chapter_body(narrative: Dict[str, Any], run_dir: Path) -> str:
    return "\n\n".join(render_beat(b, narrative, run_dir) for b in (narrative.get("beats") or []))


def render_chapter_tex(narrative: Dict[str, Any], number, run_dir: Path) -> str:
    tmpl = _env().get_template(CHAPTER_J2)
    return tmpl.render(number=str(number),
                       title=_prose(narrative.get("title_fa", "")),
                       body=render_chapter_body(narrative, run_dir))


def render_book(chapter_tex_list: List[str], out_tex, *, book_title: str = "") -> Path:
    main, english, _ = _resolve_fonts()
    tmpl = _env().get_template(MAIN_J2)
    tex = tmpl.render(main_font=main, english_font=english,
                      macros_path=_abs(TEMPLATE_DIR / MACROS_NAME),
                      book_title=_prose(book_title) if book_title else "",
                      chapters=chapter_tex_list)
    out_tex = Path(out_tex)
    out_tex.parent.mkdir(parents=True, exist_ok=True)
    out_tex.write_text(tex, encoding="utf-8")
    return out_tex


def page_count_from_log(log_path) -> Optional[int]:
    """Parse XeLaTeX log: 'Output written on file.pdf (N pages, ...)'."""
    try:
        txt = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"Output written on .*?\(\s*(\d+)\s+pages?\)", txt, re.S)
    return int(m.group(1)) if m else None


def build_from_run(run_dir, out_tex=None) -> Dict[str, Any]:
    """Full build (Prompt 8): read lessonbook_full/{lessonbook_outline.json,
    chapter_*_narrative.json} -> render book -> compile -> return result dict.
    Does NOT author content; fails closed if narrative artifacts are missing."""
    run_dir = Path(run_dir)
    lf = run_dir / "lessonbook_full"
    outline_p = lf / "lessonbook_outline.json"
    if not outline_p.is_file():
        return {"ok": False, "error": f"missing {outline_p}"}
    outline = json.loads(outline_p.read_text(encoding="utf-8"))
    chapters_tex: List[str] = []
    for ch in sorted(outline.get("chapters", []), key=lambda c: c.get("order", 0)):
        cid = ch.get("chapter_id", "")
        nar_p = lf / f"{cid}_narrative.json"
        if not nar_p.is_file():
            return {"ok": False, "error": f"missing narrative {nar_p}"}
        nar = json.loads(nar_p.read_text(encoding="utf-8"))
        chapters_tex.append(render_chapter_tex(nar, ch.get("order", len(chapters_tex) + 1), run_dir))
    out_tex = Path(out_tex) if out_tex else (lf / "final_lessonbook.tex")
    render_book(chapters_tex, out_tex, book_title=outline.get("book_title_fa", ""))
    ok, pdf, log_tail = compile_xelatex(out_tex, runs=2)
    return {"ok": ok, "tex": str(out_tex), "pdf": (str(pdf) if pdf else None),
            "page_count": page_count_from_log(out_tex.with_suffix(".log")), "log_tail": log_tail}


def render_chapter_markdown(narrative: Dict[str, Any]) -> str:
    """Emit a human-readable prose twin (.md) of the woven chapter.
    Serialization only — the content is the Claude-authored narrative; Python adds
    no teaching. Mirrors the beat order + attached elements of the JSON."""
    def by_beat(key: str, beatkey: str) -> Dict[str, list]:
        d: Dict[str, list] = {}
        for el in (narrative.get(key) or []):
            d.setdefault(el.get(beatkey), []).append(el)
        return d

    fbb = by_beat("formulas", "attaches_to_beat")
    exb = by_beat("examples", "attaches_to_beat")
    eib = by_beat("exam_insights", "attaches_to_beat")
    prb = by_beat("practice", "attaches_to_beat")
    vbb = by_beat("visuals", "attaches_to_beat")

    L: List[str] = [f"# {narrative.get('title_fa','')}\n"]
    for b in (narrative.get("beats") or []):
        bid = b.get("beat_id"); t = b.get("type"); prose = b.get("prose_fa", "")
        if t == "term_intro" and b.get("term_en"):
            L.append(f"**{b['term_en']}** ({b.get('gloss_fa','')}) — {prose}\n")
        elif t == "source_anchor":
            sa = b.get("source_anchor") or {}
            if sa.get("image_path"):
                L.append(f"![منبع: صفحهٔ {sa.get('page','')}]({sa['image_path']})\n")
            if sa.get("what_to_notice_fa"):
                L.append(f"> **به این نگاه کن:** {sa['what_to_notice_fa']}\n")
            L.append(prose + "\n")
        elif t == "recap":
            L.append(f"## جمع‌بندی این بخش\n\n{prose}\n")
        else:
            L.append(prose + "\n")
        for f in fbb.get(bid, []):
            L.append("> **حالا فرمول را آرام باز کنیم.** " + f.get("motivation_fa", ""))
            L.append(f"\n$$ {f.get('formula_latex','')} $$\n")
            syms = f.get("symbols") or []
            if syms:
                L.append("در این رابطه: " + "؛ ".join(f"${s.get('symbol','')}$ {s.get('meaning_fa','')}" for s in syms) + ".\n")
            for k in ("numeric_example_fa", "geometric_fa", "common_mistake_fa"):
                if f.get(k):
                    L.append(f.get(k) + "\n")
        for ex in exb.get(bid, []):
            L.append("> **یک مثال کوچک.** " + ex.get("statement_fa", ""))
            for s in (ex.get("steps_fa") or []):
                L.append(f"> - {s}")
            if ex.get("answer_fa"):
                L.append("> \n> **پاسخ:** " + ex["answer_fa"] + "\n")
        for e in eib.get(bid, []):
            L.append("> **حواست باشد:** " + e.get("text_fa", "") + "\n")
        for p in prb.get(bid, []):
            L.append("> **یک تمرین کوچک:** " + p.get("question_fa", ""))
            if p.get("answer_fa"):
                L.append("> \n> **پاسخ:** " + p["answer_fa"] + "\n")
        for v in vbb.get(bid, []):
            adapted = (v.get("kind") or v.get("asset_type")) in ("annotated_crop", "masked_variant", "generated_teaching_diagram", "side_by_side")
            tag = " (بازنماییِ آموزشی)" if adapted else ""
            L.append(f"![{v.get('caption_fa','')}{tag}]({v.get('path','')})\n")
    return "\n".join(L) + "\n"
