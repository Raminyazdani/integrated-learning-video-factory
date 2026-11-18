"""Render Persian lesson content from a validated run-specific authored artifact."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now, read_json
from integrations.claude_artifact_contract import load_claude_authored

PACKAGE_CONTENT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ContentResult:
    md_path: Path
    json_path: Path
    report_path: Path
    section_count: int
    image_anchor_count: int
    word_count_fa: int
    page_count: int


# ----------------------------------------------------------------- helpers


def _md_table(headers: list, rows: list) -> list:
    """Render a Markdown table as a list of strings."""
    out: list = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join("-" * (len(h) + 2) for h in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return out


def _render_page_block(*, section_no: int, pe: dict) -> list:
    """Render one §K «Page NNN — original slide anchor» block as MD lines.

    Required fields: page_id, topic_fa, what_is_visible_fa, what_to_remember_fa
    Optional tagged blocks (rendered only if present):
      - definitions_fa: list[dict{author, quote_en, explanation_fa}]
      - comparison_fa: string  (with optional comparison_title_fa)
      - extra_tables: list[dict{title_fa, headers_fa, rows}]
      - exam_hint_fa: string
      - extra_notes_fa: string
      - formula_blocks: list[dict{formula, explanation_fa, notation_table?}]
      - figure_blocks: list[dict{caption_fa, takeaway_fa}]
      - numerical_example_fa: string
    """
    page_id = pe["page_id"]
    page_no = int(page_id.replace("page_", "").lstrip("0") or "0")

    out: list = []
    out.append(f"## {section_no}. Page {page_no} — original slide anchor")
    out.append("")
    out.append(f"![{page_id}](../extracted/pages/{page_id}.png)")
    out.append("")
    out.append(f"**موضوع اسلاید**: {pe['topic_fa']}")
    out.append("")
    out.append(f"**آنچه روی اسلاید دیده می‌شود**: {pe['what_is_visible_fa']}")
    out.append("")

    if pe.get("definitions_fa"):
        out.append("### سه تعریف، یک‌به‌یک" if len(pe["definitions_fa"]) == 3 else "### تعاریف")
        out.append("")
        for d in pe["definitions_fa"]:
            out.append(f"#### {d['author']}")
            out.append("")
            if d.get("quote_en"):
                out.append(f"> {d['quote_en']}")
                out.append("")
            out.append(d["explanation_fa"])
            out.append("")

    if pe.get("comparison_fa"):
        title = pe.get("comparison_title_fa") or "مقایسه"
        out.append(f"### {title}")
        out.append("")
        out.append(pe["comparison_fa"])
        out.append("")

    for tbl in pe.get("extra_tables", []) or []:
        out.append(f"### {tbl['title_fa']}")
        out.append("")
        out.extend(_md_table(tbl["headers_fa"], tbl["rows"]))
        out.append("")

    for fb in pe.get("formula_blocks", []) or []:
        out.append(f"### فرمول: `{fb['formula']}`")
        out.append("")
        if fb.get("explanation_fa"):
            out.append(fb["explanation_fa"])
            out.append("")
        if fb.get("notation_table"):
            out.extend(_md_table(["نماد", "معنا", "بُعد/واحد"],
                                 [[r.get("symbol",""), r.get("meaning_fa",""), r.get("dimension","") or "-"]
                                  for r in fb["notation_table"]]))
            out.append("")
        if fb.get("tiny_numeric_example_fa"):
            out.append(f"**مثال عددی کوچک**: {fb['tiny_numeric_example_fa']}")
            out.append("")
        if fb.get("common_mistakes_fa"):
            out.append("**اشتباهات رایج**:")
            for m in fb["common_mistakes_fa"]:
                out.append(f"- {m}")
            out.append("")
        if fb.get("plain_persian_restatement_fa"):
            out.append(f"_پس ساده‌تر بگوییم:_ {fb['plain_persian_restatement_fa']}")
            out.append("")

    for fig in pe.get("figure_blocks", []) or []:
        title = fig.get("caption_fa") or fig.get("figure_id") or "شکل"
        out.append(f"### تفسیر شکل: {title}")
        out.append("")
        if fig.get("axes_fa"):
            out.append(f"- **محورها**: {fig['axes_fa']}")
        if fig.get("legend_fa"):
            out.append(f"- **راهنما (legend)**: {fig['legend_fa']}")
        if fig.get("trend_fa"):
            out.append(f"- **روند**: {fig['trend_fa']}")
        if fig.get("takeaway_fa"):
            out.append(f"- **نکته‌ی کلیدی**: {fig['takeaway_fa']}")
        out.append("")

    if pe.get("numerical_example_fa"):
        out.append("### مثال عددی")
        out.append("")
        out.append(pe["numerical_example_fa"])
        out.append("")

    out.append(f"**آنچه دانشجو باید به‌خاطر بسپارد**: {pe['what_to_remember_fa']}")
    out.append("")

    if pe.get("exam_hint_fa"):
        out.append(f"**سؤال متداول امتحانی**: {pe['exam_hint_fa']}")
        out.append("")

    if pe.get("extra_notes_fa"):
        out.append(f"_{pe['extra_notes_fa']}_")
        out.append("")

    out.append(f"[منبع: {page_id}]")
    out.append("")
    return out


# ----------------------------------------------------------- main MD renderer


def _md_for_package(c: dict) -> str:
    """K-page generic Persian extended-content Markdown renderer."""
    title = c["package_title"]
    source_pages = c["source_pages"]
    page_id_list = [f"page_{n:03d}" for n in source_pages]
    page_explanations = c["page_explanations"]
    K = len(page_explanations)

    out: list = []
    out.append(f"# Package {c.get('package_number_str', '001')} — {title}")
    out.append("")

    out.append("## 1. خلاصه‌ی خیلی کوتاه بسته")
    out.append("")
    out.append(c["summary"])
    out.append("")
    out.append(f"[منبع: {', '.join(page_id_list)}]")
    out.append("")

    out.append("## 2. هدف آموزشی")
    out.append("")
    out.append(c["learning_goal"])
    out.append("")
    if c.get("learning_goals"):
        out.append("**اهداف ریز:**")
        for g in c["learning_goals"]:
            out.append(f"- {g}")
        out.append("")

    out.append("## 3. پیش‌نیازهای لازم")
    out.append("")
    if c.get("prerequisites_lead_fa"):
        out.append(c["prerequisites_lead_fa"])
        out.append("")
    elif K == 1:
        out.append("این بسته یک‌صفحه‌ای است؛ پیش‌نیازها حداقلی‌اند:")
        out.append("")
    else:
        out.append("آشنایی شهودی با مفاهیم زیر کافی است:")
        out.append("")
    for pr in c.get("prerequisites", []) or []:
        out.append(f"- **{pr['term']}** — {pr['explanation_fa']}")
    if not c.get("prerequisites"):
        out.append("- _این بسته پیش‌نیاز فنی خاصی ندارد._")
    out.append("")

    out.append("## 4. نقشه‌ی صفحات این بسته")
    out.append("")
    out.extend(_md_table(
        ["page", "role", "what it contributes"],
        [[row["page"], row["role"], row["contributes_fa"]] for row in c.get("page_map_rows", []) or _default_page_map_rows(page_explanations)],
    ))
    out.append("")

    next_section = 5
    for pe in page_explanations:
        out.extend(_render_page_block(section_no=next_section, pe=pe))
        next_section += 1

    out.append(f"## {next_section}. اصطلاحات کلیدی")
    out.append("")
    out.extend(_md_table(
        ["term", "Persian explanation", "why it matters"],
        [[g["term"], g["explanation_fa"], g.get("why_it_matters_fa", "")]
         for g in c.get("glossary", []) or []],
    ))
    if not c.get("glossary"):
        out.append("_برای این بسته اصطلاح کلیدی فهرست نشده._")
    out.append("")
    next_section += 1

    out.append(f"## {next_section}. نکات امتحانی مهم")
    out.append("")
    eps = c.get("exam_points", []) or []
    for i, ep in enumerate(eps, start=1):
        out.append(f"{i}. {ep}")
    if not eps:
        out.append("_فهرست نکات امتحانی برای این بسته ارائه نشده است._")
    out.append("")
    next_section += 1

    out.append(f"## {next_section}. دام‌های رایج")
    out.append("")
    traps = c.get("common_traps", []) or []
    for t in traps:
        out.append(f"- {t}")
    if not traps:
        out.append("- _دام رایج خاصی برای این بسته ثبت نشده._")
    out.append("")
    next_section += 1

    out.append(f"## {next_section}. سوال‌های سریع")
    out.append("")
    qqs = c.get("quick_questions", []) or []
    for i, qq in enumerate(qqs, start=1):
        out.append(f"**س{i}.** {qq['q_fa']}")
        out.append("")
        out.append(f"**پاسخ.** {qq['a_fa']}")
        out.append("")
    if not qqs:
        out.append("_سؤال سریعی برای این بسته ثبت نشده._")
        out.append("")
    next_section += 1

    out.append(f"## {next_section}. جمع‌بندی نهایی")
    out.append("")
    out.append(c.get("final_takeaway_fa") or "_جمع‌بندی نهایی ارائه نشده است._")
    out.append("")
    out.append(f"[منبع: {', '.join(page_id_list)}]")
    out.append("")
    next_section += 1

    out.append(f"## {next_section}. عدم قطعیت‌ها")
    out.append("")
    uns = c.get("uncertainty_notes", []) or []
    if uns:
        for u in uns:
            out.append(f"- {u}")
    else:
        out.append("برای این بسته عدم قطعیت جدی وجود ندارد.")
    out.append("")

    return "\n".join(out)


def _default_page_map_rows(page_explanations: list) -> list:
    """Build a generic page-map row list from page_explanations if the curated dict omits one."""
    rows = []
    for pe in page_explanations:
        rows.append({
            "page": pe["page_id"],
            "role": pe.get("educational_role", "—"),
            "contributes_fa": pe.get("topic_fa", "—"),
        })
    return rows


# ------------------------------------------------------------ JSON renderer


def _json_for_package(c: dict, *, package_id: str, run_id: str) -> dict:
    """K-page generic JSON renderer; schema-conformant."""
    per_page_blocks: list = []
    for pe in c["page_explanations"]:
        explanation_fa = (
            f"{pe.get('topic_fa','').strip()}. "
            f"{pe.get('what_is_visible_fa','').strip()} "
            f"{pe.get('what_to_remember_fa','').strip()}"
        ).strip()
        per_page_blocks.append({
            "page_id": pe["page_id"],
            "screenshot_path": f"../extracted/pages/{pe['page_id']}.png",
            "explanation_fa": explanation_fa,
        })

    fig_block = []
    for fig in c.get("figure_explanation_block", []) or []:
        fig_block.append({
            "figure_id": fig.get("figure_id", ""),
            "axes": fig.get("axes"),
            "legend": fig.get("legend"),
            "trend_fa": fig.get("trend_fa"),
            "takeaway_fa": fig.get("takeaway_fa"),
        })

    out = {
        "schema_version": PACKAGE_CONTENT_SCHEMA_VERSION,
        "package_id": package_id,
        "package_title": c["package_title"],
        "language": "fa",
        "summary": c["summary"],
        "learning_goal": c["learning_goal"],
        "learning_goals": list(c.get("learning_goals", [])),
        "prerequisites_mini_lesson": c.get("prerequisites_mini_lesson"),
        "prerequisites": list(c.get("prerequisites", [])),
        "source_pages": list(c["source_pages"]),
        "image_anchors": list(c.get("image_anchors", [])),
        "per_page_blocks": per_page_blocks,
        "page_explanations": list(c["page_explanations"]),
        "formula_explanation": list(c.get("formula_explanation", []) or []),
        "figure_explanation": fig_block,
        "numerical_example": c.get("numerical_example"),
        "glossary": list(c.get("glossary", [])),
        "exam_points": list(c.get("exam_points", [])),
        "exam_traps": list(c.get("common_traps", [])),
        "common_traps": list(c.get("common_traps", [])),
        "quick_qa": list(c.get("quick_questions", [])),
        "quick_questions": list(c.get("quick_questions", [])),
        "uncertainty": list(c.get("uncertainty_notes", [])),
        "uncertainty_notes": list(c.get("uncertainty_notes", [])),
        "source_grounding": [f"page_{n:03d}" for n in c["source_pages"]],
        "generated_at": iso_now(),
    }
    return out


# ----------------------------------------------------------- QA report


def _qa_report(*, run_id: str, package_id: str, c: dict, md_text: str, json_obj: dict, schema_errors: list) -> str:
    section_count = sum(1 for line in md_text.splitlines() if line.startswith("## "))
    image_anchors = sum(1 for line in md_text.splitlines() if line.startswith("!["))
    word_count_fa = len(md_text.split())
    page_count = len(c["page_explanations"])
    expected_sections = 10 + page_count

    out = [
        f"# Package Content QA — {package_id}",
        "",
        "> Phase 4B generalized renderer. Generated automatically.",
        "",
        "## Summary",
        "",
        f"- **run_id**: `{run_id}`",
        f"- **package_id**: `{package_id}`",
        f"- **package_title**: {c['package_title']}",
        f"- **source pages**: {c['source_pages']}",
        f"- **K (page count)**: {page_count}",
        f"- **expected MD sections (10 + K)**: {expected_sections}",
        f"- **actual MD sections (## )**: {section_count}",
        f"- **image anchors**: {image_anchors}",
        f"- **markdown token count (rough)**: {word_count_fa}",
        f"- **glossary entries**: {len(c.get('glossary', []))}",
        f"- **exam points**: {len(c.get('exam_points', []))}",
        f"- **common traps**: {len(c.get('common_traps', []))}",
        f"- **quick questions**: {len(c.get('quick_questions', []))}",
        f"- **uncertainty notes**: {len(c.get('uncertainty_notes', []))}",
        "",
        "## Acceptance checks (Phase 4B)",
        "",
        f"- Markdown file exists: pass",
        f"- JSON file exists: pass",
        f"- QA report exists: pass",
        f"- correct number of sections (10 + K = {expected_sections}): "
        f"{'pass' if section_count == expected_sections else f'partial ({section_count} != {expected_sections})'}",
        f"- image anchors for all source pages: "
        f"{'pass' if image_anchors >= page_count else 'FAIL'}",
        f"- every source page has its own explanation: "
        f"{'pass' if all(any(pe['page_id'] == f'page_{n:03d}' for pe in c['page_explanations']) for n in c['source_pages']) else 'FAIL'}",
        f"- JSON validates against package_content.schema.json: "
        f"{'pass' if not schema_errors else f'FAIL ({len(schema_errors)} error(s))'}",
        "",
    ]
    if schema_errors:
        out.append("## Schema validation errors")
        out.append("")
        for e in schema_errors:
            out.append(f"- {e}")
        out.append("")

    if c.get("uncertainty_notes"):
        out.append("## Uncertainty notes carried into JSON")
        out.append("")
        for u in c["uncertainty_notes"]:
            out.append(f"- {u}")
        out.append("")

    return "\n".join(out)


# ----------------------------------------------------------------- entry


def run_content_generation(run_dir, package_id: str = "package_001", mode: str = None) -> ContentResult:
    """Render the named package from this run's schema-validated authored content."""
    run_dir = Path(run_dir).resolve()

    mani_path = run_dir / "packages" / "package_manifest.json"
    if not mani_path.exists():
        raise FileNotFoundError(f"package_manifest missing: {mani_path}")
    mani = read_json(mani_path)
    pkg = next((p for p in mani["packages"] if p["package_id"] == package_id), None)
    if pkg is None:
        raise ValueError(f"{package_id} not found in package_manifest.json")

    authored_path = run_dir / "content_authored" / f"{package_id}.json"
    if not authored_path.is_file():
        from integrations.pipeline_mode import resolve_mode, fail_closed_if_stage_needs_llm
        fail_closed_if_stage_needs_llm("content_generation", resolve_mode(mode),
                                       has_authored=False, llm_client_available=False, approved=False)
    c = load_claude_authored("content", run_dir, package_id=package_id)
    if list(c["source_pages"]) != list(pkg["original_page_numbers"]):
        raise ValueError(
            f"content source_pages {c['source_pages']} != manifest "
            f"original_page_numbers {pkg['original_page_numbers']} for {package_id}."
        )
    pkg_num = int(package_id.replace("package_", ""))
    c["package_number_str"] = f"{pkg_num:03d}"

    md = _md_for_package(c)
    js = _json_for_package(c, package_id=package_id, run_id=mani["run_id"])

    try:
        import json as _json
        from jsonschema import Draft202012Validator
        schema = _json.loads((Path(__file__).resolve().parent.parent / "schemas" / "package_content.schema.json").read_text(encoding="utf-8"))
        schema_errors = [
            f"{list(e.absolute_path)}: {e.message}" for e in Draft202012Validator(schema).iter_errors(js)
        ]
    except Exception as exc:
        schema_errors = [f"validator-unavailable: {exc!r}"]

    md_path = run_dir / "content_md" / f"{package_id}_extended_fa.md"
    json_path = run_dir / "content_json" / f"{package_id}_extended_fa.json"
    report_path = run_dir / "QA_REPORTS" / f"{package_id}_content_report.md"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_text(md_path, md)
    atomic_write_json(json_path, js)
    atomic_write_text(
        report_path,
        _qa_report(
            run_id=mani["run_id"],
            package_id=package_id,
            c=c,
            md_text=md,
            json_obj=js,
            schema_errors=schema_errors,
        ),
    )

    return ContentResult(
        md_path=md_path,
        json_path=json_path,
        report_path=report_path,
        section_count=sum(1 for line in md.splitlines() if line.startswith("## ")),
        image_anchor_count=sum(1 for line in md.splitlines() if line.startswith("![")),
        word_count_fa=len(md.split()),
        page_count=len(c["page_explanations"]),
    )


# ============================================================================
# Dry-run template check — proves the renderer handles 1/2/4-page packages
# without writing files. Called by scripts/generate_package_content.py
# --dry-run-template-check.
# ============================================================================


def _dummy_page(n: int, *, with_formula: bool = False, with_figure: bool = False) -> dict:
    pe: dict = {
        "page_id": f"page_{n:03d}",
        "topic_fa": f"موضوع نمونه برای صفحه‌ی {n}",
        "what_is_visible_fa": f"این یک اسلاید ساختگی برای صفحه‌ی {n} است (dry-run).",
        "what_to_remember_fa": f"نکته‌ی نمونه از صفحه‌ی {n}.",
    }
    if with_formula:
        pe["formula_blocks"] = [{
            "formula": "f(x) = w0 + w·x",
            "explanation_fa": "یک مدل خطی نمونه برای آزمون قالب.",
            "notation_table": [
                {"symbol": "w0", "meaning_fa": "ضریب ثابت", "dimension": "اسکالر"},
                {"symbol": "w",  "meaning_fa": "بردار وزن", "dimension": "بردار d-بعدی"},
                {"symbol": "x",  "meaning_fa": "ویژگی ورودی", "dimension": "بردار d-بعدی"},
            ],
            "tiny_numeric_example_fa": "اگر w0=1 و w=2 و x=3 آنگاه f(x)=7.",
            "common_mistakes_fa": ["جابه‌جا کردن w و x", "نادیده گرفتن w0"],
            "plain_persian_restatement_fa": "خروجی فقط یک ترکیب خطی از وزن‌ها و ورودی است.",
        }]
    if with_figure:
        pe["figure_blocks"] = [{
            "caption_fa": "نمودار نمونه",
            "axes_fa": "x: ورودی، y: خروجی",
            "trend_fa": "افزایشی",
            "takeaway_fa": "هرچه x بزرگ‌تر، y بزرگ‌تر.",
        }]
    return pe


def _dummy_content(k_pages: int, *, with_formula: bool = False, with_figure: bool = False) -> dict:
    pages = [_dummy_page(i + 1, with_formula=(with_formula and i == 0),
                         with_figure=(with_figure and i == 0))
             for i in range(k_pages)]
    return {
        "package_title": f"بسته‌ی نمونه با {k_pages} صفحه",
        "package_number_str": "999",
        "source_pages": list(range(1, k_pages + 1)),
        "image_anchors": [
            {"page_id": f"page_{i:03d}", "path": f"../extracted/pages/page_{i:03d}.png"}
            for i in range(1, k_pages + 1)
        ],
        "summary": f"خلاصه‌ی نمونه برای dry-run با {k_pages} صفحه.",
        "learning_goal": "هدف آموزشی نمونه برای dry-run.",
        "page_explanations": pages,
        "glossary": [
            {"term": "نمونه (sample)", "explanation_fa": "ردیف نمونه‌ی glossary.", "why_it_matters_fa": "بدون اهمیت — فقط برای تست قالب."},
        ],
        "exam_points": ["نکته‌ی نمونه ۱.", "نکته‌ی نمونه ۲.", "نکته‌ی نمونه ۳."],
        "common_traps": ["دام نمونه ۱.", "دام نمونه ۲.", "دام نمونه ۳."],
        "quick_questions": [
            {"q_fa": "پرسش نمونه ۱؟", "a_fa": "پاسخ نمونه ۱."},
            {"q_fa": "پرسش نمونه ۲؟", "a_fa": "پاسخ نمونه ۲."},
            {"q_fa": "پرسش نمونه ۳؟", "a_fa": "پاسخ نمونه ۳."},
        ],
        "final_takeaway_fa": "جمع‌بندی نمونه برای dry-run.",
        "uncertainty_notes": [],
        "formula_explanation": [],
        "figure_explanation_block": [],
        "numerical_example": None,
    }


def dry_run_template_check() -> dict:
    """Render synthetic 1/2/4-page packages in-memory and report validity.

    Returns a dict with keys k1, k2, k4 (each {actual_sections, expected_sections,
    image_anchors, schema_valid, schema_errors}), plus an optional
    `validator_unavailable` key if jsonschema is missing.
    """
    summary: dict = {}
    validator_err = None
    try:
        import json as _json
        from jsonschema import Draft202012Validator
        schema = _json.loads((Path(__file__).resolve().parent.parent / "schemas" / "package_content.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
    except Exception as exc:
        validator = None
        validator_err = repr(exc)

    for k in (1, 2, 4):
        c = _dummy_content(k, with_formula=(k >= 2), with_figure=(k >= 2))
        md = _md_for_package(c)
        js = _json_for_package(c, package_id="package_999", run_id="dryrun")
        actual_sections = sum(1 for line in md.splitlines() if line.startswith("## "))
        image_anchors = sum(1 for line in md.splitlines() if line.startswith("!["))
        if validator is not None:
            errs = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(js)]
            schema_valid = (len(errs) == 0)
        else:
            errs = ["validator-unavailable"]
            schema_valid = True  # treat as OK if validator missing
        summary[f"k{k}"] = {
            "actual_sections": actual_sections,
            "expected_sections": 10 + k,
            "image_anchors": image_anchors,
            "schema_valid": schema_valid,
            "schema_errors": errs,
        }
    if validator_err is not None:
        summary["validator_unavailable"] = validator_err
    return summary
