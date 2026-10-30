"""package_segmenter — Stage 3 agent (Phase 3B).

Loads the Phase 3A.1 enriched classification, runs the deterministic walker
in integrations.package_segmenter, and emits:
  runs/<run_id>/packages/PACKAGE_MAP.md
  runs/<run_id>/packages/package_manifest.json
  runs/<run_id>/QA_REPORTS/package_segmentation_report.md

Per docs/PIPELINE_SPEC.md Stage 3 + Phase 3B spec from user prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now, read_json
from integrations.package_segmenter import (
    DEFAULT_MAX_PAGES_PER_PACKAGE,
    Package,
    SegmentationResult,
    package_to_manifest_entry,
    page_view_from_enriched,
    walk_pages,
)

PACKAGE_MANIFEST_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class SegmenterResult:
    manifest_path: Path
    map_path: Path
    report_path: Path
    package_count: int
    pages_total: int
    pages_in_packages: int
    pages_context_only: int
    pages_skipped: int
    coverage_ok: bool
    largest_package: int
    uncertain_packages: list  # list of package_ids with any uncertainty_notes
    deviated_from_hint: list  # list of dicts (package_id, expected, got)


# ----------------------------------------------------------- markdown views


def _build_package_map_md(
    *,
    run_id: str,
    source_file: str,
    enriched: dict,
    manifest: dict,
    seg: SegmentationResult,
) -> str:
    out = [
        f"# PACKAGE_MAP — {run_id}",
        "",
        "> Stage 3 (Phase 3B). تقسیم اسلاید‌ها به بسته‌های آموزشی برای تدریس مستقل.",
        f"> منبع: `{source_file}`",
        f"> پایه‌ی این تقسیم‌بندی: `classification/page_classification_enriched.json`",
        "> این فایل به‌صورت خودکار تولید می‌شود — قبل از Phase 4 توصیه می‌شود انسان آن را مرور کند.",
        "",
        "## ۱) خلاصه",
        "",
        f"- **تعداد کل صفحات**: {enriched['page_count']}",
        f"- **تعداد بسته‌ها**: {len(manifest['packages'])}",
        f"- **صفحات در بسته‌ها**: {sum(len(p['original_page_numbers']) for p in manifest['packages'])}",
        f"- **صفحات context_only**: {len(manifest.get('context_only_pages', []))}",
        f"- **صفحات skipped**: {len(manifest.get('skipped_pages', []))}",
        "",
        "## ۲) بسته‌ها",
        "",
    ]
    for pkg in manifest["packages"]:
        rng = pkg["page_range"]
        nums = pkg["original_page_numbers"]
        out.append(f"### {pkg['package_id']} — صفحات {rng[0]}–{rng[1]} ({len(nums)} صفحه)")
        out.append("")
        out.append(f"**عنوان**: {pkg['package_title']}")
        out.append("")
        out.append(f"**چرا این صفحات با هم؟** {pkg['reason_for_grouping']}")
        out.append("")
        out.append(f"**صفحات اصلی (anchor)**: " + ", ".join(f"page_{n:03d}" for n in nums))
        out.append("")
        out.append(f"**نقش‌های آموزشی صفحات**: " + ", ".join(pkg.get("page_roles", [])))
        out.append("")
        out.append("**مفاهیم اصلی**: " + ", ".join(pkg.get("main_concepts", []) or ["—"]))
        out.append("")
        if pkg.get("prerequisite_concepts"):
            out.append("**پیش‌نیازهای مفهومی**: " + ", ".join(pkg["prerequisite_concepts"]))
            out.append("")
        # what the student will learn — derived from main_concepts
        out.append("**دانشجو در این بسته یاد می‌گیرد**:")
        for c in (pkg.get("main_concepts") or [])[:5]:
            out.append(f"- {c}")
        out.append("")
        # what needs special explanation
        needs_bits: list = []
        if pkg.get("needs_deep_math_explanation"):
            needs_bits.append("توضیح عمیق ریاضی (Math/Notation sub-agent)")
        if pkg.get("needs_visual_explanation"):
            needs_bits.append("توضیح بصری (Figure/Plot sub-agent)")
        if pkg.get("needs_notation_explanation"):
            needs_bits.append("توضیح نمادگذاری")
        if pkg.get("exercise_or_question_presence"):
            needs_bits.append("ارائه‌ی راه‌حل تمرین")
        if needs_bits:
            out.append("**نیازمندی‌های توضیح خاص**:")
            for b in needs_bits:
                out.append(f"- {b}")
            out.append("")
        # difficulty + duration + confidence
        out.append(
            f"**سختی پیش‌بینی‌شده**: {pkg.get('expected_difficulty', '—')}  ·  "
            f"**مدت تقریبی تدریس**: {pkg.get('estimated_teaching_duration_minutes', '?')} دقیقه  ·  "
            f"**اعتماد**: {pkg.get('confidence', '—')}"
        )
        out.append("")
        if pkg.get("uncertainty_notes"):
            out.append("**هشدارها / عدم قطعیت‌ها**:")
            for u in pkg["uncertainty_notes"]:
                out.append(f"- {u}")
            out.append("")
        out.append(f"**پیشنهاد برای Phase 4**: {pkg.get('next_stage_recommendation', '—')}")
        out.append("")
        out.append("---")
        out.append("")

    # Full coverage table
    out.append("## ۳) جدول پوشش (page → package)")
    out.append("")
    out.append("| page_number | page_id | package_id | role |")
    out.append("|-------------|---------|------------|------|")
    page_to_pkg = {}
    for pkg in manifest["packages"]:
        for pid, num, role in zip(pkg["page_ids"], pkg["original_page_numbers"], pkg.get("page_roles", [])):
            page_to_pkg[num] = (pid, pkg["package_id"], role)
    for n in range(1, enriched["page_count"] + 1):
        if n in page_to_pkg:
            pid, pkid, role = page_to_pkg[n]
            out.append(f"| {n} | `{pid}` | `{pkid}` | {role} |")
        else:
            # check context_only / skipped
            target_pid = f"page_{n:03d}"
            if target_pid in manifest.get("context_only_pages", []):
                out.append(f"| {n} | `{target_pid}` | _context_only_ | — |")
            elif target_pid in manifest.get("skipped_pages", []):
                out.append(f"| {n} | `{target_pid}` | _skipped_ | — |")
            else:
                out.append(f"| {n} | `{target_pid}` | **MISSING** | — |")
    out.append("")

    # context_only / skipped
    if manifest.get("context_only_pages") or manifest.get("skipped_pages"):
        out.append("## ۴) صفحات context_only / skipped")
        out.append("")
        if manifest.get("context_only_pages"):
            out.append("**context_only**: " + ", ".join(manifest["context_only_pages"]))
            out.append("")
        if manifest.get("skipped_pages"):
            out.append("**skipped**: " + ", ".join(manifest["skipped_pages"]))
            out.append("")

    # recommended order for Phase 4
    out.append("## ۵) ترتیب پیشنهادی برای Phase 4 (تولید محتوای فارسی)")
    out.append("")
    out.append("ترتیب طبیعی همان ترتیب package_id است (1 → N). "
               "برای dependency بهتر، بسته‌هایی که `needs_deep_math_explanation=true` دارند "
               "می‌توانند با احتیاط بیشتر و sub-agent ریاضی اجرا شوند.")
    out.append("")
    out.append("| # | package_id | عنوان | روتین Phase 4 |")
    out.append("|---|------------|------|---------------|")
    for i, pkg in enumerate(manifest["packages"], start=1):
        out.append(f"| {i} | `{pkg['package_id']}` | {pkg['package_title']} | {pkg.get('next_stage_recommendation','—')} |")
    out.append("")

    # segmentation summary
    out.append("## ۶) خلاصه‌ی segmentation")
    out.append("")
    out.append(f"- بزرگ‌ترین بسته: {max((len(p['original_page_numbers']) for p in manifest['packages']), default=0)} صفحه")
    out.append(f"- کوچک‌ترین بسته: {min((len(p['original_page_numbers']) for p in manifest['packages']), default=0)} صفحه")
    avg = (sum(len(p['original_page_numbers']) for p in manifest['packages']) /
           max(len(manifest['packages']), 1))
    out.append(f"- میانگین: {avg:.1f} صفحه/بسته")
    out.append(f"- پوشش: {sum(len(p['original_page_numbers']) for p in manifest['packages']) + len(manifest.get('context_only_pages', [])) + len(manifest.get('skipped_pages', []))} / {enriched['page_count']}")
    out.append("")
    if seg.overrides:
        out.append("### override notes از walker")
        out.append("")
        for o in seg.overrides:
            out.append(f"- {o}")
        out.append("")
    return "\n".join(out)


def _build_qa_report(
    *,
    run_id: str,
    enriched: dict,
    manifest: dict,
    seg: SegmentationResult,
    result: SegmenterResult,
) -> str:
    out = [
        f"# Package Segmentation QA Report — {run_id}",
        "",
        "> Stage 3 (Phase 3B) QA. Generated automatically.",
        "",
        "## Summary",
        "",
        f"- **pages total**: {result.pages_total}",
        f"- **packages**: {result.package_count}",
        f"- **pages in packages**: {result.pages_in_packages}",
        f"- **pages context_only**: {result.pages_context_only}",
        f"- **pages skipped**: {result.pages_skipped}",
        f"- **coverage exhaustive**: {'pass' if result.coverage_ok else 'FAIL'}",
        f"- **largest package size**: {result.largest_package} "
        f"(default cap {DEFAULT_MAX_PAGES_PER_PACKAGE})",
        "",
        "## Per-package size",
        "",
        "| package_id | pages | size | role mix |",
        "|------------|-------|-----:|----------|",
    ]
    for pkg in manifest["packages"]:
        nums = pkg["original_page_numbers"]
        roles = " · ".join(pkg.get("page_roles", []) or ["—"])
        out.append(f"| `{pkg['package_id']}` | {nums[0]}–{nums[-1]} | {len(nums)} | {roles} |")

    out += [
        "",
        "## Constraints",
        "",
    ]
    # consecutiveness
    consec_ok = True
    for pkg in manifest["packages"]:
        ns = pkg["original_page_numbers"]
        if ns != list(range(ns[0], ns[-1] + 1)):
            consec_ok = False
            out.append(f"- `{pkg['package_id']}`: pages NOT consecutive → FAIL ({ns})")
    if consec_ok:
        out.append("- All packages consecutive: pass")

    # uniqueness
    flat = []
    for pkg in manifest["packages"]:
        flat.extend(pkg["original_page_numbers"])
    dup_check = "pass" if len(flat) == len(set(flat)) else "FAIL"
    out.append(f"- No duplicate page assignments: {dup_check}")

    # size cap
    over_cap = [
        pkg["package_id"] for pkg in manifest["packages"]
        if len(pkg["original_page_numbers"]) > DEFAULT_MAX_PAGES_PER_PACKAGE
    ]
    if over_cap:
        out.append(f"- Packages exceeding cap {DEFAULT_MAX_PAGES_PER_PACKAGE}: {over_cap} (justified in uncertainty_notes)")
    else:
        out.append(f"- No package exceeds {DEFAULT_MAX_PAGES_PER_PACKAGE}: pass")

    # required fields populated
    req_ok = all(pkg.get("reason_for_grouping") for pkg in manifest["packages"])
    out.append(f"- Every package has reason_for_grouping: {'pass' if req_ok else 'FAIL'}")
    conf_ok = all(pkg.get("confidence") in ("high", "medium", "low") for pkg in manifest["packages"])
    out.append(f"- Every package has confidence: {'pass' if conf_ok else 'FAIL'}")
    diff_ok = all(pkg.get("expected_difficulty") for pkg in manifest["packages"])
    out.append(f"- Every package has expected_difficulty: {'pass' if diff_ok else 'FAIL'}")

    # uncertain packages
    out += [
        "",
        "## Uncertain packages (have uncertainty_notes)",
        "",
    ]
    if not result.uncertain_packages:
        out.append("None.")
    else:
        for pid in result.uncertain_packages:
            out.append(f"- `{pid}`")

    # deviations from Phase 3A.1 hints
    out += [
        "",
        "## Deviations from Phase 3A.1 suggested groupings",
        "",
    ]
    if not result.deviated_from_hint:
        out.append("None — segmenter matched the hint table exactly.")
    else:
        for d in result.deviated_from_hint:
            out.append(f"- `{d['package_id']}`: expected pages {d['expected']}, got {d['got']}")

    out += [
        "",
        "## Acceptance checks (Phase 3B)",
        "",
        f"- PACKAGE_MAP.md exists: pass",
        f"- package_manifest.json exists: pass",
        f"- this QA report exists: pass",
        f"- coverage exhaustive: {'pass' if result.coverage_ok else 'FAIL'}",
        f"- all consecutive: {'pass' if consec_ok else 'FAIL'}",
        f"- no duplicates: {dup_check}",
        f"- all reason_for_grouping present: {'pass' if req_ok else 'FAIL'}",
        f"- schema-conformant (see CLI output): see scripts/segment_packages.py",
        "",
    ]
    return "\n".join(out)


# ----------------------------------------------------------------- entry


def run_segmentation(
    run_dir: Path | str,
    *,
    max_pages_per_package: int = DEFAULT_MAX_PAGES_PER_PACKAGE,
) -> SegmenterResult:
    """Segment a run's enriched classification into educational packages."""
    run_dir = Path(run_dir).resolve()
    enriched_path = run_dir / "classification" / "page_classification_enriched.json"
    if not enriched_path.exists():
        raise FileNotFoundError(
            "page_classification_enriched.json missing. Run Phase 3A.1 first: "
            "python scripts/review_low_confidence_pages.py --run <run_dir>"
        )

    enriched = read_json(enriched_path)
    pages = sorted(enriched["pages"], key=lambda p: p["page_number"])
    views = [page_view_from_enriched(p) for p in pages]

    hints = enriched.get("segmentation_hints") or {}
    hint_uncertain = set(hints.get("still_uncertain_pages") or [])
    suggested = hints.get("suggested_groupings") or []
    sugg_by_first_page: dict = {
        g["pages"][0]: g for g in suggested if g.get("pages")
    }
    natural_boundaries = list(hints.get("natural_boundaries_before_page") or [])

    seg = walk_pages(
        views,
        max_pages_per_package=max_pages_per_package,
        hints_uncertain=hint_uncertain,
        force_starts=natural_boundaries,
    )

    # Build manifest entries with prior-package context for prereqs.
    entries: list = []
    deviated: list = []
    closed_packages: list = []
    for pkg in seg.packages:
        first_page = pkg.pages[0].page_number
        sg = sugg_by_first_page.get(first_page)
        entry = package_to_manifest_entry(
            pkg,
            source_file=enriched.get("source_file"),
            prior_packages=closed_packages,
            suggested_grouping=sg,
            hints_uncertain=hint_uncertain,
            overrides_notes=seg.overrides,
        )
        # Apply Persian hint title if exact match — preserves the human-chosen wording.
        if sg and sg["pages"] == [p.page_number for p in pkg.pages]:
            hint_title = sg.get("title_hint_fa")
            if hint_title:
                entry["package_title"] = hint_title
        entries.append(entry)
        closed_packages.append(pkg)

        if sg and sg["pages"] != [p.page_number for p in pkg.pages]:
            deviated.append({
                "package_id": entry["package_id"],
                "expected": sg["pages"],
                "got": [p.page_number for p in pkg.pages],
            })

    manifest = {
        "schema_version": PACKAGE_MANIFEST_SCHEMA_VERSION,
        "run_id": enriched["run_id"],
        "source_file": enriched.get("source_file", ""),
        "packages": entries,
        "context_only_pages": seg.context_only_pages,
        "skipped_pages": seg.skipped_pages,
        "generated_at": iso_now(),
    }

    # Persist
    pkg_dir = run_dir / "packages"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "QA_REPORTS").mkdir(parents=True, exist_ok=True)

    manifest_path = pkg_dir / "package_manifest.json"
    atomic_write_json(manifest_path, manifest)

    # Coverage check
    flat_pages = []
    for e in entries:
        flat_pages.extend(e["original_page_numbers"])
    covered = set(flat_pages) | {int(pid.split("_")[1]) for pid in seg.context_only_pages} | {int(pid.split("_")[1]) for pid in seg.skipped_pages}
    all_pages = set(range(1, enriched["page_count"] + 1))
    coverage_ok = covered == all_pages

    largest_package = max((len(e["original_page_numbers"]) for e in entries), default=0)
    uncertain_pkg_ids = [e["package_id"] for e in entries if e.get("uncertainty_notes")]

    pre_result = SegmenterResult(
        manifest_path=manifest_path,
        map_path=pkg_dir / "PACKAGE_MAP.md",
        report_path=run_dir / "QA_REPORTS" / "package_segmentation_report.md",
        package_count=len(entries),
        pages_total=enriched["page_count"],
        pages_in_packages=len(flat_pages),
        pages_context_only=len(seg.context_only_pages),
        pages_skipped=len(seg.skipped_pages),
        coverage_ok=coverage_ok,
        largest_package=largest_package,
        uncertain_packages=uncertain_pkg_ids,
        deviated_from_hint=deviated,
    )

    pkg_map = _build_package_map_md(
        run_id=enriched["run_id"],
        source_file=enriched.get("source_file", ""),
        enriched=enriched,
        manifest=manifest,
        seg=seg,
    )
    atomic_write_text(pre_result.map_path, pkg_map)

    qa = _build_qa_report(
        run_id=enriched["run_id"],
        enriched=enriched,
        manifest=manifest,
        seg=seg,
        result=pre_result,
    )
    atomic_write_text(pre_result.report_path, qa)

    return pre_result
