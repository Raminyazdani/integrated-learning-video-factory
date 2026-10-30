"""llm_page_review_agent - generic page-review producer (LLM / manual-agent).

Produces `runs/<id>/classification/llm_page_review.json` (shape compatible with
authored_review.json, validated by schemas/page_review.schema.json) so that
`visual_classification_reviewer` can consume it for NON-file-1 inputs.

Source precedence per page (no file-1 leakage, no fabrication of specifics):
  1. manual-agent authored review (passed in `authored_reviews`)        -> CLAUDE_CODE_MANUAL_AGENT
  2. llm_client.vision_page_review(...) when available (None in manual/ -> LLM_PAID / LLM_DRY_RUN
     dry-run -> falls through)
  3. baseline auto-skeleton from the heuristic classification + page text -> BASELINE_AUTO
     (confidence=low, explicit uncertainty note: needs real review)

The agent itself contains NO input-specific data; per-input content lives in the
run artifact, exactly like file 3's authored content.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.visual_classification_reviewer import (  # noqa: E402
    _infer_role, _infer_boundary, _validate_enrichment, ALLOWED_PRIMARY_LABELS,
    _is_file1_run,
)


def _read_json(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _write_json(p: Path, obj: dict) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(str(tmp), str(p))


def _baseline_classification(run_dir: Path) -> dict:
    cdir = run_dir / "classification"
    for name in ("page_classification.json", "page_classification_enriched.json"):
        p = cdir / name
        if p.is_file():
            return _read_json(p)
    cands = sorted(cdir.glob("page_classification*.json"))
    if cands:
        return _read_json(cands[0])
    raise FileNotFoundError(f"no page_classification*.json in {cdir}")


def _page_text(run_dir: Path, page_id: str) -> str:
    p = run_dir / "extracted" / "page_text" / f"{page_id}.txt"
    if p.is_file():
        return " ".join(p.read_text(encoding="utf-8").split())
    return ""


def _baseline_entry(page: dict, page_text: str) -> dict:
    label = page.get("primary_label", "unknown")
    if label not in ALLOWED_PRIMARY_LABELS:
        label = "unknown"
    secs = [s for s in page.get("secondary_labels", []) if s in ALLOWED_PRIMARY_LABELS][:3]
    snippet = page_text[:90].strip()
    return {
        "enriched_primary_label": label,
        "enriched_secondary_labels": secs,
        "visual_summary_fa": ("خلاصهٔ خودکار از متن صفحه: " + snippet) if snippet else "متن قابل‌خواندن نبود؛ نیاز به بررسی.",
        "corrected_title_guess": None,
        "educational_role": _infer_role(label),
        "package_boundary_signal": _infer_boundary(page),
        "confidence_after_visual_review": "low",
        "reason_for_change": "baseline auto-skeleton (no LLM/vision or manual review yet)",
        "uncertainty_notes": ["auto-generated skeleton; needs LLM/vision or manual-agent review"],
    }


def build_page_review(run_dir, *, mode: str = None, llm_client=None,
                      authored_reviews: Optional[dict] = None) -> dict:
    """Build + write classification/llm_page_review.json. Returns the object."""
    run_dir = Path(run_dir).resolve()
    baseline = _baseline_classification(run_dir)

    if _is_file1_run(baseline):
        raise ValueError(
            "Refusing to build llm_page_review.json for the FILE-1 source: file 1 "
            "uses its gated _VISUAL_REVIEWS fixture, not this generic path."
        )

    try:
        from integrations.pipeline_mode import resolve_mode
        mode = resolve_mode(mode)
    except Exception:
        mode = mode or "manual_agent"

    authored_reviews = authored_reviews or {}
    pages = baseline.get("pages", [])
    reviews = {}
    used_authored = used_llm = used_baseline = 0

    for page in pages:
        pid = page["page_id"]
        entry = None
        # 1. manual-agent authored
        if pid in authored_reviews:
            entry = dict(authored_reviews[pid]); used_authored += 1
        # 2. LLM/vision (None in manual/dry-run skeleton)
        elif llm_client is not None:
            try:
                res = llm_client.vision_page_review(
                    image_path=run_dir / "extracted" / "pages" / f"{pid}.png",
                    page_text=_page_text(run_dir, pid), page_id=pid)
            except Exception:
                res = None
            if res:
                entry = dict(res); used_llm += 1
        # 3. baseline skeleton
        if entry is None:
            entry = _baseline_entry(page, _page_text(run_dir, pid)); used_baseline += 1
        # validate; if invalid, fall back to baseline skeleton
        check = dict(entry); check.setdefault("page_id", pid)
        if _validate_enrichment(check):
            entry = _baseline_entry(page, _page_text(run_dir, pid));
        reviews[pid] = entry

    if used_authored:
        executor = "CLAUDE_CODE_MANUAL_AGENT"
    elif used_llm and mode == "llm_paid":
        executor = "LLM_PAID"
    elif used_llm:
        executor = "LLM_DRY_RUN"
    else:
        executor = "BASELINE_AUTO"

    obj = {
        "run_id": baseline.get("run_id"),
        "source_file": baseline.get("source_file"),
        "executor": executor,
        "pipeline_mode": mode,
        "prompt_version": "v1",
        "reviews": reviews,
        "segmentation_hints": {},
        "low_confidence_pages": [p["page_number"] for p in pages
                                 if reviews[p["page_id"]]["confidence_after_visual_review"] == "low"],
        "notes": [
            f"authored={used_authored} llm={used_llm} baseline_skeleton={used_baseline}",
            "No file-1 fixtures used. No fabrication: unreviewed pages carry uncertainty notes.",
        ],
    }
    _write_json(run_dir / "classification" / "llm_page_review.json", obj)
    return obj
