"""page_classifier — pure heuristic page classifier for Phase 3A.

No external API calls. Implements the decision rules described in
prompts/page_classifier_prompt.md §4 and the allowed-labels set in §2.

Phase 3A constraint: heuristic-only. The full LLM-backed flow lives in
agents/page_classifier.py and consumes this module's `classify_page` output
as its starting point.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------- labels

ALLOWED_LABELS = frozenset(
    {
        "title",
        "outline",
        "transition",
        "definition",
        "concept_explanation",
        "theorem",
        "proof",
        "derivation",
        "formula",
        "example",
        "exercise",
        "solution",
        "plot_or_figure",
        "table",
        "code_or_pseudocode",
        "algorithm",
        "summary",
        "bibliography",
        "mixed",
        "unknown",
        "context_only",
    }
)

EVIDENCE_MAX = 400  # chars

# ---------------------------------------------------------------- signal sets

# Only unambiguous math characters. Plain `=`, `+`, `-`, `*`, `/`, `^` are
# excluded because hyphenated words ("pre-processing", "Big-data") and ASCII
# dashes in URLs produced too many false positives on slide-deck text.
_STRONG_MATH_CHARS = set(
    "∑∫∏∂∇√∞≤≥≠≈"
    "→←↔∈∉⊂⊃∩∪⊕⊗"
    "αβγδεζηθικλ"
    "μνξοπρστυφχ"
    "ψωΓΔΘΛΞΠΣΦΨΩ"
)

# Patterns that strongly indicate a real mathematical expression.
_MATH_PATTERN_RE = re.compile(
    r"\b[a-zA-Z_][a-zA-Z_0-9]*\s*\(\s*[a-zA-Z0-9_,\s]*\s*\)\s*="
    r"|(?<![A-Za-z])[a-zA-Z]\s*=\s*[\d\.\+\-\*/\(]"
    r"|\d+\s*[+×÷*/]\s*\d+"
    r"|\b\d+\s*=\s*\d+"
    r"|\^\s*[\d{(]"
    r"|\\[a-zA-Z]+\s*\{"
    r"|\b(?:sum|prod|int|sqrt|frac|sigma|alpha|beta|gamma|theta|lambda|mu|pi)\b"
    r"|→|⟹|⇒|⇔",
    re.IGNORECASE,
)

_FORMULA_KW = ("formula", "equation", "فرمول", "معادله")
_OUTLINE_KW = ("outline", "agenda", "contents", "table of contents",
               "فهرست", "سرفصل", "برنامه")
_BIB_KW = ("references", "bibliography", "further reading",
           "منابع", "مراجع")
_FIGURE_KW = (
    "figure", "image from", "images from", "picture from", "pictures from",
    "plot", "diagram", "chart", "schematic",
    "تصویر", "شکل", "نمودار",
)
_DEFINITION_KW = (
    "is defined as", "we define", "definition:", "definition of", "definitions",
    "تعریف", "یعنی",
)
_EXAMPLE_KW = ("example", "e.g.", "for instance", "مثال")
_EXERCISE_KW = (
    "exercise", "problem set", "homework",
    "تمرین", "سؤال", "مسئله",
)
_SOLUTION_KW = ("answer:", "راه‌حل", "راه حل", "پاسخ:")  # bare 'solution' too noisy in prose
_SOLUTION_RE = re.compile(r'(?:^|\n)\s*solution\s*[:\.]', re.IGNORECASE)

# Algorithm needs structural context (start of line, with colon, or pseudocode).
_ALGORITHM_RE = re.compile(
    r"(?:^|\n)\s*algorithm\s*\d*\s*[:\.]"
    r"|\bpseudo(?:-|\s)?code\b"
    r"|الگوریتم",
    re.IGNORECASE,
)

_CODE_TOKENS = (
    "def ", "function(", "return ", "import ", "#include",
    "public class", "private void", "void main", "->", "fn ",
)
_THEOREM_KW = (
    "theorem", "lemma", "corollary",
    "قضیه", "لم", "نتیجه",
)
_PROOF_KW = (
    "proof", "اثبات", "برهان",
    "q.e.d.", "∎",
)
_DERIVATION_KW = (
    "derivation", "hence", "therefore", "thus we have",
    "بنابراین", "پس",
)
_SUMMARY_KW = (
    "summary", "conclusion", "takeaway",
    "خلاصه", "نتیجه‌گیری",
    "جمع‌بندی",
)

_BLOCK_RE = re.compile(r"\bblock\s+(?:i{1,3}|iv|v|vi)\b", re.IGNORECASE)
_BULLET_PREFIXES = ("§", "•", "*", "–", "○", "▪", "■", "►")
_NUMBERED_BULLET_RE = re.compile(r"^\s*\d+[\.\)]\s")
_INLINE_MATH_RE = re.compile(r"\b[a-zA-Z_]\s*\([a-zA-Z_]\)\s*=")
_QUOTE_CHARS = ('"', "“", "”", "«", "»", "''", "``")
_ASCII_DASH_PREFIXES = ("- ", "— ", "– ")

_KEY_TERMS_VOCAB = (
    "machine learning", "supervised learning", "unsupervised learning",
    "reinforcement learning", "classification", "regression", "clustering",
    "kernel", "svm", "deep learning", "neural network", "decision theory",
    "probability theory", "empirical risk minimization", "structural risk minimization",
    "density estimation", "pca", "k-means", "kmeans", "gmm", "explainability",
    "interpretability", "linear regression", "linear classification",
    "feature selection", "model training", "model validation",
    "data normalization", "data preprocessing", "data pre-processing",
    "bayesian", "vapnik", "mitchell", "samuel", "murphy", "bishop",
)


# ----------------------------------------------------------- output container


@dataclass
class HeuristicClassification:
    page_id: str
    page_number: int
    primary_label: str
    secondary_labels: list = field(default_factory=list)
    title_guess: Optional[str] = None
    short_summary_fa: str = ""
    key_terms: list = field(default_factory=list)
    formulas_detected: bool = False
    figures_or_plots_detected: bool = False
    exercise_detected: bool = False
    difficulty_guess: str = "unknown"
    needs_deep_math_explanation: bool = False
    needs_visual_explanation: bool = False
    confidence: str = "low"
    evidence_text_excerpt: str = ""
    warnings: list = field(default_factory=list)
    _signals: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "page_number": self.page_number,
            "primary_label": self.primary_label,
            "secondary_labels": list(self.secondary_labels),
            "title_guess": self.title_guess,
            "short_summary_fa": self.short_summary_fa,
            "key_terms": list(self.key_terms),
            "formulas_detected": self.formulas_detected,
            "figures_or_plots_detected": self.figures_or_plots_detected,
            "exercise_detected": self.exercise_detected,
            "difficulty_guess": self.difficulty_guess,
            "needs_deep_math_explanation": self.needs_deep_math_explanation,
            "needs_visual_explanation": self.needs_visual_explanation,
            "confidence": self.confidence,
            "evidence_text_excerpt": self.evidence_text_excerpt,
            "warnings": list(self.warnings),
        }


# ----------------------------------------------------------------- utilities


def _first_nonempty_line(text):
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s
    return None


def _count_bullets(lines):
    n = 0
    for line in lines:
        s = line.lstrip()
        if not s:
            continue
        if any(s.startswith(p) for p in _BULLET_PREFIXES):
            n += 1
        elif any(s.startswith(p) for p in _ASCII_DASH_PREFIXES):
            n += 1
        elif _NUMBERED_BULLET_RE.match(s):
            n += 1
    return n


def _count_numbered_steps(lines):
    nums = []
    for line in lines:
        m = _NUMBERED_BULLET_RE.match(line.lstrip())
        if m:
            mm = re.match(r"\d+", line.lstrip())
            if mm:
                try:
                    nums.append(int(mm.group()))
                except ValueError:
                    pass
    if len(nums) >= 3 and sorted(nums)[:3] == [1, 2, 3]:
        return len(nums)
    return 0


def _has_math_char(text):
    return any(c in _STRONG_MATH_CHARS for c in text)


def _has_inline_math(text):
    return bool(_INLINE_MATH_RE.search(text)) or bool(_MATH_PATTERN_RE.search(text))


def _has_algorithm_context(text):
    return bool(_ALGORITHM_RE.search(text))


def _quote_count(text):
    return sum(text.count(q) for q in _QUOTE_CHARS)


def _has_url(text):
    return "http://" in text or "https://" in text or "doi:" in text.lower()


def _has_year_in_paren(text):
    return bool(re.search(r"\((?:19|20)\d{2}\)", text))


def _looks_like_table(text):
    if text.count("|") < 4:
        return "table " in text.lower()
    pipe_lines = [ln for ln in text.splitlines() if ln.count("|") >= 2]
    return len(pipe_lines) >= 2 or "table " in text.lower()


def _extract_key_terms(text_lower):
    found = []
    for term in _KEY_TERMS_VOCAB:
        if term in text_lower and term not in found:
            found.append(term)
        if len(found) >= 8:
            break
    return found


def _guess_difficulty(primary_label, char_count, formulas):
    if primary_label in ("title", "outline", "transition", "context_only", "bibliography"):
        return "easy"
    if primary_label in ("theorem", "proof", "derivation"):
        return "hard"
    if formulas and char_count > 150:
        return "medium"
    if primary_label in ("definition", "concept_explanation", "example"):
        return "easy" if char_count < 250 else "medium"
    if primary_label in ("algorithm", "code_or_pseudocode"):
        return "medium"
    return "unknown"


_SUMMARY_TEMPLATES_FA = {
    "title": "این اسلاید، اسلاید عنوان است و موضوع کلی درس را معرفی می‌کند.",
    "outline": "این اسلاید فهرست/سرفصل بحث را ارائه می‌دهد.",
    "transition": "این اسلاید نقش انتقال یا تیتر بخش را دارد و محتوای تدریسی محدودی دارد.",
    "definition": "این اسلاید تعریف(های) رسمی یک یا چند مفهوم را ارائه می‌دهد.",
    "concept_explanation": "این اسلاید یک ایده/مفهوم را به‌صورت توضیحی بیان می‌کند.",
    "theorem": "این اسلاید بیان یک قضیه/لم است.",
    "proof": "این اسلاید اثبات یک قضیه را ارائه می‌دهد.",
    "derivation": "این اسلاید استخراج گام‌به‌گام یک رابطه/فرمول است.",
    "formula": "این اسلاید بر یک یا چند فرمول ریاضی متمرکز است.",
    "example": "این اسلاید یک مثال کاربردی/عددی را شرح می‌دهد.",
    "exercise": "این اسلاید یک تمرین/سؤال برای دانشجو طرح می‌کند.",
    "solution": "این اسلاید راه‌حل یک تمرین قبلی را ارائه می‌دهد.",
    "plot_or_figure": "این اسلاید عمدتاً تصویری است (شکل/نمودار/طرح‌واره)؛ متن استخراج‌شده محدود است.",
    "table": "این اسلاید داده‌ها را در قالب جدول نمایش می‌دهد.",
    "code_or_pseudocode": "این اسلاید قطعه کد یا شبه‌کد را نشان می‌دهد.",
    "algorithm": "این اسلاید یک الگوریتم را به‌صورت گام‌به‌گام شرح می‌دهد.",
    "summary": "این اسلاید جمع‌بندی/خلاصه‌ی بخش است.",
    "bibliography": "این اسلاید فهرست منابع/مراجع را ارائه می‌دهد.",
    "mixed": "این اسلاید چند موضوع متفاوت را در کنار هم آورده است.",
    "unknown": "برچسب این اسلاید قابل تشخیص قطعی نیست؛ نیاز به بررسی انسانی دارد.",
    "context_only": "این اسلاید محتوای آموزشی مستقیم ندارد و فقط نقش زمینه‌ای دارد.",
}


def _build_summary_fa(primary_label, title_guess):
    base = _SUMMARY_TEMPLATES_FA.get(primary_label, _SUMMARY_TEMPLATES_FA["unknown"])
    if title_guess:
        return base + " عنوان قابل تشخیص: «" + title_guess + "»."
    return base


# -------------------------------------------------------------- core function


def classify_page(*, page_id, page_number, text, total_pages, extraction_status="ok"):
    text = text or ""
    text_stripped = text.strip()
    lower = text_stripped.lower()
    lines = [ln for ln in text_stripped.splitlines() if ln.strip()]
    char_count = len(text_stripped)

    signals = []
    matched = []
    warnings = []

    bullet_count = _count_bullets(lines)
    numbered_step_count = _count_numbered_steps(lines)
    has_math = _has_math_char(text_stripped)
    has_inline = _has_inline_math(text_stripped)
    quote_count = _quote_count(text_stripped)
    has_url = _has_url(text_stripped)
    has_year_par = _has_year_in_paren(text_stripped)
    has_block_marker = bool(_BLOCK_RE.search(text_stripped))

    # rule 1: bibliography
    if any(kw in lower for kw in _BIB_KW):
        if (has_url or has_year_par) and len(lines) >= 2:
            matched.append("bibliography")
            signals.append("bibliography_kw+citation_format")

    # rule 2: title (page 1, short, no real math)
    if (page_number == 1 and char_count < 300 and not has_math and not has_inline
            and "bibliography" not in matched):
        matched.append("title")
        signals.append("first_page+short+no_math")

    # rule 3: outline
    outline_signal = any(kw in lower for kw in _OUTLINE_KW)
    if (outline_signal and bullet_count >= 3) or (has_block_marker and bullet_count >= 2):
        matched.append("outline")
        signals.append("outline_kw_or_block_marker+bullets")

    # rule 4: algorithm — structural only, not casual "ML algorithms"
    if _has_algorithm_context(text_stripped) or numbered_step_count >= 3:
        matched.append("algorithm")
        signals.append("algorithm_structural_or_numbered_steps")

    # rule 5: code
    if any(tok in text_stripped for tok in _CODE_TOKENS):
        matched.append("code_or_pseudocode")
        signals.append("code_tokens")

    # rule 6: theorem
    if any(kw in lower for kw in _THEOREM_KW):
        matched.append("theorem")
        signals.append("theorem_kw")

    # rule 7: proof
    if any(kw in lower for kw in _PROOF_KW):
        matched.append("proof")
        signals.append("proof_kw")

    # rule 8: derivation
    derivation_hit = any(kw in lower for kw in _DERIVATION_KW)
    if derivation_hit and (has_math or has_inline):
        matched.append("derivation")
        signals.append("derivation_kw+math")

    # rule 9: formula — only strong math or pattern
    if has_math or has_inline or any(kw in lower for kw in _FORMULA_KW):
        matched.append("formula")
        signals.append("math_char_or_math_pattern")

    # rule 10: exercise
    if any(kw in lower for kw in _EXERCISE_KW):
        matched.append("exercise")
        signals.append("exercise_kw")

    # rule 11: solution (structural keyword required to avoid 'principles of solution' false positives)
    if any(kw in lower for kw in _SOLUTION_KW) or _SOLUTION_RE.search(text_stripped):
        matched.append("solution")
        signals.append("solution_kw")

    # rule 12: example
    if any(kw in lower for kw in _EXAMPLE_KW):
        matched.append("example")
        signals.append("example_kw")

    # rule 13: table
    if _looks_like_table(text_stripped):
        matched.append("table")
        signals.append("pipe_table_or_table_kw")

    # rule 14: plot_or_figure
    if any(kw in lower for kw in _FIGURE_KW):
        matched.append("plot_or_figure")
        signals.append("figure_kw")

    # rule 15: definition
    def_kw_hit = any(kw in lower for kw in _DEFINITION_KW)
    if def_kw_hit or (quote_count >= 4 and char_count >= 100):
        matched.append("definition")
        signals.append("definition_kw_or_multi_quote")

    # rule 16: summary
    if any(kw in lower for kw in _SUMMARY_KW):
        matched.append("summary")
        signals.append("summary_kw")

    # rule 17: transition (short non-math fallback)
    if not matched and char_count < 120 and not has_math:
        matched.append("transition")
        signals.append("short_text_no_math")

    # rule 18: concept_explanation (fallback for narrative pages)
    if not matched:
        if bullet_count >= 1 or char_count >= 300:
            matched.append("concept_explanation")
            signals.append("narrative_or_bullets")

    # rule 19: context_only
    if not matched:
        if char_count < 20 or extraction_status == "empty_text":
            matched.append("context_only")
            signals.append("very_short_or_empty")

    # rule 20: mixed
    distinct = []
    for m in matched:
        if m not in distinct:
            distinct.append(m)
    is_mixed = len(distinct) >= 4

    # rule 21: unknown
    if not matched:
        matched.append("unknown")
        signals.append("fallback_unknown")
        warnings.append("no_signal_matched: heuristic could not assign a label")

    deduped = []
    for m in matched:
        if m not in deduped and m in ALLOWED_LABELS:
            deduped.append(m)

    if is_mixed:
        primary = "mixed"
        secondary = [m for m in deduped if m != "mixed"][:3]
    else:
        primary = deduped[0]
        secondary = deduped[1:4]

    # title_guess: skip pure page-number lines like "26" or "7348"
    title_guess = _first_nonempty_line(text_stripped)
    if title_guess and re.fullmatch(r"\d{1,5}", title_guess):
        for ln in lines[1:]:
            if not re.fullmatch(r"\d{1,5}", ln.strip()):
                title_guess = ln.strip()
                break

    key_terms = _extract_key_terms(lower)

    evidence = text_stripped[:EVIDENCE_MAX]
    if not evidence:
        warnings.append("no_evidence_text: page text is empty after extraction")

    formulas_detected = has_math or has_inline or any(kw in lower for kw in _FORMULA_KW)
    figures_detected = any(kw in lower for kw in _FIGURE_KW) or primary == "plot_or_figure"
    exercise_detected = any(kw in lower for kw in _EXERCISE_KW)

    needs_deep_math = primary in ("formula", "derivation", "proof", "theorem") or (
        formulas_detected and char_count > 150
    )
    needs_visual = primary in ("plot_or_figure", "table") or figures_detected

    # confidence
    strong_signals = 0
    if char_count >= 400 and len(signals) >= 1:
        strong_signals += 1
    if len(signals) >= 2:
        strong_signals += 1
    if primary in ("title", "outline", "bibliography") and signals:
        strong_signals += 1
    if primary in ("unknown", "context_only"):
        strong_signals -= 1
    if char_count < 50 and primary not in ("title", "context_only", "transition"):
        strong_signals -= 1

    if strong_signals >= 2:
        confidence = "high"
    elif strong_signals == 1:
        confidence = "medium"
    else:
        confidence = "low"

    if confidence == "low" and not warnings:
        warnings.append(
            "low_confidence: only weak signal(s) for primary=" + primary
            + "; signals=" + (",".join(signals) if signals else "none")
        )

    if char_count < 50 and "short_text" not in " ".join(warnings):
        warnings.append("short_text: " + str(char_count) + " chars only")
    if char_count < 50 and primary not in ("title", "transition", "context_only"):
        warnings.append("image_only: low text yield, classification rests on caption/page number")

    difficulty = _guess_difficulty(primary, char_count, formulas_detected)
    summary_fa = _build_summary_fa(primary, title_guess)

    return HeuristicClassification(
        page_id=page_id,
        page_number=page_number,
        primary_label=primary,
        secondary_labels=secondary,
        title_guess=title_guess,
        short_summary_fa=summary_fa,
        key_terms=key_terms,
        formulas_detected=formulas_detected,
        figures_or_plots_detected=figures_detected,
        exercise_detected=exercise_detected,
        difficulty_guess=difficulty,
        needs_deep_math_explanation=needs_deep_math,
        needs_visual_explanation=needs_visual,
        confidence=confidence,
        evidence_text_excerpt=evidence,
        warnings=warnings,
        _signals=signals,
    )
