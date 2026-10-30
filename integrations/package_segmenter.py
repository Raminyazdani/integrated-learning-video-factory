"""package_segmenter — deterministic Stage 3 (Phase 3B).

Greedy boundary-signal walker over the enriched page list. Honors:
  - hard rules: consecutive, coverage, size limits, no duplicates
  - soft rules: prefer-start at outline/title/transition/section_boundary,
    suggested_groupings prior, still_uncertain pages flagged
  - config limits: max_pages_per_package from configs/pipeline_config.yaml

No LLM call in Phase 3B MVP. The prompt at prompts/package_grouping_prompt.md
remains the contract for future LLM-backed runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- constants

DEFAULT_MAX_PAGES_PER_PACKAGE = 5
EXCEPTIONAL_MAX_PAGES = 8

ALLOWED_DIFFICULTY = ("easy", "medium", "hard", "unknown")
ALLOWED_CONFIDENCE = ("high", "medium", "low")

# Default per-page minutes by role (visual-only pages teach faster than
# formula/derivation pages). Used by the duration estimator.
_ROLE_MINUTES = {
    "section_boundary": 0.5,
    "transition_only": 0.5,
    "context_only": 0.0,
    "concept_explanation": 2.0,
    "concept_introduction": 1.5,
    "example_walkthrough": 2.5,
    "formula_focus": 3.0,
    "visual_diagram": 1.5,
    "summary": 1.5,
    "exercise": 3.0,
    "solution": 2.5,
    "data_table": 2.0,
    "code_block": 2.5,
    "algorithm": 3.0,
    "unknown": 1.5,
}

# Conf ranking helper for aggregating per-page confidences.
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
_CONF_OF_RANK = {0: "low", 1: "medium", 2: "high"}


# --------------------------------------------------------------- containers


@dataclass
class PageView:
    """Slim view over an enriched-classification page entry, used by the walker."""
    page_id: str
    page_number: int
    primary_label: str
    secondary_labels: list
    educational_role: str
    boundary_signal: str
    confidence: str  # high|medium|low
    title_guess: Optional[str]
    visual_summary_fa: str
    visually_reviewed: bool
    uncertainty_notes: list = field(default_factory=list)


@dataclass
class Package:
    package_id: str
    pages: list  # list[PageView]
    open_reason: str = ""           # why we opened this package
    forced_close_reason: str = ""   # if closed early, why


@dataclass
class SegmentationResult:
    packages: list  # list[Package]
    context_only_pages: list  # list[str] page_ids
    skipped_pages: list       # list[str] page_ids
    overrides: list           # human-readable notes about deviations from hints


# --------------------------------------------------------- public utilities


def page_view_from_enriched(p: dict) -> PageView:
    return PageView(
        page_id=p["page_id"],
        page_number=p["page_number"],
        primary_label=p["enriched_primary_label"],
        secondary_labels=list(p.get("enriched_secondary_labels", [])),
        educational_role=p.get("educational_role", "unknown"),
        boundary_signal=p.get("package_boundary_signal", "continue"),
        confidence=p.get("confidence_after_visual_review", "low"),
        title_guess=p.get("corrected_title_guess"),
        visual_summary_fa=p.get("visual_summary_fa") or "",
        visually_reviewed=bool(p.get("visually_reviewed", False)),
        uncertainty_notes=list(p.get("uncertainty_notes", [])),
    )


# ---------------------------------------------------------- core walker


def walk_pages(
    pages: list,
    *,
    max_pages_per_package: int = DEFAULT_MAX_PAGES_PER_PACKAGE,
    hints_uncertain: Optional[list] = None,
    force_starts: Optional[list] = None,
) -> SegmentationResult:
    """Greedy boundary-signal walker. See prompts/package_grouping_prompt.md §5.

    Args:
      pages: ordered list[PageView] (one per page, sorted by page_number).
      max_pages_per_package: hard cap (config: configs/pipeline_config.yaml).
      hints_uncertain: 1-based page numbers flagged as still_uncertain by 3A.1
                       (used only to annotate overrides; does not change merging).
    """
    hints_uncertain = set(hints_uncertain or [])
    force_starts_set = set(force_starts or [])
    packages: list = []
    context_only: list = []
    skipped: list = []
    overrides: list = []
    current: Optional[Package] = None
    next_idx = 1

    def _close_current(reason: str = "") -> None:
        nonlocal current
        if current is not None:
            current.forced_close_reason = reason
            packages.append(current)
            current = None

    def _open_new(p: PageView, reason: str) -> None:
        nonlocal current, next_idx
        current = Package(
            package_id=f"package_{next_idx:03d}",
            pages=[p],
            open_reason=reason,
        )
        next_idx += 1

    for p in pages:
        # Strong prior from Phase 3A.1: natural_boundaries_before_page
        if p.page_number in force_starts_set and p.boundary_signal != "context_only":
            _close_current(reason="forced_by_3a1_natural_boundary_hint")
            _open_new(p, reason="forced_start_from_3a1_natural_boundary")
            if p.boundary_signal != "start":
                overrides.append(
                    f"page_{p.page_number:03d}: forced new package due to "
                    "Phase 3A.1 natural_boundary hint, overriding boundary_signal="
                    f"{p.boundary_signal}"
                )
            continue
        if p.boundary_signal == "context_only":
            _close_current(reason="context_only_page_encountered")
            context_only.append(p.page_id)
            continue

        if p.boundary_signal == "start":
            _close_current(reason="next_page_signals_start")
            _open_new(p, reason=f"boundary_signal=start ({p.educational_role})")
            if p.page_number in hints_uncertain:
                overrides.append(
                    f"{p.page_id}: marked still_uncertain by 3A.1 but boundary_signal=start, "
                    "respected as a new-package opener"
                )
            continue

        # boundary_signal in {"continue","boundary","unknown"}
        if current is None:
            _open_new(p, reason=f"no_active_package_at_{p.page_id}")
            continue

        if len(current.pages) >= max_pages_per_package:
            _close_current(reason=f"size_cap_{max_pages_per_package}_reached")
            _open_new(p, reason="size_cap_forced_new_package")
            continue

        # default: append
        current.pages.append(p)

    _close_current(reason="end_of_stream")

    return SegmentationResult(
        packages=packages,
        context_only_pages=context_only,
        skipped_pages=skipped,
        overrides=overrides,
    )


# ------------------------------------------------------- package field calc

def _aggregate_confidence(pages: list) -> str:
    """Package confidence = min of per-page confidence ranks.
    A package is only `high` if every page is `high`."""
    if not pages:
        return "low"
    return _CONF_OF_RANK[min(_CONF_RANK[p.confidence] for p in pages)]


def _guess_difficulty(pages: list) -> str:
    """Map dominant primary label / role to difficulty bucket.
    Vocabulary: easy|medium|hard|unknown.
    """
    has_proof_or_derivation = any(
        p.primary_label in ("proof", "theorem", "derivation") for p in pages
    )
    has_formula = any(p.primary_label == "formula" for p in pages)
    has_only_visual_or_transition = all(
        p.primary_label in ("plot_or_figure", "transition", "title", "outline", "summary")
        for p in pages
    )

    if has_proof_or_derivation:
        return "hard"
    if has_formula:
        return "medium"
    if has_only_visual_or_transition:
        return "easy"
    if any(p.primary_label in ("concept_explanation", "definition", "example") for p in pages):
        return "medium" if len(pages) >= 3 else "easy"
    return "unknown"


def _estimate_duration_minutes(pages: list) -> float:
    """Sum of role-based per-page minutes; round to 0.5."""
    total = 0.0
    for p in pages:
        total += _ROLE_MINUTES.get(p.educational_role, 1.5)
    return round(total * 2) / 2.0


def _flag_needs(pages: list) -> dict:
    """Decide whether downstream Stage 4 needs Math / Visual / Notation help."""
    needs_math = any(
        p.primary_label in ("formula", "derivation", "proof", "theorem")
        for p in pages
    )
    needs_visual = any(
        p.primary_label in ("plot_or_figure", "table")
        or p.educational_role in ("visual_diagram", "data_table")
        for p in pages
    )
    needs_notation = any(
        p.primary_label in ("formula", "derivation")
        or "notation" in (p.visual_summary_fa or "").lower()
        for p in pages
    )
    has_exercise = any(p.primary_label in ("exercise", "solution") for p in pages)
    return {
        "needs_deep_math_explanation": needs_math,
        "needs_visual_explanation": needs_visual,
        "needs_notation_explanation": needs_notation,
        "exercise_or_question_presence": has_exercise,
    }


def _next_stage_recommendation(pages: list, needs: dict) -> str:
    bits: list = []
    if needs["needs_deep_math_explanation"]:
        bits.append("Stage4: route through Math/Notation sub-agent")
    if needs["needs_visual_explanation"]:
        bits.append("Stage4: route through Figure/Plot sub-agent")
    if needs["exercise_or_question_presence"]:
        bits.append("Stage4: include worked solution")
    if not bits:
        bits.append("Stage4: standard Persian content path")
    return " | ".join(bits)


def _format_title(pages: list) -> str:
    """Compose a human Persian-leaning title from the page titles."""
    titles: list = []
    seen: set = set()
    for p in pages:
        t = (p.title_guess or "").strip()
        if not t or t in seen:
            continue
        # drop pure-number titles like '26' or '7348'
        if t.isdigit():
            continue
        seen.add(t)
        titles.append(t)
        if len(titles) >= 3:
            break
    if not titles:
        return f"بسته‌ی {pages[0].page_number}–{pages[-1].page_number}"
    if len(titles) == 1:
        return titles[0]
    return " | ".join(titles)


def _build_reason(pages: list) -> str:
    """Compose a per-package reason_for_grouping in Persian-leaning prose."""
    n = len(pages)
    if n == 1:
        p = pages[0]
        return (
            f"page {p.page_number} به‌تنهایی این بسته را تشکیل می‌دهد. "
            f"نقش آن «{p.educational_role}» است و سیگنال مرز آن «{p.boundary_signal}» بود "
            f"(عنوان: «{p.title_guess or '—'}»)."
        )
    first = pages[0]
    last = pages[-1]
    roles = []
    seen: set = set()
    for p in pages:
        r = p.educational_role
        if r not in seen:
            seen.add(r)
            roles.append(r)
    return (
        f"pages {first.page_number}–{last.page_number} ({n} صفحه) به‌خاطر تداوم موضوع و نقش‌های "
        f"({', '.join(roles)}) به‌صورت یک واحد آموزشی گروه‌بندی شده‌اند. "
        f"این بسته با page {first.page_number} (سیگنال start، عنوان «{first.title_guess or '—'}») آغاز می‌شود "
        f"و تا page {last.page_number} (عنوان «{last.title_guess or '—'}») ادامه می‌یابد."
    )


def _collect_main_concepts(pages: list) -> list:
    """Extract main_concepts from titles and labels. Always returns ≥ 1 entry."""
    concepts: list = []
    seen: set = set()
    for p in pages:
        if p.title_guess and not p.title_guess.isdigit():
            t = p.title_guess.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                concepts.append(t)
        for lab in [p.primary_label] + list(p.secondary_labels):
            if lab in ("title", "outline", "transition", "context_only", "unknown"):
                continue
            if lab not in seen:
                seen.add(lab)
                concepts.append(lab)
    if not concepts:
        concepts.append(f"package_{pages[0].page_number}_{pages[-1].page_number}")
    return concepts[:8]


def _collect_prereqs(pages: list, prior_packages: list) -> list:
    """Heuristic prereqs: main_concepts of the immediately preceding package, if any."""
    if not prior_packages:
        return []
    prior = prior_packages[-1]
    pri_pages = prior.pages
    pri_concepts = _collect_main_concepts(pri_pages)
    return pri_concepts[:3]


def _collect_formulas(pages: list) -> list:
    """We do not have raw formula text; we record per-page page_ids that contain formulas."""
    return [p.page_id for p in pages if p.primary_label == "formula"
            or "formula" in p.secondary_labels]


def _collect_figures(pages: list) -> list:
    return [p.page_id for p in pages if p.primary_label in ("plot_or_figure", "table")
            or "plot_or_figure" in p.secondary_labels
            or "table" in p.secondary_labels]


def _page_roles(pages: list) -> list:
    return [p.educational_role for p in pages]


# ---------------------------------------------------------- package builder


def package_to_manifest_entry(
    pkg: Package,
    *,
    source_file: Optional[str] = None,
    prior_packages: Optional[list] = None,
    suggested_grouping: Optional[dict] = None,
    hints_uncertain: Optional[set] = None,
    overrides_notes: Optional[list] = None,
) -> dict:
    """Build one schema-conformant package_manifest entry from a Package.

    Args:
      pkg: a closed Package from walk_pages.
      source_file: original source path string.
      prior_packages: list of previously closed Packages (for prereq inference).
      suggested_grouping: matching item from segmentation_hints.suggested_groupings,
                          if any (used to detect deviations).
      hints_uncertain: set of still_uncertain page numbers (used for uncertainty_notes).
      overrides_notes: from walk_pages.overrides (used for uncertainty_notes).
    """
    pages = pkg.pages
    page_numbers = [p.page_number for p in pages]
    page_ids = [p.page_id for p in pages]

    needs = _flag_needs(pages)
    diff = _guess_difficulty(pages)
    conf = _aggregate_confidence(pages)
    title = _format_title(pages)
    reason = _build_reason(pages)
    main_concepts = _collect_main_concepts(pages)
    prereqs = _collect_prereqs(pages, prior_packages or [])
    duration = _estimate_duration_minutes(pages)

    uncertainty: list = []
    for p in pages:
        for note in p.uncertainty_notes:
            if note and note not in uncertainty:
                uncertainty.append(f"{p.page_id}: {note}")
    if hints_uncertain:
        flagged = [p.page_number for p in pages if p.page_number in hints_uncertain]
        if flagged:
            uncertainty.append(
                f"pages {flagged} were flagged as still_uncertain by Phase 3A.1 "
                "— Phase 4 may want to re-examine their boundary placement"
            )
    if suggested_grouping and suggested_grouping["pages"] != page_numbers:
        uncertainty.append(
            f"deviates from Phase 3A.1 suggested grouping "
            f"{suggested_grouping['pages']} (got {page_numbers})"
        )
    # any walker-level override notes that touch a page in this package
    if overrides_notes:
        for note in overrides_notes:
            for pid in page_ids:
                if pid in note and note not in uncertainty:
                    uncertainty.append(note)

    if len(pages) > DEFAULT_MAX_PAGES_PER_PACKAGE:
        uncertainty.append(
            f"package size {len(pages)} exceeds default cap "
            f"{DEFAULT_MAX_PAGES_PER_PACKAGE} (exceptional)"
        )

    entry = {
        "package_id": pkg.package_id,
        "package_title": title,
        "source_file": source_file or "",
        "page_range": [page_numbers[0], page_numbers[-1]],
        "original_page_numbers": page_numbers,
        "page_ids": page_ids,
        "reason_for_grouping": reason,
        "prerequisite_concepts": prereqs,
        "main_concepts": main_concepts,
        "page_roles": _page_roles(pages),
        "formulas": _collect_formulas(pages),
        "figures_or_plots": _collect_figures(pages),
        "exercise_or_question_presence": needs["exercise_or_question_presence"],
        "needs_deep_math_explanation": needs["needs_deep_math_explanation"],
        "needs_visual_explanation": needs["needs_visual_explanation"],
        "needs_notation_explanation": needs["needs_notation_explanation"],
        "expected_difficulty": diff,
        "estimated_teaching_duration_min": duration,
        "estimated_teaching_duration_minutes": duration,
        "QA_status": "pending",
        "confidence": conf,
        "uncertainty_notes": uncertainty,
        "next_stage_recommendation": _next_stage_recommendation(pages, needs),
    }
    return entry
