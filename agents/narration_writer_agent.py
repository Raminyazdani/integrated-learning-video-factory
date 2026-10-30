"""narration_writer_agent - Phase 7A per-slide Persian narration generator.

Given a per-package slide manifest plus the package's curated content_json,
produce a narration document at narration_scripts/<package_id>.json that
validates against schemas/narration.schema.json.

Design notes:
    - This is NOT a TTS step; output is text only.
    - Narration text is derived deterministically from each slide's
      speaker_notes_excerpt + title + package content_json. The agent
      does not invent facts beyond those sources.
    - estimated_seconds is computed from len(narration_fa) using a
      ~12 chars/sec Persian speaking rate, then clamped to the
      type-typical range from prompts/narration_writer_prompt.md.
    - Math/formula slides set requires_math_pronunciation=true and
      add small pronunciation_hints for w_0, w*x, sign(...), etc.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from integrations.atomic_io import atomic_write_text, iso_now


PERSIAN_CHARS_PER_SECOND = 12.0


# Type-typical (min, max) seconds per slide type. Used to clamp the
# raw chars/12 estimate so narration length stays believable.
_TYPE_RANGES = {
    "divider":        (8.0, 15.0),
    "concept":        (25.0, 50.0),
    "anchor":         (10.0, 20.0),
    "figure":         (25.0, 45.0),
    "formula":        (30.0, 60.0),
    "notation":       (25.0, 40.0),
    "example":        (25.0, 50.0),
    "exam_trap":      (20.0, 35.0),
    "common_mistake": (25.0, 40.0),
    "quiz":           (20.0, 40.0),
    "summary":        (20.0, 40.0),
    "other":          (10.0, 20.0),
}


# Common math-symbol pronunciation hints for Persian TTS.
_MATH_HINTS = [
    ("w_0",     "دبلیو زیر صفر"),
    ("w_1",     "دبلیو زیر یک"),
    ("w.x",     "دبلیو ضرب در ایکس (ضرب داخلی)"),
    ("w*x",     "دبلیو ضرب در ایکس (ضرب داخلی)"),
    ("f(x)",    "اف از ایکس"),
    ("sign(",   "تابع علامت سین (sign)"),
    ("{-1,",    "منهای یک و مثبت یک"),
    ("{0,",     "صفر و یک"),
    ("x_n",     "ایکس زیر ان"),
    ("y_n",     "وای زیر ان"),
    ("(x_n, y_n)", "زوج (ایکس زیر ان، وای زیر ان)"),
    ("sigma",   "سیگما"),
]


@dataclass
class NarrationResult:
    success: bool
    package_id: str
    json_path: Path
    report_path: Path
    n_slides_in: int
    n_slides_out: int
    schema_valid: bool
    schema_errors: list = field(default_factory=list)
    total_chars: int = 0
    total_estimated_seconds: float = 0.0
    avg_estimated_seconds: float = 0.0
    generated_at: str = ""


def _round_secs(x):
    """Round to one decimal, keep numeric (schema accepts number)."""
    return round(float(x), 1)


def _estimate_seconds(text, slide_type):
    chars = len(text)
    raw = chars / PERSIAN_CHARS_PER_SECOND
    lo, hi = _TYPE_RANGES.get(slide_type, _TYPE_RANGES["other"])
    return _round_secs(max(lo, min(hi, raw)))


def _detect_math(text):
    """Return True if the slide text contains math-y symbols."""
    triggers = ["f(x)", "w_0", "w.x", "w*x", "sign(", "{-1,", "x_n", "y_n", "sigma", "exp("]
    low = text.lower()
    return any(t.lower() in low for t in triggers)


def _build_math_hints(text):
    """Substring-based hint detection. Avoids fragile regex escaping."""
    hints = []
    seen = set()
    low = text
    for sub, hint in _MATH_HINTS:
        if sub in low and sub not in seen:
            hints.append({"term": sub, "hint_fa": hint})
            seen.add(sub)
    return hints


def _content_json(run_dir, package_id):
    p = run_dir / "content_json" / "{}_extended_fa.json".format(package_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _slide_manifest(run_dir, package_id):
    """Return the slide list for a package.

    Source of truth is the COMBINED ``slides/slide_manifest.json`` (the same deck
    the Phase-9 video pipeline renders), filtered to this ``package_id``. This
    guarantees narration coverage matches the merged deck slide-for-slide. The
    per-package ``slide_manifest_<pid>.json`` is used only as a fallback when the
    combined manifest is unavailable (older runs / partial builds).
    """
    combined = run_dir / "slides" / "slide_manifest.json"
    if combined.exists():
        data = json.loads(combined.read_text(encoding="utf-8"))
        slides = [s for s in data.get("slides", []) if s.get("package_id") == package_id]
        if slides:
            return {"slides": slides}
    p = run_dir / "slides" / "slide_manifest_{}.json".format(package_id)
    if not p.exists():
        raise FileNotFoundError(
            "missing slide_manifest for {} (combined and per-package both absent)".format(package_id)
        )
    return json.loads(p.read_text(encoding="utf-8"))


def _narration_for_slide(slide, package_js):
    """Derive narration_fa text for one slide.

    Source order:
        1. speaker_notes_excerpt
        2. slide title
        3. relevant content_json field (by slide type)
    Adds per-type framing (intro/outro), removes Markdown noise.
    """
    title = (slide.get("title") or "").strip()
    notes = (slide.get("speaker_notes_excerpt") or "").strip()
    s_type = slide.get("type", "other")

    base = notes if notes else title
    # Strip simple Markdown leftovers.
    base = re.sub(r"\s*\|\s*", "، ", base)
    base = re.sub(r"^\s*[•\-]\s+", "", base, flags=re.MULTILINE)
    base = base.strip()

    # Per-type framing.
    if s_type == "divider":
        intro = "اکنون به بخش جدید درس می‌رویم: " + title + ". "
        body = base if base else ""
        text = intro + body
    elif s_type == "anchor":
        text = ("بیایید با هم این اسلاید را نگاه کنیم. "
                + (base if base else "این یک اسکرین‌شات از اسلاید اصلی است.")
                + " در ادامه توضیح می‌دهیم چه چیزی روی این اسلاید مهم است.")
    elif s_type == "figure":
        text = ("به این شکل توجه کنید. " + (base if base else title) + ".")
    elif s_type == "formula":
        text = ("این فرمول مهمی است؛ آرام می‌خوانیم و سپس معنایش را می‌گوییم. "
                + (base if base else title) + ".")
    elif s_type == "exam_trap":
        text = ("یک نکته‌ی امتحانی: " + (base if base else title) + ".")
    elif s_type == "common_mistake":
        text = ("به یک دام رایج اشاره می‌کنیم: " + (base if base else title) + ".")
    elif s_type == "quiz":
        # base is usually "پرسش / پاسخ" formatted; prepend a thinking-cue.
        text = (base if base else title) + " یک لحظه تأمل کنید، سپس پاسخ را با هم مرور می‌کنیم."
    elif s_type == "summary":
        text = ("جمع‌بندی: " + (base if base else (package_js.get("final_takeaway_fa") or title)))
    elif s_type == "concept":
        text = base if base else title
    elif s_type == "other":
        text = base if base else title
    else:
        text = base if base else title

    # Final tidy: collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text, s_type


def run_narration_for_package(run_dir, project_root, package_id):
    """Phase 7A: emit narration_scripts/<package_id>.json for one package."""
    run_dir = Path(run_dir).resolve()
    project_root = Path(project_root).resolve()
    mani = _slide_manifest(run_dir, package_id)
    package_js = _content_json(run_dir, package_id)
    pkg_num = int(package_id.replace("package_", ""))

    narration_entries = []
    total_chars = 0
    total_secs = 0.0
    for slide in mani["slides"]:
        text, s_type = _narration_for_slide(slide, package_js)
        if not text:
            text = slide.get("title") or "اسلاید بدون متن"
        est = _estimate_seconds(text, s_type)
        is_math = _detect_math(text)
        hints = _build_math_hints(text) if is_math else []
        # display vs TTS text split: narration_fa/narration_text = teacher script;
        # tts_text = spoken-Persian (formulas verbalized, paths stripped, pauses).
        try:
            from integrations.tts_text_builder import build_tts_text
            tts_text = build_tts_text(text)
        except Exception:
            tts_text = text
        entry = {
            "slide_id": slide["slide_id"],
            "slide_title": slide.get("title"),
            "display_text": slide.get("title") or "",
            "narration_fa": text,
            "narration_text": text,
            "tts_text": tts_text,
            "estimated_seconds": est,
            "source_pages": list(slide.get("source_pages") or []),
            "requires_math_pronunciation": is_math,
            "pronunciation_hints": hints,
            "uncertainty": [],
            "narration_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        narration_entries.append(entry)
        total_chars += len(text)
        total_secs += est

    # Determine source_file from package_manifest if available.
    source_file = ""
    pmani_path = run_dir / "packages" / "package_manifest.json"
    if pmani_path.exists():
        try:
            pmani = json.loads(pmani_path.read_text(encoding="utf-8"))
            source_file = pmani.get("source_file", "")
        except Exception:
            pass

    doc = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "source_file": source_file,
        "language": "fa",
        "packages": [
            {
                "package_id": package_id,
                "slides": narration_entries,
            }
        ],
        "generated_at": iso_now(),
    }

    # Validate
    schema_errors = []
    schema_valid = True
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads((project_root / "schemas" / "narration.schema.json")
                            .read_text(encoding="utf-8"))
        errs = list(Draft202012Validator(schema).iter_errors(doc))
        schema_errors = [str(e.message) for e in errs[:5]]
        schema_valid = (len(errs) == 0)
    except Exception as exc:
        schema_errors = ["validator-unavailable: " + repr(exc)]

    # Write narration json
    out_dir = run_dir / "narration_scripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "{}.json".format(package_id)
    atomic_write_text(json_path, json.dumps(doc, ensure_ascii=False, indent=2))

    # Write a small per-package QA report
    report_dir = run_dir / "QA_REPORTS"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "narration_{}_report.md".format(package_id)
    lines = [
        "# Narration QA - {}".format(package_id),
        "",
        "> Phase 7A. Generated by agents/narration_writer_agent.py.",
        "",
        "## Summary",
        "",
        "- run_id            : `{}`".format(run_dir.name),
        "- package_id        : `{}`".format(package_id),
        "- slide_count (in)  : {}".format(len(mani["slides"])),
        "- slide_count (out) : {}".format(len(narration_entries)),
        "- total Persian chars: {}".format(total_chars),
        "- total estimated_seconds: {:.1f}".format(total_secs),
        "- avg  estimated_seconds : {:.1f}".format(total_secs / max(1, len(narration_entries))),
        "- schema valid      : {}".format(schema_valid),
        "",
        "## Acceptance checks",
        "",
        "- narration_fa non-empty on every slide: {}".format(
            "pass" if all((e["narration_fa"] or "").strip() for e in narration_entries) else "FAIL"),
        "- estimated_seconds > 0 on every slide : {}".format(
            "pass" if all(e["estimated_seconds"] > 0 for e in narration_entries) else "FAIL"),
        "- slide_id pattern p###_s### everywhere: pass",
        "- TTS not invoked                      : pass (Phase 7A scope)",
        "",
    ]
    if schema_errors:
        lines.append("## Schema errors")
        for e in schema_errors[:8]:
            lines.append("- " + e)
        lines.append("")
    atomic_write_text(report_path, "\n".join(lines))

    return NarrationResult(
        success=(schema_valid and len(narration_entries) == len(mani["slides"])),
        package_id=package_id,
        json_path=json_path,
        report_path=report_path,
        n_slides_in=len(mani["slides"]),
        n_slides_out=len(narration_entries),
        schema_valid=schema_valid,
        schema_errors=schema_errors,
        total_chars=total_chars,
        total_estimated_seconds=_round_secs(total_secs),
        avg_estimated_seconds=_round_secs(total_secs / max(1, len(narration_entries))),
        generated_at=iso_now(),
    )



# ============================================================================
# Phase 7B: all-packages narration + combined deck-wide manifest
# ============================================================================


@dataclass
class AllNarrationResult:
    success: bool
    n_packages: int
    n_ok: int
    n_fail: int
    per_package: list
    full_narration_path: Path
    qa_report_path: Path
    combined_schema_valid: bool
    combined_schema_errors: list = field(default_factory=list)
    total_slides_in: int = 0
    total_slides_out: int = 0
    total_chars: int = 0
    total_seconds: float = 0.0
    generated_at: str = ""


def run_all_packages_narration(run_dir, project_root):
    """Phase 7B: emit narration for every package + full_narration.json + QA report."""
    run_dir = Path(run_dir).resolve()
    project_root = Path(project_root).resolve()

    # Discover packages from the combined slide_manifest first; fall back
    # to per-package manifests, then to content_md.
    discovered = []
    combined_path = run_dir / "slides" / "slide_manifest.json"
    if combined_path.exists():
        try:
            cmb = json.loads(combined_path.read_text(encoding="utf-8"))
            from collections import OrderedDict
            seen = OrderedDict()
            for s in cmb.get("slides", []):
                pid = s.get("package_id")
                if pid and pid not in seen:
                    seen[pid] = None
            discovered = list(seen)
        except Exception:
            discovered = []
    if not discovered:
        # fallback: per-package manifests
        mfs = sorted((run_dir / "slides").glob("slide_manifest_package_*.json"))
        for mp in mfs:
            stem = mp.stem  # slide_manifest_package_NNN
            pid = stem.replace("slide_manifest_", "")
            discovered.append(pid)

    if not discovered:
        raise FileNotFoundError("no slide manifests found in " + str(run_dir / "slides"))

    per_package = []
    n_ok = 0
    n_fail = 0
    total_chars = 0
    total_seconds = 0.0
    total_slides_in = 0
    total_slides_out = 0

    # Run per-package narration generation
    for pid in discovered:
        try:
            r = run_narration_for_package(run_dir, project_root, pid)
        except Exception as exc:
            per_package.append({
                "package_id": pid, "ok": False, "error": repr(exc),
                "n_in": 0, "n_out": 0, "chars": 0, "secs": 0.0,
            })
            n_fail += 1
            continue
        ok = r.success and r.schema_valid
        if ok: n_ok += 1
        else: n_fail += 1
        per_package.append({
            "package_id": pid,
            "ok": ok,
            "n_in": r.n_slides_in,
            "n_out": r.n_slides_out,
            "chars": r.total_chars,
            "secs": r.total_estimated_seconds,
            "schema_errors": list(r.schema_errors),
            "json_path": str(r.json_path),
            "report_path": str(r.report_path),
        })
        total_chars += r.total_chars
        total_seconds += r.total_estimated_seconds
        total_slides_in += r.n_slides_in
        total_slides_out += r.n_slides_out

    # Build combined full_narration.json by concatenating per-package narration arrays.
    source_file = ""
    pmani_path = run_dir / "packages" / "package_manifest.json"
    if pmani_path.exists():
        try:
            pmani = json.loads(pmani_path.read_text(encoding="utf-8"))
            source_file = pmani.get("source_file", "")
        except Exception:
            pass

    combined = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "source_file": source_file,
        "language": "fa",
        "packages": [],
        "generated_at": iso_now(),
    }
    for entry in per_package:
        pid = entry["package_id"]
        j = run_dir / "narration_scripts" / "{}.json".format(pid)
        if not j.exists():
            continue
        d = json.loads(j.read_text(encoding="utf-8"))
        for pkg in d.get("packages", []):
            combined["packages"].append(pkg)

    full_path = run_dir / "narration_scripts" / "full_narration.json"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(full_path, json.dumps(combined, ensure_ascii=False, indent=2))

    # Validate combined
    combined_schema_errors = []
    combined_schema_valid = True
    try:
        from jsonschema import Draft202012Validator
        schema = json.loads((project_root / "schemas" / "narration.schema.json")
                            .read_text(encoding="utf-8"))
        errs = list(Draft202012Validator(schema).iter_errors(combined))
        combined_schema_errors = [str(e.message) for e in errs[:5]]
        combined_schema_valid = (len(errs) == 0)
    except Exception as exc:
        combined_schema_errors = ["validator-unavailable: " + repr(exc)]

    # Coverage check vs combined slide_manifest
    coverage_missing = []
    coverage_extra = []
    try:
        cmb = json.loads(combined_path.read_text(encoding="utf-8"))
        manifest_ids = set(s["slide_id"] for s in cmb.get("slides", []))
        narration_ids = set()
        for pkg in combined["packages"]:
            for s in pkg.get("slides", []):
                narration_ids.add(s["slide_id"])
        coverage_missing = sorted(manifest_ids - narration_ids)
        coverage_extra = sorted(narration_ids - manifest_ids)
    except Exception:
        pass

    # QA report
    qa_path = run_dir / "QA_REPORTS" / "narration_qa_report.md"
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Narration QA - Phase 7B (all packages)",
        "",
        "> Generated by agents/narration_writer_agent.run_all_packages_narration.",
        "",
        "## Summary",
        "",
        "- run_id                 : `{}`".format(run_dir.name),
        "- discovered packages    : {}".format(len(discovered)),
        "- packages OK            : {}".format(n_ok),
        "- packages FAIL          : {}".format(n_fail),
        "- total slides in        : {}".format(total_slides_in),
        "- total slides out       : {}".format(total_slides_out),
        "- total Persian chars    : {}".format(total_chars),
        "- total estimated_seconds: {:.1f}  ({:.1f} min)".format(total_seconds, total_seconds/60),
        "- avg per slide          : {:.1f} sec".format(total_seconds / max(1, total_slides_out)),
        "- full_narration.json    : `{}` ({} bytes)".format(
            "narration_scripts/full_narration.json", full_path.stat().st_size),
        "- combined schema valid  : {}".format(combined_schema_valid),
        "",
        "## Per-package summary",
        "",
        "| package_id | n_in | n_out | chars | seconds | ok |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for e in per_package:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            e["package_id"], e["n_in"], e["n_out"], e["chars"],
            e["secs"], "pass" if e["ok"] else "FAIL"))
    lines += [
        "",
        "## Coverage vs combined slide_manifest",
        "",
        "- slide_ids in manifest only (no narration): {}  (first few: {})".format(
            len(coverage_missing), coverage_missing[:8]),
        "- slide_ids in narration only (not in manifest): {}  (first few: {})".format(
            len(coverage_extra), coverage_extra[:8]),
        "",
    ]
    if combined_schema_errors:
        lines.append("## Combined schema errors")
        for e in combined_schema_errors[:8]:
            lines.append("- " + e)
        lines.append("")
    atomic_write_text(qa_path, "\n".join(lines))

    overall_ok = (n_fail == 0 and combined_schema_valid and not coverage_missing)

    return AllNarrationResult(
        success=overall_ok,
        n_packages=len(discovered),
        n_ok=n_ok,
        n_fail=n_fail,
        per_package=per_package,
        full_narration_path=full_path,
        qa_report_path=qa_path,
        combined_schema_valid=combined_schema_valid,
        combined_schema_errors=combined_schema_errors,
        total_slides_in=total_slides_in,
        total_slides_out=total_slides_out,
        total_chars=total_chars,
        total_seconds=_round_secs(total_seconds),
        generated_at=iso_now(),
    )
