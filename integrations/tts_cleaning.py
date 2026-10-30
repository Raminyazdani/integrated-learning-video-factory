"""tts_cleaning — Persian-text cleanup helpers for TTS.

This module is the canonical pre-TTS text normalizer. It applies, in order:

    1. ``pronunciation_hints`` from the narration entry (term → Persian phrase),
       if supplied — these come from the narration writer and win.
    2. A small math-symbol map (``w_0`` → «دبلیو زیر صفر», etc.).
    3. A small inline-term map (English technical terms transliterated when
       safe — e.g. ``perceptron`` → «پرسپترون»).
    4. Persian digit normalization (ASCII ``0-9`` → ۰-۹) when enabled.
    5. Whitespace cleanup.

It is conservative on purpose: the narration writer (Phase 7) already produces
text close to a final read script. The point here is to fix things that
*specifically* mispronounce in TTS engines (math symbols, English tokens
sprinkled inside Persian sentences).

The functions never mutate the input narration JSON; they return new strings.

Public API
----------
``clean_text_for_tts(text, hints=None, *, apply_symbol_map=True,
                     apply_term_replacements=True, persian_digit_conversion=True)
                     -> str``

``compute_text_stats(text) -> dict``  — chars / words / estimated seconds
                                        (≈12 chars/sec Persian, matching
                                        narration_writer_agent.py).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------

# Math/symbol patterns. Order matters — longer patterns first.
_MATH_PATTERNS: List[Tuple[str, str]] = [
    ("(x_n, y_n)", "زوج ایکس زیر ان و وای زیر ان"),
    ("w_0", "دبلیو زیر صفر"),
    ("w_1", "دبلیو زیر یک"),
    ("w_i", "دبلیو زیر آی"),
    ("x_n", "ایکس زیر ان"),
    ("y_n", "وای زیر ان"),
    ("w.x", "ضرب داخلی دبلیو در ایکس"),
    ("w*x", "ضرب داخلی دبلیو در ایکس"),
    ("f(x)", "اف از ایکس"),
    ("sign(", "تابع علامت سین "),
    ("{-1,", " منهای یک و ", ),
    ("{0,", " صفر و ", ),
    (",+1}", " مثبت یک ",),
    (",1}", " یک ",),
    ("sigma", "سیگما"),
    ("alpha", "آلفا"),
    ("beta", "بتا"),
    ("theta", "تتا"),
    ("lambda", "لاندا"),
    ("=>", " پس "),
    ("->", " به "),
]

# Inline technical terms that read better as Persian phonetic.
_TERM_REPLACEMENTS: List[Tuple[str, str]] = [
    ("Perceptron", "پرسپترون"),
    ("perceptron", "پرسپترون"),
    ("Bias", "بایاس"),
    ("bias", "بایاس"),
    ("Loss", "لاس"),
    ("loss", "لاس"),
    ("Threshold", "تِرِشهولد"),
    ("threshold", "تِرِشهولد"),
    ("Gradient", "گرَدیان"),
    ("gradient", "گرَدیان"),
    ("Overfit", "اُورفیت"),
    ("Overfitting", "اُورفیتینگ"),
    ("Underfit", "آندرفیت"),
    ("Underfitting", "آندرفیتینگ"),
    ("Regularization", "رگولاریزیشن"),
    ("Validation", "ولیدِیشن"),
    ("Training", "ترِینینگ"),
    ("Inference", "اینفِرنس"),
    ("Feature", "فیچر"),
    ("features", "فیچرها"),
    ("ML", "ام اِل"),
    ("AI", "اِی آی"),
]

# ASCII → Persian digit map.
_ASCII_TO_FA_DIGIT = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Persian speaking rate used by the narration writer.
PERSIAN_CHARS_PER_SECOND = 12.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_replacements(text: str, pairs: Iterable[Tuple[str, str]]) -> str:
    for needle, replacement in pairs:
        if not needle:
            continue
        text = text.replace(needle, replacement)
    return text


def _normalize_whitespace(text: str) -> str:
    # Collapse runs of whitespace, normalize NBSP and ZWNJ-related noise.
    text = unicodedata.normalize("NFC", text)
    # Keep ZWNJ (‌) as-is — it matters for Persian display, TTS handles it.
    text = text.replace("​", "")  # strip ZWSP only
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def clean_text_for_tts(
    text: str,
    hints: Optional[List[dict]] = None,
    *,
    apply_symbol_map: bool = True,
    apply_term_replacements: bool = True,
    persian_digit_conversion: bool = True,
) -> str:
    """Return a TTS-friendly version of ``text``.

    ``hints``: optional list of ``{"term": ..., "hint_fa": ...}`` dicts from
    the narration entry's ``pronunciation_hints`` field. They are applied
    first so the narration writer's local decisions win.
    """
    if not text:
        return ""

    out = text

    # 1. Per-entry hints from the narration writer.
    if hints:
        pairs: List[Tuple[str, str]] = []
        for h in hints:
            term = (h or {}).get("term") or ""
            hint = (h or {}).get("hint_fa") or ""
            if term and hint:
                pairs.append((term, hint))
        if pairs:
            # Longest first so hints like "w*x" win over "w".
            pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
            out = _apply_replacements(out, pairs)

    # 2. Math/symbol map.
    if apply_symbol_map:
        out = _apply_replacements(out, _MATH_PATTERNS)

    # 3. Inline English-term replacements.
    if apply_term_replacements:
        out = _apply_replacements(out, _TERM_REPLACEMENTS)

    # 4. ASCII → Persian digits.
    if persian_digit_conversion:
        out = out.translate(_ASCII_TO_FA_DIGIT)

    # 5. Final whitespace pass.
    out = _normalize_whitespace(out)
    return out


def compute_text_stats(text: str) -> dict:
    """Cheap, deterministic stats used by TTS planning / QA.

    The estimated_seconds uses the same constant as
    ``narration_writer_agent.PERSIAN_CHARS_PER_SECOND`` so plans align.
    """
    s = text or ""
    n_chars = len(s)
    n_words = len([w for w in re.split(r"\s+", s) if w])
    est_seconds = n_chars / PERSIAN_CHARS_PER_SECOND if n_chars else 0.0
    return {
        "n_chars": n_chars,
        "n_words": n_words,
        "estimated_seconds": round(est_seconds, 2),
    }
