"""full_orchestrator - one-command, mode-gated, resumable pipeline driver.

`convert <token> --mode <mode>` walks every stage in order:
  extraction → classification → visual_review(3A.1) → segmentation →
  package_titles(3B.1) → content(Phase4) → package_pdfs → slides →
  narration → audio → video → final_qa

Each stage declares: executor label, precondition artifact, output artifact (for
RESUME skip), and whether it is an UNDERSTANDING stage (visual_review, titles,
content). Understanding stages are mode-gated via integrations/pipeline_mode and
NEVER silently fall back to weak heuristics:
  - manual_agent : require the run's authored input, else BLOCK with a clear message.
  - llm_dry_run  : estimate only; if no authored input, BLOCK (no fabrication, no call).
  - llm_paid     : fail-closed unless a real llm_client + approval + key + budget exist
                   (none today → BLOCK; no paid call attempted).

Deterministic/render/TTS/ffmpeg stages run via the existing per-stage scripts and
are skipped when their output already exists (resume). Nothing here uses file-1
fixtures; the file-1 gates live in the agents themselves.
"""
from __future__ import annotations

import glob
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SCRIPTS = PROJECT_ROOT / "scripts"

UNDERSTANDING = {"visual_review", "package_titles", "content"}


def _run_dir_for(token: str) -> Optional[Path]:
    cands = sorted((PROJECT_ROOT / "runs").glob(f"*_{token}_*"))
    cands = [c for c in cands if c.is_dir()]
    return cands[-1] if cands else None


def _packages(run_dir: Path) -> List[str]:
    p = run_dir / "packages" / "package_manifest.json"
    if not p.is_file():
        return []
    try:
        return [pk["package_id"] for pk in json.loads(p.read_text(encoding="utf-8"))["packages"]]
    except Exception:
        return []


@dataclass
class Stage:
    id: str
    title: str
    executor: str
    output_rel: List[str]              # outputs (resume skip if all exist)
    authored_rel: List[str] = field(default_factory=list)   # understanding input(s); any-of
    cmd: Optional[List[str]] = None    # template; {run}/{token}/{pkg} filled at runtime
    per_package: bool = False

    @property
    def understanding(self) -> bool:
        return self.id in UNDERSTANDING


def _stages() -> List[Stage]:
    sp = str(SCRIPTS)
    return [
        Stage("extraction", "PDF extraction", "CODE_DETERMINISTIC",
              ["extracted/extraction_manifest.json"], cmd=[PY, sp + "/convert_file.py", "{token}", "--stop-after", "extraction"]),
        Stage("classification", "Page classification", "CODE_HEURISTIC",
              ["classification/page_classification.json"], cmd=[PY, sp + "/classify_pages.py", "--run", "{run}"]),
        Stage("visual_review", "Visual review (3A.1)", "CLAUDE_CODE_MANUAL_AGENT/EXTERNAL_LLM",
              ["classification/page_classification_enriched.json"],
              authored_rel=["classification/llm_page_review.json", "classification/authored_review.json"],
              cmd=[PY, sp + "/review_low_confidence_pages.py", "--run", "{run}"]),
        Stage("segmentation", "Package segmentation (3B)", "CODE_HEURISTIC",
              ["packages/package_manifest.json"], cmd=[PY, sp + "/segment_packages.py", "--run", "{run}"]),
        Stage("package_titles", "Package titles/metadata (3B.1)", "CLAUDE_CODE_MANUAL_AGENT/EXTERNAL_LLM",
              ["packages/package_metadata_llm.json"],
              authored_rel=["packages/package_metadata_llm.json"], cmd=None),
        Stage("content", "Persian content (Phase 4)", "CLAUDE_CODE_MANUAL_AGENT/EXTERNAL_LLM",
              ["content_json/{pkg}_extended_fa.json"],
              authored_rel=["content_authored/{pkg}.json"],
              cmd=[PY, sp + "/generate_package_content.py", "--run", "{run}", "--package", "{pkg}"], per_package=True),
        Stage("package_pdfs", "Package PDFs (5)", "CODE_DETERMINISTIC",
              ["package_pdfs"], cmd=[PY, sp + "/build_package_pdf.py", "--run", "{run}", "--all"]),
        Stage("slides", "Teaching slides + images (6)", "CODE_DETERMINISTIC+LIBREOFFICE",
              ["slides/slide_manifest.json"],
              cmd=[PY, sp + "/build_teaching_slides.py", "--run", "{run}", "--all", "--merge", "--export-images", "--dpi", "200"]),
        Stage("narration", "Narration (7)", "CODE_HEURISTIC",
              ["narration_scripts/full_narration.json"], cmd=[PY, sp + "/generate_narration.py", "--run", "{run}", "--all"]),
        Stage("audio", "TTS audio (8)", "TTS_PROVIDER",
              ["audio/audio_manifest.json"], cmd=[PY, sp + "/generate_audio.py", "--run", "{run}", "--all"]),
        Stage("video", "Video + final concat (9)", "FFMPEG_RENDER",
              ["artifact_manifest.json"], cmd=[PY, sp + "/build_video.py", "--run", "{run}", "--all", "--concat-final"]),
        Stage("final_qa", "Final QA (9D)", "CODE_DETERMINISTIC",
              ["QA_REPORTS/final_validation_report.md"], cmd=[PY, sp + "/run_final_qa.py", "--run", "{run}"]),
    ]


def _exists_all(run_dir: Path, rels: List[str], pkgs: List[str]) -> bool:
    for rel in rels:
        if "{pkg}" in rel:
            if not pkgs or not all((run_dir / rel.format(pkg=p)).exists() for p in pkgs):
                return False
        else:
            if not (run_dir / rel).exists():
                return False
    return True


def _authored_present(run_dir: Path, st: Stage, pkgs: List[str]) -> bool:
    if not st.authored_rel:
        return True
    for rel in st.authored_rel:
        if "{pkg}" in rel:
            if pkgs and all((run_dir / rel.format(pkg=p)).exists() for p in pkgs):
                return True
        else:
            if (run_dir / rel).exists():
                return True
    return False


def plan(token: str, mode: str) -> dict:
    from integrations.pipeline_mode import resolve_mode, mode_allows_paid
    mode = resolve_mode(mode)
    run_dir = _run_dir_for(token)
    pkgs = _packages(run_dir) if run_dir else []
    rows = []
    for st in _stages():
        if run_dir is None:
            action = "run" if st.id == "extraction" else "pending (no run yet)"
            done = False
        else:
            done = _exists_all(run_dir, st.output_rel, pkgs)
            action = "skip (done)" if done else "run"
            if not done and st.understanding:
                authored = _authored_present(run_dir, st, pkgs)
                if mode in ("manual_agent", "llm_dry_run") and not authored:
                    action = "BLOCKED: author " + " | ".join(st.authored_rel)
                elif mode == "llm_dry_run" and authored:
                    action = "run (authored)"
                elif mode == "llm_dry_run":
                    action = "ESTIMATE only (LLM dry-run; no call, no fabrication)"
                elif mode == "llm_paid":
                    action = "PAID-GATED (needs llm_client+approval+key+budget; fail-closed)"
        rows.append({"stage": st.id, "title": st.title, "executor": st.executor,
                     "understanding": st.understanding, "done": done, "action": action})
    return {"token": token, "mode": mode, "paid_allowed_mode": mode_allows_paid(mode),
            "run_dir": str(run_dir) if run_dir else None, "packages": pkgs, "stages": rows}


def write_plan_report(token: str, mode: str) -> Path:
    pl = plan(token, mode)
    run_dir = Path(pl["run_dir"]) if pl["run_dir"] else (PROJECT_ROOT / "QA_REPORTS")
    out_dir = (Path(pl["run_dir"]) / "QA_REPORTS") if pl["run_dir"] else (PROJECT_ROOT / "QA_REPORTS")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"autopilot_full_plan_{pl['mode']}.md"
    lines = [f"# Autopilot full plan — token `{token}` · mode `{pl['mode']}`", "",
             f"- run_dir: {pl['run_dir'] or '(none yet)'}",
             f"- paid calls allowed in this mode: {pl['paid_allowed_mode']}",
             f"- packages: {pl['packages'] or '(unknown)'}", "",
             "| # | stage | executor | understanding | done | action |",
             "|---|---|---|---|---|---|"]
    for i, r in enumerate(pl["stages"], 1):
        lines.append(f"| {i} | {r['stage']} | {r['executor']} | {'YES' if r['understanding'] else ''} | {'✓' if r['done'] else ''} | {r['action']} |")
    lines += ["", "_No stage hides manual-agent work; understanding stages never fall back to weak heuristics._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_full_convert(token: str, mode: str, *, approve_paid: bool = False) -> dict:
    """Execute stages in order, honoring resume + mode gates. Returns a result dict."""
    from integrations.pipeline_mode import resolve_mode, fail_closed_if_stage_needs_llm, PipelineModeError
    mode = resolve_mode(mode)
    results = []
    run_dir = _run_dir_for(token)
    pkgs = _packages(run_dir) if run_dir else []

    for st in _stages():
        run_dir = _run_dir_for(token)  # re-resolve (extraction may create it)
        pkgs = _packages(run_dir) if run_dir else []
        done = run_dir is not None and _exists_all(run_dir, st.output_rel, pkgs)
        if done:
            results.append((st.id, "skip"))
            continue

        # understanding-stage gate
        if st.understanding:
            authored = run_dir is not None and _authored_present(run_dir, st, pkgs)
            try:
                fail_closed_if_stage_needs_llm(
                    "content_generation" if st.id == "content" else
                    ("visual_review" if st.id == "visual_review" else "package_titles"),
                    mode, has_authored=authored, llm_client_available=False, approved=approve_paid)
            except PipelineModeError as e:
                results.append((st.id, f"BLOCKED: {e}"))
                return {"token": token, "mode": mode, "stopped_at": st.id,
                        "reason": str(e), "results": results}
            if mode in ("manual_agent", "llm_dry_run") and not authored:
                msg = "author " + " | ".join(a.format(pkg="<pkg>") for a in st.authored_rel)
                results.append((st.id, f"BLOCKED: {msg}"))
                return {"token": token, "mode": mode, "stopped_at": st.id,
                        "reason": msg, "results": results}
            if st.cmd is None:  # authored-only stage (e.g. titles): present → ok, absent handled above
                results.append((st.id, "ok (authored)"))
                continue

        if st.cmd is None:
            results.append((st.id, "skip (no command)"))
            continue

        # build + run command(s)
        targets = pkgs if st.per_package else [None]
        for pk in targets:
            cmd = []
            for tok in st.cmd:
                t = tok.replace("{token}", token)
                if run_dir is not None:
                    t = t.replace("{run}", str(run_dir))
                if pk is not None:
                    t = t.replace("{pkg}", pk)
                cmd.append(t)
            env_note = {"ILVF_PIPELINE_MODE": mode}
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env={**_env(), **env_note}, cwd=str(PROJECT_ROOT))
            if proc.returncode != 0:
                results.append((st.id, f"FAILED rc={proc.returncode}: {proc.stderr[-200:]}"))
                return {"token": token, "mode": mode, "stopped_at": st.id,
                        "reason": proc.stderr[-300:], "results": results}
        results.append((st.id, "ran"))
    return {"token": token, "mode": mode, "stopped_at": None, "results": results}


def _env():
    import os
    return dict(os.environ)
