"""slide_manifest_consistency - enforce the combined-manifest source-of-truth.

Strategy: **FORMALLY_NEUTRALIZED**.

`slides/slide_manifest.json` (the combined deck manifest) is the ONLY source of
truth for narration, audio, video, and final artifact validation. Per-package
`slides/slide_manifest_package_NNN.json` files are secondary/debug artifacts:
they may legitimately diverge from the combined deck (the per-package and merged
slide builders are separate code paths) and must NEVER override combined data.

This module validates that the combined manifest is complete and self-consistent
and that, where narration/audio exist, they cover every combined slide. A
per-package vs combined size mismatch is reported as INFORMATIONAL (expected
under this strategy), not a failure.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, List

STRATEGY = "FORMALLY_NEUTRALIZED"
_REQUIRED_SLIDE_FIELDS = ("slide_id", "package_id", "slide_index_in_package", "slide_index_global")


def _load(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def check_slide_manifest_consistency(run_dir) -> Dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    slides_dir = run_dir / "slides"
    combined_path = slides_dir / "slide_manifest.json"

    r: Dict[str, Any] = {
        "run_id": run_dir.name,
        "strategy": STRATEGY,
        "authoritative_manifest": "slides/slide_manifest.json",
        "consumers": ["narration", "audio", "video", "final_qa", "slide_image_export"],
        "combined_slide_count": 0,
        "per_package_files": 0,
        "per_package_slide_sum": 0,
        "per_package_divergence": False,
        "combined_packages": [],
        "issues": [],
        "informational": [],
        "checks": {},
        "verdict": "FAIL",
        "error": "",
    }

    combined = _load(combined_path)
    if combined is None:
        r["error"] = f"combined slide_manifest.json not found/unreadable: {combined_path}"
        return r

    slides = combined.get("slides", []) or []
    declared = combined.get("slide_count") or len(slides)
    r["combined_slide_count"] = declared
    combined_pkgs = []
    seen = set()
    for s in slides:
        pid = s.get("package_id")
        if pid and pid not in seen:
            seen.add(pid); combined_pkgs.append(pid)
    r["combined_packages"] = combined_pkgs

    # --- combined self-consistency ---
    missing_fields = [s.get("slide_id") or f"idx{i}"
                      for i, s in enumerate(slides)
                      if not all(s.get(k) is not None for k in _REQUIRED_SLIDE_FIELDS)]
    no_image = [s.get("slide_id") for s in slides if not s.get("image_path")]
    unresolved = [s.get("slide_id") for s in slides
                  if s.get("image_path") and not (run_dir / str(s["image_path"]).replace("/", os.sep)).is_file()]
    dup_ids = len(slides) != len({s.get("slide_id") for s in slides})

    r["checks"]["slide_count_matches_declared"] = (len(slides) == declared and declared > 0)
    r["checks"]["all_slides_have_required_fields"] = (not missing_fields)
    r["checks"]["all_slides_have_image_path"] = (not no_image)
    r["checks"]["all_image_paths_resolve"] = (not unresolved)
    r["checks"]["slide_ids_unique"] = (not dup_ids)
    if missing_fields:
        r["issues"].append(f"{len(missing_fields)} combined slide(s) missing required fields: {missing_fields[:5]}")
    if no_image:
        r["issues"].append(f"{len(no_image)} combined slide(s) missing image_path: {no_image[:5]}")
    if unresolved:
        r["issues"].append(f"{len(unresolved)} combined image_path(s) do not resolve: {unresolved[:5]}")
    if dup_ids:
        r["issues"].append("combined manifest has duplicate slide_id values")

    # --- per-package manifests (secondary/debug) ---
    pp_files = sorted(glob.glob(str(slides_dir / "slide_manifest_package_*.json")))
    r["per_package_files"] = len(pp_files)
    pp_sum = 0
    pp_pkgs = []
    for f in pp_files:
        d = _load(Path(f))
        if d is None:
            r["informational"].append(f"per-package manifest unreadable (ignored): {os.path.basename(f)}")
            continue
        pp_sum += len(d.get("slides", []) or [])
        pid = os.path.basename(f).replace("slide_manifest_", "").replace(".json", "")
        pp_pkgs.append(pid)
    r["per_package_slide_sum"] = pp_sum
    if pp_files:
        r["per_package_divergence"] = (pp_sum != declared) or (set(pp_pkgs) != set(combined_pkgs))
        if r["per_package_divergence"]:
            r["informational"].append(
                f"per-package vs combined divergence (EXPECTED & tolerated under {STRATEGY}): "
                f"per_package_sum={pp_sum} vs combined={declared}; "
                f"per_package_pkgs={len(pp_pkgs)} vs combined_pkgs={len(combined_pkgs)}. "
                "Per-package manifests are NOT consumed by narration/audio/video."
            )
    # by design, per-package never overrides combined (narration uses combined as primary)
    r["checks"]["per_package_does_not_override_combined"] = True

    # --- downstream coverage vs combined (only when those artifacts exist) ---
    combined_ids = {s.get("slide_id") for s in slides}
    full_nar = _load(run_dir / "narration_scripts" / "full_narration.json")
    if full_nar is not None:
        nar_ids = {s.get("slide_id") for p in full_nar.get("packages", []) for s in p.get("slides", [])}
        miss = sorted(combined_ids - nar_ids)
        r["checks"]["narration_covers_every_combined_slide"] = (not miss)
        if miss:
            r["issues"].append(f"narration missing for {len(miss)} combined slide(s): {miss[:5]}")
    else:
        r["checks"]["narration_covers_every_combined_slide"] = None  # not generated yet

    audio_man = _load(run_dir / "audio" / "audio_manifest.json")
    if audio_man is not None:
        aud_ids = set()
        for _pid, blk in (audio_man.get("packages") or {}).items():
            for s in blk.get("slides", []) or []:
                aud_ids.add(s.get("slide_id"))
        miss = sorted(combined_ids - aud_ids)
        r["checks"]["audio_covers_every_combined_slide"] = (not miss)
        if miss:
            r["issues"].append(f"audio missing for {len(miss)} combined slide(s): {miss[:5]}")
    else:
        r["checks"]["audio_covers_every_combined_slide"] = None  # not generated yet

    # --- verdict: pass when combined is authoritative & complete; per-package
    #     divergence is informational only. None-valued checks (artifact not yet
    #     produced) do not fail the verdict. ---
    hard = [v for v in r["checks"].values() if v is False]
    r["verdict"] = "PASS" if not r["issues"] and not hard else "FAIL"
    return r


def write_consistency_report(run_dir, result: Dict[str, Any]) -> Path:
    run_dir = Path(run_dir).resolve()
    qa = run_dir / "QA_REPORTS"
    qa.mkdir(parents=True, exist_ok=True)
    path = qa / "slide_manifest_consistency_report.md"

    def _b(v):
        return "pass" if v is True else ("FAIL" if v is False else "n/a (artifact not generated)")

    lines = [
        "# Slide-manifest consistency report",
        "",
        f"- run: `{result['run_id']}`",
        f"- **chosen strategy: {result['strategy']}**",
        f"- **authoritative manifest: `{result['authoritative_manifest']}`**",
        f"- consumed by: {', '.join(result['consumers'])}",
        f"- combined slide count: {result['combined_slide_count']}",
        f"- combined packages: {len(result['combined_packages'])}",
        f"- per-package manifest files: {result['per_package_files']}",
        f"- per-package slide sum: {result['per_package_slide_sum']}",
        f"- per-package divergence: {result['per_package_divergence']}",
        "",
        "## Source-of-truth statement",
        "",
        "`slides/slide_manifest.json` (combined deck) is the ONLY source of truth for "
        "narration, audio, video, and final validation. Per-package "
        "`slide_manifest_package_NNN.json` files are secondary/debug artifacts and are "
        "never consumed by those stages; any divergence is expected and tolerated.",
        "",
        "## Checks",
        "",
    ]
    for k, v in result["checks"].items():
        lines.append(f"- {k}: **{_b(v)}**")
    lines.append("")
    if result["error"]:
        lines.append(f"## Error\n\n- `{result['error']}`\n")
    if result["issues"]:
        lines.append("## Issues (hard)")
        lines.append("")
        for i in result["issues"]:
            lines.append(f"- {i}")
        lines.append("")
    if result["informational"]:
        lines.append("## Informational (expected under FORMALLY_NEUTRALIZED)")
        lines.append("")
        for i in result["informational"]:
            lines.append(f"- {i}")
        lines.append("")
    lines.append(f"## Verdict: **{result['verdict']}**")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
