"""package_metadata_agent - meaningful package titles + metadata (LLM / manual-agent).

Replaces weak header-derived titles (e.g. "Bibliography") with topic-based Persian
metadata derived from the package's PAGE REVIEWS (classification/llm_page_review.json
or authored_review.json) — NOT from clean_package_manifest.py's file-1 curated data.

Per-package source precedence (no file-1/file-3 leakage, no fabrication):
  1. manual-agent authored metadata (passed in `authored_metadata`)   -> CLAUDE_CODE_MANUAL_AGENT
  2. llm_client.package_titles(...) when available (None in manual/    -> LLM_PAID / LLM_DRY_RUN
     dry-run)
  3. baseline derived from page reviews (title from the start page's    -> BASELINE_AUTO
     corrected_title_guess; concepts from review labels)

Writes packages/package_metadata_llm.json and ENHANCES packages/package_manifest.json
(package_title, teaching_goal_fa, main_concepts, needs_formula_explanation,
needs_visual_explanation, expected_difficulty, uncertainty_notes). Package-item
schema allows additionalProperties, so enrichment is schema-safe.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_HEADER_JUNK = {"bibliography", "introduction", "outline", "appendix", "summary",
                "formula", "mixed", "unknown", "context_only"}
_FORMULA_LABELS = {"formula", "derivation", "theorem"}
_FIGURE_LABELS = {"plot_or_figure"}


def _read(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _write(p: Path, obj: dict) -> None:
    p = Path(p)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(p))


def _load_reviews(run_dir: Path) -> dict:
    for name in ("llm_page_review.json", "authored_review.json"):
        p = run_dir / "classification" / name
        if p.is_file():
            return _read(p).get("reviews", {}) or {}
    return {}


def _is_file1(manifest: dict) -> bool:
    sf = (manifest.get("source_file") or "").replace("\\", "/").lower().rsplit("/", 1)[-1]
    return sf in {"1.pdf", "l01_-_introduction_to_ml.pdf"}


def _derive(pkg: dict, reviews: dict) -> dict:
    pids = pkg.get("page_ids", [])
    revs = [reviews[pid] for pid in pids if pid in reviews]
    # title from the first non-context page's corrected_title_guess
    title = None
    for r in revs:
        if r.get("package_boundary_signal") != "context_only" and r.get("corrected_title_guess"):
            title = r["corrected_title_guess"]; break
    if not title:
        title = (revs[0].get("corrected_title_guess") if revs else None) or "بستهٔ آموزشی"
    concepts = []
    for r in revs:
        g = r.get("corrected_title_guess")
        if g and g not in concepts:
            concepts.append(g)
    concepts = concepts[:6]
    needs_formula = any(
        (r.get("enriched_primary_label") in _FORMULA_LABELS)
        or any(s in _FORMULA_LABELS for s in r.get("enriched_secondary_labels", []))
        or r.get("educational_role") == "formula_focus"
        for r in revs)
    needs_visual = any(
        (r.get("enriched_primary_label") in _FIGURE_LABELS)
        or any(s in _FIGURE_LABELS for s in r.get("enriched_secondary_labels", []))
        or r.get("educational_role") in ("visual_diagram", "example_walkthrough")
        for r in revs)
    unc = []
    for r in revs:
        for u in r.get("uncertainty_notes", []):
            if u not in unc:
                unc.append(u)
    return {
        "package_title": title,
        "teaching_goal_fa": "هدف: درک مفاهیم این بسته بر اساس صفحات منبع (مشتق‌شده از مرور صفحات).",
        "main_concepts": concepts or ["(نیاز به بررسی)"],
        "expected_difficulty": pkg.get("expected_difficulty", "medium"),
        "needs_formula_explanation": bool(needs_formula),
        "needs_visual_explanation": bool(needs_visual),
        "uncertainty_notes": unc,
    }


def build_package_metadata(run_dir, *, mode: str = None, llm_client=None,
                           authored_metadata: Optional[dict] = None) -> dict:
    run_dir = Path(run_dir).resolve()
    mani_path = run_dir / "packages" / "package_manifest.json"
    manifest = _read(mani_path)
    if _is_file1(manifest):
        raise ValueError("Refusing: file-1 uses curated titles, not this generic path.")
    try:
        from integrations.pipeline_mode import resolve_mode
        mode = resolve_mode(mode)
    except Exception:
        mode = mode or "manual_agent"

    reviews = _load_reviews(run_dir)
    authored_metadata = authored_metadata or {}
    used_authored = used_llm = used_baseline = 0
    out_meta = {}

    for pkg in manifest.get("packages", []):
        pid = pkg["package_id"]
        meta = None
        if pid in authored_metadata:
            meta = dict(authored_metadata[pid]); used_authored += 1
        elif llm_client is not None:
            try:
                res = llm_client.package_titles(package_id=pid, evidence={
                    "page_ids": pkg.get("page_ids"),
                    "reviews": {p: reviews.get(p) for p in pkg.get("page_ids", [])}})
            except Exception:
                res = None
            if res:
                meta = dict(res); used_llm += 1
        if meta is None:
            meta = _derive(pkg, reviews); used_baseline += 1
        out_meta[pid] = meta

        # enrich manifest (schema-safe: package items allow additionalProperties)
        pkg["package_title"] = meta["package_title"]
        pkg["teaching_goal_fa"] = meta.get("teaching_goal_fa", "")
        pkg["main_concepts"] = meta.get("main_concepts", [])
        pkg["needs_formula_explanation"] = bool(meta.get("needs_formula_explanation", False))
        pkg["needs_visual_explanation"] = bool(meta.get("needs_visual_explanation", False))
        if meta.get("expected_difficulty"):
            pkg["expected_difficulty"] = meta["expected_difficulty"]
        pkg["uncertainty_notes"] = meta.get("uncertainty_notes", [])

    _write(mani_path, manifest)

    executor = ("CLAUDE_CODE_MANUAL_AGENT" if used_authored else
                ("LLM_PAID" if (used_llm and mode == "llm_paid") else
                 ("LLM_DRY_RUN" if used_llm else "BASELINE_AUTO")))
    obj = {
        "run_id": manifest.get("run_id"),
        "source_file": manifest.get("source_file"),
        "executor": executor,
        "pipeline_mode": mode,
        "prompt_version": "v1",
        "packages": out_meta,
        "notes": [f"authored={used_authored} llm={used_llm} baseline={used_baseline}",
                  "No file-1 curated titles used; derived from page reviews of this run."],
    }
    _write(run_dir / "packages" / "package_metadata_llm.json", obj)
    return obj
