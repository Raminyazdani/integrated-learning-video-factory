"""auto_fixer_latex - minimal rule-based LaTeX error remediation.

Phase 5A scope: just enough to handle the failure modes the smoke test
realistically hits in this sandbox:
    (1) a configured Persian font is missing -> walk the font cascade
    (2) xepersian missing -> strip xepersian (polyglossia is enough)
    (3) bidi.sty missing -> strip polyglossia and degrade to LTR
        fontspec-only mode so the rest of the toolchain can still be tested

Both fixes return a *rewritten* TeX source string; the agent then re-runs
the build. No filesystem side effects.

This is deliberately small. Phase 5B / 5C will grow it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from integrations.latex_builder import BuildResult, ErrorMatch


@dataclass(frozen=True)
class FixDecision:
    applied: bool
    reason: str
    new_tex: Optional[str] = None
    note: str = ""


def propose_fix(
    *,
    build_result,
    current_tex,
    font_cascade,
    chosen_font,
    final_fallback="DejaVu Sans",
):
    """Inspect a failed BuildResult and propose a single fix."""
    if build_result.success:
        return FixDecision(False, "build already succeeded; nothing to fix")

    # (1) Missing-font remediation.
    missing_font = _first_of(build_result.errors, "missing-font")
    if missing_font is not None:
        next_font = _next_font_in_cascade(font_cascade + [final_fallback], chosen_font)
        if next_font and next_font != chosen_font:
            rewritten = _swap_main_font(current_tex, old=chosen_font, new=next_font)
            return FixDecision(
                True,
                "missing-font detected; swapped main font {!r} -> {!r}".format(chosen_font, next_font),
                new_tex=rewritten,
                note=missing_font.line[:200],
            )
        return FixDecision(
            False,
            "missing-font but no further fallback after {!r}".format(chosen_font),
            note=missing_font.line[:200],
        )

    # (2/3) Missing-package remediation.
    missing_pkg = _first_of(build_result.errors, "missing-package")
    if missing_pkg is not None:
        pkg = (missing_pkg.detail or "").lower()
        if "xepersian" in pkg:
            rewritten = re.sub(
                r"^\s*\\usepackage(\[[^]]*\])?\{xepersian\}\s*$\n?",
                "",
                current_tex,
                flags=re.MULTILINE,
            )
            if rewritten != current_tex:
                return FixDecision(
                    True,
                    "xepersian missing; removed usepackage xepersian - polyglossia is sufficient",
                    new_tex=rewritten,
                    note=missing_pkg.line[:200],
                )
        if "bidi" in pkg or "polyglossia" in pkg:
            rewritten = _strip_polyglossia(current_tex)
            if rewritten != current_tex:
                return FixDecision(
                    True,
                    "bidi.sty missing; degraded to LTR fontspec-only smoke source "
                    "(polyglossia + persian language setup removed). Persian glyphs "
                    "still render via fontspec; RTL layout requires installing bidi.",
                    new_tex=rewritten,
                    note=missing_pkg.line[:200],
                )
        return FixDecision(
            False,
            "missing-package {!r} has no auto-fix rule yet".format(pkg),
            note=missing_pkg.line[:200],
        )

    return FixDecision(False, "no known rule matches the observed errors")


# --- helpers ----------------------------------------------------------------


def _first_of(errors, kind):
    for e in errors:
        if e.kind == kind:
            return e
    return None


def _next_font_in_cascade(cascade, current):
    lower_cur = current.lower()
    seen_current = False
    for f in cascade:
        if seen_current:
            return f
        if f.lower() == lower_cur:
            seen_current = True
    return None


def _swap_main_font(tex, *, old, new):
    pattern = re.compile(
        r"(\\(?:setmainfont|setsansfont|newfontfamily\\[A-Za-z]+|setotherlanguage)?\s*\{)\s*"
        + re.escape(old)
        + r"\s*(\})"
    )
    return pattern.sub(r"\g<1>" + new + r"\g<2>", tex)


def _strip_polyglossia(tex):
    """Remove polyglossia and any Persian/English language-setup commands."""
    out = tex
    out = re.sub(
        r"^\s*\\usepackage(\[[^]]*\])?\{polyglossia\}\s*\n?",
        "", out, flags=re.MULTILINE,
    )
    out = re.sub(
        r"^\s*\\(?:setdefaultlanguage|setotherlanguage|setmainlanguage)(\[[^]]*\])?\{[^}]*\}\s*\n?",
        "", out, flags=re.MULTILINE,
    )
    out = re.sub(
        r"\\begin\{english\}(.*?)\\end\{english\}",
        r"\1", out, flags=re.DOTALL,
    )
    out = re.sub(r"\\textenglish\{([^}]*)\}", r"\1", out)
    return out
