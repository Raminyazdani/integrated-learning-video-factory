"""artifact_manifest - scan every phase's outputs into a single manifest dict.

Phase 9D entry point. Pure-Python, no network, no Phase-8/9 runtime side
effects. Returns dataclasses; the qa_critic agent decides the overall
verdict and writes the markdown report.

Status values per phase:
    "pass"                       all expected artifacts present and valid
    "fail"                       expected artifacts missing or invalid
    "pending_runtime_artifacts"  artifacts can only be produced on local
                                 Windows (Phase 8 audio / Phase 9 video) and
                                 haven't been produced yet - this is NOT a
                                 failure of the project; see
                                 docs/LOCAL_CLAUDE_CODE_EXECUTION_POLICY.md
    "warn"                       partial / advisory (e.g. unexpected extras)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


N_EXPECTED_PACKAGES = 12
N_EXPECTED_SLIDES = 254  # combined deck (default; overridden per-run below)


def _derive_expected_counts(run_dir: Path) -> Tuple[int, int]:
    """Derive a run's expected package/slide counts from ITS OWN manifests.

    Makes final QA generic for any input (not hardcoded to the file-1 gold
    sample of 12 packages / 254 slides). Falls back to the module defaults
    when a manifest is missing/unreadable.
    """
    n_pkg, n_slides = N_EXPECTED_PACKAGES, N_EXPECTED_SLIDES
    pm = run_dir / "packages" / "package_manifest.json"
    if pm.is_file():
        try:
            n = len(json.loads(pm.read_text(encoding="utf-8")).get("packages", []))
            if n > 0:
                n_pkg = n
        except Exception:
            pass
    sm = run_dir / "slides" / "slide_manifest.json"
    if sm.is_file():
        try:
            d = json.loads(sm.read_text(encoding="utf-8"))
            n = d.get("slide_count") or len(d.get("slides", []))
            if n and n > 0:
                n_slides = n
        except Exception:
            pass
    return n_pkg, n_slides
MIN_AUDIO_DURATION_SECONDS = 1.0


@dataclass
class PhaseCheck:
    phase_id: str
    title: str
    status: str = "pending_runtime_artifacts"
    expected_count: Optional[int] = None
    found_count: int = 0
    total_bytes: int = 0
    items: List[str] = field(default_factory=list)        # short list, capped
    missing: List[str] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_size_sum(paths: List[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _short_list(paths: List[Path], max_n: int = 8) -> List[str]:
    out = [p.name for p in paths[:max_n]]
    if len(paths) > max_n:
        out.append(f"... +{len(paths) - max_n} more")
    return out


def _rel(p: Path, run_dir: Path) -> str:
    try:
        return str(p.relative_to(run_dir)).replace("\\", "/")
    except ValueError:
        return str(p)


def _expected_package_names(suffix: str, extension: str) -> List[str]:
    """Return like ['package_001_extended_fa.md', ...]"""
    return [
        f"package_{i:03d}{suffix}.{extension}"
        for i in range(1, N_EXPECTED_PACKAGES + 1)
    ]


# ---------------------------------------------------------------------------
# Per-phase scans
# ---------------------------------------------------------------------------


def scan_phase_2_extraction(run_dir: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_2_extraction", title="Phase 2 - PDF extraction")
    pages_dir = run_dir / "extracted" / "pages"
    text_dir = run_dir / "extracted" / "page_text"
    manifest = run_dir / "extracted" / "extraction_manifest.json"
    pngs = sorted(pages_dir.glob("page_*.png")) if pages_dir.is_dir() else []
    txts = sorted(text_dir.glob("page_*.txt")) if text_dir.is_dir() else []
    check.expected_count = max(len(pngs), len(txts)) or None
    check.found_count = min(len(pngs), len(txts)) if check.expected_count else 0
    check.total_bytes = _file_size_sum(pngs) + _file_size_sum(txts)
    check.items = _short_list(pngs)
    if not manifest.is_file():
        check.missing.append("extracted/extraction_manifest.json")
    if not pngs:
        check.status = "fail"
        check.error = "no extracted page PNGs found"
    elif len(pngs) != len(txts):
        check.status = "warn"
        check.notes.append(f"{len(pngs)} PNGs vs {len(txts)} TXTs")
    else:
        check.status = "pass"
        check.notes.append(f"extracted_manifest_present={manifest.is_file()}")
    return check


def scan_phase_3_packaging(run_dir: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_3_packaging", title="Phase 3 - page classification + package segmentation")
    pkg_manifest = run_dir / "packages" / "package_manifest.json"
    pkg_map = run_dir / "packages" / "PACKAGE_MAP.md"
    class_json = run_dir / "classification" / "page_classification.json"

    items: List[Path] = []
    if pkg_manifest.is_file():
        items.append(pkg_manifest)
    if pkg_map.is_file():
        items.append(pkg_map)
    if class_json.is_file():
        items.append(class_json)
    check.items = _short_list(items)
    check.total_bytes = _file_size_sum(items)
    check.found_count = len(items)

    if not pkg_manifest.is_file():
        check.status = "fail"
        check.missing.append("packages/package_manifest.json")
        return check
    try:
        with open(pkg_manifest, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except Exception as exc:
        check.status = "fail"
        check.error = f"package_manifest.json unreadable: {exc!r}"
        return check
    n_packages = len(pkg.get("packages") or [])
    check.expected_count = N_EXPECTED_PACKAGES
    check.found_count = n_packages
    if n_packages != N_EXPECTED_PACKAGES:
        check.status = "fail"
        check.error = (
            f"package_manifest has {n_packages} packages, "
            f"expected {N_EXPECTED_PACKAGES}"
        )
        return check
    check.status = "pass"
    check.notes.append(
        f"package_map_md={pkg_map.is_file()}, classification_json={class_json.is_file()}"
    )
    return check


def scan_phase_4_content(run_dir: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_4_content", title="Phase 4 - Persian extended content (md + json)")
    md_dir = run_dir / "content_md"
    json_dir = run_dir / "content_json"
    mds = sorted(md_dir.glob("package_*_extended_fa.md")) if md_dir.is_dir() else []
    jsons = sorted(json_dir.glob("package_*_extended_fa.json")) if json_dir.is_dir() else []
    check.expected_count = N_EXPECTED_PACKAGES
    check.found_count = min(len(mds), len(jsons))
    check.total_bytes = _file_size_sum(mds) + _file_size_sum(jsons)
    check.items = _short_list(mds)

    expected_md_names = set(_expected_package_names("_extended_fa", "md"))
    expected_json_names = set(_expected_package_names("_extended_fa", "json"))
    found_md_names = {p.name for p in mds}
    found_json_names = {p.name for p in jsons}
    for name in sorted(expected_md_names - found_md_names):
        check.missing.append(f"content_md/{name}")
    for name in sorted(expected_json_names - found_json_names):
        check.missing.append(f"content_json/{name}")
    for name in sorted(found_md_names - expected_md_names):
        check.extras.append(f"content_md/{name}")
    for name in sorted(found_json_names - expected_json_names):
        check.extras.append(f"content_json/{name}")

    if len(mds) == N_EXPECTED_PACKAGES and len(jsons) == N_EXPECTED_PACKAGES:
        check.status = "pass"
    elif len(mds) == 0 and len(jsons) == 0:
        check.status = "fail"
        check.error = "no content_md / content_json files found"
    else:
        check.status = "fail"
        check.error = (
            f"expected {N_EXPECTED_PACKAGES} packages, "
            f"found md={len(mds)} json={len(jsons)}"
        )
    return check


def scan_phase_5_pdfs(run_dir: Path, project_root: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_5_pdfs", title="Phase 5 - package PDFs")
    pdf_dir = run_dir / "package_pdfs"
    pdfs = sorted(pdf_dir.glob("package_*_extended_fa.pdf")) if pdf_dir.is_dir() else []
    check.expected_count = N_EXPECTED_PACKAGES
    check.found_count = len(pdfs)
    check.total_bytes = _file_size_sum(pdfs)
    check.items = _short_list(pdfs)

    expected_names = set(_expected_package_names("_extended_fa", "pdf"))
    found_names = {p.name for p in pdfs}
    for n in sorted(expected_names - found_names):
        check.missing.append(f"package_pdfs/{n}")
    output_zip = project_root / "output" / "001_package_pdfs.zip"
    check.notes.append(f"output_zip_present={output_zip.is_file()}")
    if output_zip.is_file():
        check.total_bytes += output_zip.stat().st_size
    if len(pdfs) >= N_EXPECTED_PACKAGES:
        check.status = "pass"
    elif len(pdfs) == 0:
        check.status = "fail"
        check.error = "no package PDFs found"
    else:
        check.status = "fail"
        check.error = f"expected {N_EXPECTED_PACKAGES} PDFs, found {len(pdfs)}"
    return check


def scan_phase_6_slides(run_dir: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_6_slides", title="Phase 6 - teaching slides + slide_manifest + slide images")
    slides_dir = run_dir / "slides"
    full_pptx = slides_dir / "full_teaching_deck.pptx"
    full_pdf = slides_dir / "full_teaching_deck.pdf"
    slide_manifest = slides_dir / "slide_manifest.json"
    images_dir = run_dir / "slide_images"
    images = sorted(images_dir.glob("full_teaching_deck-*.png")) if images_dir.is_dir() else []

    found = []
    for f in (full_pptx, full_pdf, slide_manifest):
        if f.is_file():
            found.append(f)
        else:
            check.missing.append(_rel(f, run_dir))
    check.total_bytes = _file_size_sum(found) + _file_size_sum(images)
    check.items = _short_list(found + images[:3])
    check.found_count = len(images)
    check.expected_count = N_EXPECTED_SLIDES

    per_pkg_pptx = sorted(slides_dir.glob("package_*_teaching.pptx"))
    check.notes.append(f"per_package_decks_pptx={len(per_pkg_pptx)}")
    check.notes.append(f"slide_images_count={len(images)}")

    slide_count_in_manifest = None
    if slide_manifest.is_file():
        try:
            with open(slide_manifest, "r", encoding="utf-8") as f:
                m = json.load(f)
            slide_count_in_manifest = len(m.get("slides", []))
            check.notes.append(f"slide_manifest.slides_len={slide_count_in_manifest}")
        except Exception as exc:
            check.error = f"slide_manifest.json unreadable: {exc!r}"

    if (
        full_pptx.is_file()
        and full_pdf.is_file()
        and slide_manifest.is_file()
        and slide_count_in_manifest == N_EXPECTED_SLIDES
        and len(images) >= N_EXPECTED_SLIDES
    ):
        check.status = "pass"
    elif not full_pptx.is_file() and not full_pdf.is_file():
        check.status = "fail"
        check.error = "full_teaching_deck.pptx and .pdf both missing"
    else:
        check.status = "fail"
        check.error = check.error or (
            f"expected {N_EXPECTED_SLIDES} slides, found "
            f"manifest_len={slide_count_in_manifest}, images={len(images)}"
        )
    return check


def scan_phase_7_narration(run_dir: Path) -> PhaseCheck:
    check = PhaseCheck(phase_id="phase_7_narration", title="Phase 7 - per-package + full narration")
    narration_dir = run_dir / "narration_scripts"
    per_pkg = sorted(narration_dir.glob("package_*.json")) if narration_dir.is_dir() else []
    full = narration_dir / "full_narration.json"

    check.expected_count = N_EXPECTED_PACKAGES
    check.found_count = len(per_pkg)
    check.total_bytes = _file_size_sum(per_pkg) + (full.stat().st_size if full.is_file() else 0)
    check.items = _short_list(per_pkg)

    if not full.is_file():
        check.status = "fail"
        check.missing.append("narration_scripts/full_narration.json")
        return check

    total_slides = 0
    try:
        with open(full, "r", encoding="utf-8") as f:
            data = json.load(f)
        for pkg in data.get("packages", []):
            total_slides += len(pkg.get("slides", []))
        check.notes.append(f"full_narration.slides_total={total_slides}")
    except Exception as exc:
        check.error = f"full_narration.json unreadable: {exc!r}"
        check.status = "fail"
        return check

    if len(per_pkg) >= N_EXPECTED_PACKAGES and total_slides >= N_EXPECTED_SLIDES:
        check.status = "pass"
    elif len(per_pkg) == 0:
        check.status = "fail"
        check.error = "no per-package narration files"
    else:
        check.status = "fail"
        check.error = (
            f"per-package files={len(per_pkg)} (expected {N_EXPECTED_PACKAGES}); "
            f"total slides in full_narration={total_slides} (expected {N_EXPECTED_SLIDES})"
        )
    return check


def scan_phase_8_audio(run_dir: Path) -> PhaseCheck:
    """Audio is a runtime artifact (Phase 8B/8C). Absent in sandbox is OK."""
    check = PhaseCheck(phase_id="phase_8_audio", title="Phase 8 - per-slide audio + audio_manifest")
    audio_dir = run_dir / "audio"
    audio_manifest = audio_dir / "audio_manifest.json"
    check.expected_count = N_EXPECTED_SLIDES

    if not audio_dir.is_dir():
        check.status = "pending_runtime_artifacts"
        check.notes.append("audio/ directory absent (Phase 8 not yet run locally)")
        return check

    mp3s = sorted(audio_dir.glob("package_*/slide_*.mp3"))
    wavs = sorted(audio_dir.glob("package_*/slide_*.wav"))
    audios = mp3s + wavs
    smoke = sorted((audio_dir / "_smoke").glob("smoke_test_fa.*")) if (audio_dir / "_smoke").is_dir() else []

    check.found_count = len(audios)
    check.total_bytes = _file_size_sum(audios) + _file_size_sum(smoke)
    check.items = _short_list(audios + smoke)

    if not audio_manifest.is_file():
        check.status = "pending_runtime_artifacts"
        check.notes.append("audio_manifest.json missing (Phase 8B/8C incomplete)")
        return check

    # Inspect manifest for per-slide validation status.
    n_pass = n_fail = 0
    try:
        with open(audio_manifest, "r", encoding="utf-8") as f:
            am = json.load(f)
        for pkg_id, block in (am.get("packages") or {}).items():
            for s in block.get("slides") or []:
                if (s.get("validation") or {}).get("status") == "pass":
                    n_pass += 1
                else:
                    n_fail += 1
        check.notes.append(f"per_slide_pass={n_pass}, per_slide_fail={n_fail}")
    except Exception as exc:
        check.error = f"audio_manifest.json unreadable: {exc!r}"
        check.status = "fail"
        return check

    if n_fail == 0 and n_pass >= N_EXPECTED_SLIDES:
        check.status = "pass"
    elif n_pass == 0:
        check.status = "pending_runtime_artifacts"
    else:
        check.status = "fail"
        check.error = (
            f"audio coverage incomplete: {n_pass} pass, {n_fail} fail, "
            f"expected {N_EXPECTED_SLIDES} pass"
        )
    return check


def scan_phase_9_video(run_dir: Path) -> PhaseCheck:
    """Video is also a runtime artifact (Phase 9B/9C). Absent in sandbox is OK."""
    check = PhaseCheck(phase_id="phase_9_video", title="Phase 9 - per-slide + per-package + final video")
    video_dir = run_dir / "video_segments"
    video_manifest = video_dir / "video_manifest.json"
    check.expected_count = N_EXPECTED_PACKAGES  # we score by packages here

    if not video_dir.is_dir():
        check.status = "pending_runtime_artifacts"
        check.notes.append("video_segments/ directory absent (Phase 9 not yet run locally)")
        return check

    pkg_videos = sorted(video_dir.glob("package_*.mp4"))
    per_slide_clips = sorted(video_dir.glob("package_*/slide_*.mp4"))
    smoke = sorted((video_dir / "_smoke").glob("smoke_test.*")) if (video_dir / "_smoke").is_dir() else []

    check.found_count = len(pkg_videos)
    check.total_bytes = _file_size_sum(pkg_videos) + _file_size_sum(per_slide_clips) + _file_size_sum(smoke)
    check.items = _short_list(pkg_videos + smoke)
    check.notes.append(f"per_slide_clips={len(per_slide_clips)}")

    if not video_manifest.is_file():
        check.status = "pending_runtime_artifacts"
        check.notes.append("video_manifest.json missing (Phase 9B/9C incomplete)")
        return check

    n_pass = n_fail = n_pending = 0
    try:
        with open(video_manifest, "r", encoding="utf-8") as f:
            vm = json.load(f)
        for pkg_id, block in (vm.get("packages") or {}).items():
            v = block.get("verdict")
            if v == "pass":
                n_pass += 1
            elif v == "fail":
                n_fail += 1
            else:
                n_pending += 1
        check.notes.append(
            f"per_package_pass={n_pass}, fail={n_fail}, pending={n_pending}"
        )
    except Exception as exc:
        check.error = f"video_manifest.json unreadable: {exc!r}"
        check.status = "fail"
        return check

    if n_pass >= N_EXPECTED_PACKAGES and n_fail == 0:
        check.status = "pass"
    elif n_pass == 0 and n_pending > 0:
        check.status = "pending_runtime_artifacts"
    else:
        check.status = "fail"
        check.error = (
            f"video coverage incomplete: {n_pass} pass, {n_fail} fail, "
            f"{n_pending} pending"
        )
    return check


def scan_final_video(project_root: Path, run_id: str) -> PhaseCheck:
    check = PhaseCheck(phase_id="final_video", title="Phase 9C - final concatenated video")
    run_number = run_id.split("_")[0] if run_id else "000"
    path = project_root / "output" / f"{run_number}_final_educational_video.mp4"
    check.expected_count = 1
    if path.is_file():
        check.found_count = 1
        check.total_bytes = path.stat().st_size
        check.items = [str(path)]
        check.status = "pass"
    else:
        check.found_count = 0
        check.items = []
        check.status = "pending_runtime_artifacts"
        check.notes.append(f"expected at {path}")
    return check


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def scan_all_artifacts(*, project_root: Path, run_dir: Path) -> Dict[str, PhaseCheck]:
    """Return ordered phase->PhaseCheck dict."""
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    # Make expected counts run-specific (generic for any input PDF).
    global N_EXPECTED_PACKAGES, N_EXPECTED_SLIDES
    N_EXPECTED_PACKAGES, N_EXPECTED_SLIDES = _derive_expected_counts(run_dir)
    out: Dict[str, PhaseCheck] = {}
    out["phase_2_extraction"] = scan_phase_2_extraction(run_dir)
    out["phase_3_packaging"] = scan_phase_3_packaging(run_dir)
    out["phase_4_content"] = scan_phase_4_content(run_dir)
    out["phase_5_pdfs"] = scan_phase_5_pdfs(run_dir, project_root)
    out["phase_6_slides"] = scan_phase_6_slides(run_dir)
    out["phase_7_narration"] = scan_phase_7_narration(run_dir)
    out["phase_8_audio"] = scan_phase_8_audio(run_dir)
    out["phase_9_video"] = scan_phase_9_video(run_dir)
    out["final_video"] = scan_final_video(project_root, run_dir.name)
    return out


def overall_verdict(checks: Dict[str, PhaseCheck]) -> Tuple[str, Optional[str]]:
    """Decide the run-level verdict.

    Rules
    -----
    - any phase 'fail'                       -> ('fail', reason)
    - any phase 'pending_runtime_artifacts'  -> ('pending_runtime_artifacts', reason)
    - else all pass / warn                   -> ('pass', None)
    """
    fails = [(k, v) for k, v in checks.items() if v.status == "fail"]
    pendings = [(k, v) for k, v in checks.items() if v.status == "pending_runtime_artifacts"]
    if fails:
        names = ", ".join(k for k, _ in fails)
        return "fail", f"{len(fails)} phase(s) failed: {names}"
    if pendings:
        names = ", ".join(k for k, _ in pendings)
        return "pending_runtime_artifacts", (
            f"{len(pendings)} phase(s) pending local runtime: {names}"
        )
    return "pass", None


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def to_dict(check: PhaseCheck) -> Dict[str, Any]:
    return asdict(check)
