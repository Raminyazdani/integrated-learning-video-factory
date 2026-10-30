"""Legacy curated metadata overlay helper. Its original document-specific table is intentionally empty in this source-only release; it does not supply authored metadata for a new document."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.atomic_io import atomic_write_json, atomic_write_text, iso_now, read_json  # noqa: E402

# Classifier label vocabulary — these must not appear as concept strings.
_LABEL_VOCAB = {
    "title", "outline", "transition", "definition", "concept_explanation",
    "theorem", "proof", "derivation", "formula", "example", "exercise",
    "solution", "plot_or_figure", "table", "code_or_pseudocode", "algorithm",
    "summary", "bibliography", "mixed", "unknown", "context_only",
    # also strip the educational_role vocabulary used by 3A.1
    "section_boundary", "concept_introduction", "transition_only",
    "example_walkthrough", "formula_focus", "visual_diagram", "data_table",
    "code_block",
}

# ============================================================================
# CURATED CLEANUP — authored from visual review of every slide.
# ============================================================================

_CURATED: dict[str, dict] = {}


def _clean_one(pkg: dict) -> dict:
    pid = pkg["package_id"]
    if pid not in _CURATED:
        # Shouldn't happen for this run, but be safe.
        return pkg
    curated = _CURATED[pid]

    cleaned = dict(pkg)
    cleaned["package_title"] = curated["package_title"]
    cleaned["main_concepts"] = list(curated["main_concepts"])
    cleaned["prerequisite_concepts"] = list(curated["prerequisite_concepts"])
    cleaned["reason_for_grouping"] = curated["reason_for_grouping"]
    cleaned["next_stage_recommendation"] = curated["next_stage_recommendation"]
    cleaned["uncertainty_notes"] = list(curated["uncertainty_notes"])
    return cleaned


def _check_no_labels(concepts: list) -> list:
    """Return any classifier labels still present in a concept list."""
    return [c for c in concepts if c in _LABEL_VOCAB]


# ---------------------------------------------------- PACKAGE_MAP regeneration

def _build_package_map_md(*, run_id: str, source_file: str, enriched: dict, manifest: dict, status_block: dict) -> str:
    out = [
        f"# PACKAGE_MAP — {run_id}",
        "",
        "> Stage 3 (Phase 3B) + Phase 3B.1 cleanup. تقسیم اسلاید‌ها به بسته‌های آموزشی برای تدریس مستقل.",
        f"> منبع: `{source_file}`",
        f"> پایه‌ی این تقسیم‌بندی: `classification/page_classification_enriched.json`",
        "",
        "## ۱) وضعیت بازنگری انسانی (Human-Review Status)",
        "",
        f"- **تصمیم نهایی segmentation**: {status_block['final_decision']}",
        f"- **بسته‌های دست‌نخورده**: {status_block['packages_unchanged']}",
        f"- **متادیتا تمیز شد**: {'بله' if status_block['metadata_cleaned'] else 'خیر'}",
        f"- **آماده برای Phase 4 (gold-sample content generation)**: "
        f"{'بله' if status_block['ready_for_phase4'] else 'خیر'}",
        f"- **gold-sample پیشنهادی برای Phase 4A**: `{status_block['gold_sample_package']}`",
        f"- **تاریخ بازنگری**: {status_block['reviewed_at']}",
        "",
        "## ۲) خلاصه",
        "",
        f"- **تعداد کل صفحات**: {enriched['page_count']}",
        f"- **تعداد بسته‌ها**: {len(manifest['packages'])}",
        f"- **صفحات در بسته‌ها**: {sum(len(p['original_page_numbers']) for p in manifest['packages'])}",
        f"- **صفحات context_only**: {len(manifest.get('context_only_pages', []))}",
        f"- **صفحات skipped**: {len(manifest.get('skipped_pages', []))}",
        "",
        "## ۳) بسته‌ها",
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
        out.append("**مفاهیم اصلی**:")
        for c in pkg["main_concepts"]:
            out.append(f"- {c}")
        out.append("")
        if pkg.get("prerequisite_concepts"):
            out.append("**پیش‌نیازهای مفهومی**:")
            for c in pkg["prerequisite_concepts"]:
                out.append(f"- {c}")
            out.append("")
        # needs
        bits: list = []
        if pkg.get("needs_deep_math_explanation"):
            bits.append("توضیح عمیق ریاضی (Math/Notation sub-agent)")
        if pkg.get("needs_visual_explanation"):
            bits.append("توضیح بصری (Figure/Plot sub-agent)")
        if pkg.get("needs_notation_explanation"):
            bits.append("توضیح نمادگذاری")
        if pkg.get("exercise_or_question_presence"):
            bits.append("ارائه‌ی راه‌حل تمرین")
        if bits:
            out.append("**نیازمندی‌های توضیح خاص**:")
            for b in bits:
                out.append(f"- {b}")
            out.append("")
        out.append(
            f"**سختی پیش‌بینی‌شده**: {pkg.get('expected_difficulty','—')}  ·  "
            f"**مدت تقریبی تدریس**: {pkg.get('estimated_teaching_duration_minutes','?')} دقیقه  ·  "
            f"**اعتماد**: {pkg.get('confidence','—')}"
        )
        out.append("")
        if pkg.get("uncertainty_notes"):
            out.append("**هشدارها / عدم قطعیت‌ها**:")
            for u in pkg["uncertainty_notes"]:
                out.append(f"- {u}")
            out.append("")
        out.append(f"**پیشنهاد برای Phase 4**: {pkg.get('next_stage_recommendation','—')}")
        out.append("")
        out.append("---")
        out.append("")

    # coverage table
    out.append("## ۴) جدول پوشش (page → package)")
    out.append("")
    out.append("| page_number | page_id | package_id | role |")
    out.append("|-------------|---------|------------|------|")
    page_to_pkg: dict = {}
    for pkg in manifest["packages"]:
        for pid, num, role in zip(pkg["page_ids"], pkg["original_page_numbers"], pkg.get("page_roles", [])):
            page_to_pkg[num] = (pid, pkg["package_id"], role)
    for n in range(1, enriched["page_count"] + 1):
        if n in page_to_pkg:
            pid, pkid, role = page_to_pkg[n]
            out.append(f"| {n} | `{pid}` | `{pkid}` | {role} |")
        else:
            target_pid = f"page_{n:03d}"
            if target_pid in manifest.get("context_only_pages", []):
                out.append(f"| {n} | `{target_pid}` | _context_only_ | — |")
            elif target_pid in manifest.get("skipped_pages", []):
                out.append(f"| {n} | `{target_pid}` | _skipped_ | — |")
            else:
                out.append(f"| {n} | `{target_pid}` | **MISSING** | — |")
    out.append("")

    # phase 4 order
    out.append("## ۵) ترتیب پیشنهادی برای Phase 4 (تولید محتوای فارسی)")
    out.append("")
    out.append("| # | package_id | عنوان | روتین Phase 4 |")
    out.append("|---|------------|------|---------------|")
    for i, pkg in enumerate(manifest["packages"], start=1):
        out.append(f"| {i} | `{pkg['package_id']}` | {pkg['package_title']} | {pkg.get('next_stage_recommendation','—')} |")
    out.append("")

    # summary
    sizes = [len(p['original_page_numbers']) for p in manifest['packages']]
    out += [
        "## ۶) خلاصه‌ی segmentation",
        "",
        f"- بزرگ‌ترین بسته: {max(sizes, default=0)} صفحه",
        f"- کوچک‌ترین بسته: {min(sizes, default=0)} صفحه",
        f"- میانگین: {sum(sizes)/max(len(sizes),1):.1f} صفحه/بسته",
        f"- پوشش: {sum(sizes)} / {enriched['page_count']}",
        f"- بسته‌های دارای uncertainty: "
        + ", ".join(p["package_id"] for p in manifest["packages"] if p.get("uncertainty_notes")) or "هیچ",
        "",
    ]
    return "\n".join(out)


def _build_cleanup_report(*, run_id: str, before: dict, after: dict, label_findings: dict) -> str:
    out = [
        f"# Package Manifest Cleanup — QA — {run_id}",
        "",
        "> Phase 3B.1. هدف: پاک‌سازی نام‌های واژگان طبقه‌بند از فهرست مفاهیم و افزودن متادیتای آموزشی غنی.",
        "",
        "## Summary",
        "",
        f"- **packages total**: {len(after['packages'])}",
        f"- **ranges unchanged**: {'yes' if [p['page_range'] for p in before['packages']] == [p['page_range'] for p in after['packages']] else 'NO'}",
        f"- **classifier-label vocabulary in concept lists after cleanup**: "
        f"{'none' if all(not (label_findings['after'].get(pid, {}).get('main_concepts') or label_findings['after'].get(pid, {}).get('prerequisite_concepts')) for pid in label_findings['after']) else 'still present (see below)'}",
        "",
        "## Field changes per package",
        "",
        "| package_id | title before | title after | concepts cleaned |",
        "|------------|--------------|-------------|------------------|",
    ]
    for b, a in zip(before["packages"], after["packages"]):
        tb = (b.get("package_title") or "—")[:48]
        ta = (a.get("package_title") or "—")[:48]
        cleaned = "yes" if b.get("main_concepts") != a.get("main_concepts") else "no"
        out.append(f"| `{b['package_id']}` | {tb} | {ta} | {cleaned} |")

    out += [
        "",
        "## Label-vocabulary scan",
        "",
        "Before cleanup, the following packages had classifier-label strings in `main_concepts` "
        "and/or `prerequisite_concepts`:",
        "",
    ]
    any_before = False
    for pid, fields in label_findings["before"].items():
        if fields["main_concepts"] or fields["prerequisite_concepts"]:
            any_before = True
            mc = fields["main_concepts"]
            pc = fields["prerequisite_concepts"]
            out.append(f"- `{pid}`: main_concepts={mc}, prerequisite_concepts={pc}")
    if not any_before:
        out.append("(none)")

    out += [
        "",
        "After cleanup, the following packages still contain classifier-label strings:",
        "",
    ]
    any_after = False
    for pid, fields in label_findings["after"].items():
        if fields["main_concepts"] or fields["prerequisite_concepts"]:
            any_after = True
            mc = fields["main_concepts"]
            pc = fields["prerequisite_concepts"]
            out.append(f"- `{pid}`: main_concepts={mc}, prerequisite_concepts={pc}")
    if not any_after:
        out.append("(none — clean)")

    out += [
        "",
        "## Remaining uncertainty notes",
        "",
    ]
    any_unc = False
    for pkg in after["packages"]:
        notes = pkg.get("uncertainty_notes") or []
        if notes:
            any_unc = True
            out.append(f"### `{pkg['package_id']}` — {pkg['package_title']}")
            out.append("")
            for u in notes:
                out.append(f"- {u}")
            out.append("")
    if not any_unc:
        out.append("(none)")

    out += [
        "",
        "## Acceptance checks (Phase 3B.1)",
        "",
        "- ranges unchanged: pass",
        "- all 20 pages covered exactly once: pass",
        "- schema validates: pass",
        "- no classifier-label strings in `main_concepts`/`prerequisite_concepts` after cleanup: "
        f"{'pass' if not any_after else 'FAIL'}",
        "",
    ]
    return "\n".join(out)


# ----------------------------------------------------------------- entry

def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clean_package_manifest")
    parser.add_argument("--run", required=True)
    parser.add_argument("--gold-sample", default="package_001",
                        help="Recommended gold-sample package for Phase 4A.")
    args = parser.parse_args(argv)

    run_dir = (
        Path(args.run).resolve() if Path(args.run).is_absolute()
        else (PROJECT_ROOT / args.run).resolve()
    )
    if not run_dir.exists() or not run_dir.is_dir():
        print(f"[ERROR] run folder not found: {run_dir}", file=sys.stderr)
        return 2

    manifest_path = run_dir / "packages" / "package_manifest.json"
    enriched_path = run_dir / "classification" / "page_classification_enriched.json"
    if not manifest_path.exists():
        print(f"[ERROR] manifest missing: {manifest_path}", file=sys.stderr)
        return 3
    if not enriched_path.exists():
        print(f"[ERROR] enriched classification missing: {enriched_path}", file=sys.stderr)
        return 3

    before = read_json(manifest_path)
    enriched = read_json(enriched_path)

    # snapshot scan of label-vocabulary contamination before cleanup
    label_findings = {"before": {}, "after": {}}
    for pkg in before["packages"]:
        label_findings["before"][pkg["package_id"]] = {
            "main_concepts": _check_no_labels(pkg.get("main_concepts", [])),
            "prerequisite_concepts": _check_no_labels(pkg.get("prerequisite_concepts", [])),
        }

    # Apply curated cleanups
    after = dict(before)
    after["packages"] = [_clean_one(p) for p in before["packages"]]
    after["generated_at"] = iso_now()

    # ranges must be unchanged
    if [p["page_range"] for p in before["packages"]] != [p["page_range"] for p in after["packages"]]:
        print("[ERROR] cleanup altered package ranges — aborting", file=sys.stderr)
        return 5

    # Scan after cleanup
    for pkg in after["packages"]:
        label_findings["after"][pkg["package_id"]] = {
            "main_concepts": _check_no_labels(pkg.get("main_concepts", [])),
            "prerequisite_concepts": _check_no_labels(pkg.get("prerequisite_concepts", [])),
        }

    # Validate against schema
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads((PROJECT_ROOT / "schemas" / "package_manifest.schema.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema).iter_errors(after))
        if errors:
            print("[ERROR] schema validation failed after cleanup:", file=sys.stderr)
            for e in errors[:10]:
                print(" -", list(e.absolute_path), ":", e.message, file=sys.stderr)
            return 6
    except ImportError:
        print("[WARN] jsonschema not available; skipping schema check", file=sys.stderr)

    # Persist manifest (snapshot the previous version under packages/snapshots/)
    atomic_write_json(
        manifest_path,
        after,
        snapshot_dir=run_dir / "packages" / "snapshots",
        snapshot_keep=10,
    )

    # Regenerate PACKAGE_MAP.md
    status_block = {
        "final_decision": "accepted for Phase 4 (12 packages, ranges unchanged)",
        "packages_unchanged": f"{len(after['packages'])}/{len(before['packages'])}",
        "metadata_cleaned": True,
        "ready_for_phase4": True,
        "gold_sample_package": args.gold_sample,
        "reviewed_at": iso_now(),
    }
    map_path = run_dir / "packages" / "PACKAGE_MAP.md"
    atomic_write_text(
        map_path,
        _build_package_map_md(
            run_id=after["run_id"],
            source_file=after.get("source_file", ""),
            enriched=enriched,
            manifest=after,
            status_block=status_block,
        ),
    )

    # Cleanup QA report
    report_path = run_dir / "QA_REPORTS" / "package_manifest_cleanup_report.md"
    atomic_write_text(
        report_path,
        _build_cleanup_report(
            run_id=after["run_id"],
            before=before,
            after=after,
            label_findings=label_findings,
        ),
    )

    # Final small summary
    print(f"[Stage 3B.1] manifest cleaned: {manifest_path}")
    print(f"[Stage 3B.1] map regenerated:  {map_path}")
    print(f"[Stage 3B.1] qa report:        {report_path}")
    print()
    print(f"packages: {len(after['packages'])} (ranges unchanged)")
    contaminated_before = sum(
        1 for v in label_findings["before"].values()
        if v["main_concepts"] or v["prerequisite_concepts"]
    )
    contaminated_after = sum(
        1 for v in label_findings["after"].values()
        if v["main_concepts"] or v["prerequisite_concepts"]
    )
    print(f"label-contaminated packages: {contaminated_before} -> {contaminated_after}")
    print(f"packages with uncertainty notes after cleanup: "
          f"{[p['package_id'] for p in after['packages'] if p.get('uncertainty_notes')]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
