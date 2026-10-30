"""create_company_task_graph - build a DRY-RUN company task graph for a run.

Models page-review parallel workers, per-package content (nested subworker groups),
slide spec, narration, audio, video, join gates after each parallel stage, and the
final QA board. Writes task_graph.json + dry_run_report.md. NO pipeline execution.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.company_task_graph import Task, save_graph, validate_graph, validate_schema, summary  # noqa: E402


def _pages(run_dir: Path):
    for n in ("page_classification.json", "page_understanding.json", "page_classification_enriched.json"):
        p = run_dir / "classification" / n
        if p.is_file():
            return [pg["page_id"] for pg in json.loads(p.read_text(encoding="utf-8")).get("pages", [])]
    return [f"page_{i:03d}" for i in range(1, 4)]


def _packages(run_dir: Path):
    p = run_dir / "packages" / "package_manifest.json"
    if p.is_file():
        return [pk["package_id"] for pk in json.loads(p.read_text(encoding="utf-8")).get("packages", [])]
    return ["package_001"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="create_company_task_graph")
    ap.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    run_dir = Path(args.run) if Path(args.run).is_absolute() else (ROOT / args.run)
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run not found: {run_dir}", file=sys.stderr); return 2

    pages = _pages(run_dir)
    pkgs = _packages(run_dir)
    T = []

    def add(task_id, dept, agent, role, specialty, deps, *, pg=None, jg=None,
            outs=None, reviewer=None, sgate=None, authored_by="pending", parent=None):
        T.append(Task(
            task_id=task_id, department=dept, agent_name=agent, role_type=role, specialty=specialty,
            parent_task_id=parent, dependencies=deps, parallel_group_id=pg, join_gate_id=jg,
            input_artifacts=[], output_artifacts=(outs or []),
            handoff_path=f"company_workflow/handoffs/{task_id}.handoff.json",
            reviewer_agent=reviewer, supervisor_gate=sgate, status="pending", max_retries=2,
            provenance={"authored_by": authored_by, "source_sha": None, "timestamp": None},
        ).to_dict())

    # 1. extraction (PYTHON)
    add("T_extract", "extraction", "file-intake-extraction-queen", "queen", "pdf_extraction", [],
        outs=["extracted/extraction_manifest.json"], authored_by="python_executor")

    # 2. page review — parallel per page (CLAUDE) + join gate
    page_tasks = []
    for pid in pages:
        tid = f"T_pagerev_{pid}"
        page_tasks.append(tid)
        add(tid, "page_understanding", "page-review-worker", "worker", "page_understanding",
            ["T_extract"], pg="PG_page_review", outs=[f"classification/page_understanding.json#{pid}"],
            reviewer="page-review-qa", sgate="JG_page_review", authored_by="claude_code_manual_agent")
    add("GATE_page_review", "page_understanding", "page-review-qa", "supervisor", "join_gate",
        page_tasks, pg="PG_page_review", jg="JG_page_review", reviewer="page-review-qa",
        sgate="JG_page_review", authored_by="coordination")
    add("T_understanding", "page_understanding", "page-understanding-queen", "queen", "understanding_merge",
        ["GATE_page_review"], outs=["classification/page_understanding.json"], authored_by="claude_code_manual_agent")

    # 3. grouping + metadata (CLAUDE, sequential)
    add("T_grouping", "package_architecture", "package-grouping-worker", "worker", "package_grouping",
        ["T_understanding"], outs=["packages/package_grouping.json"], reviewer="package-architecture-queen",
        sgate="SG_grouping", authored_by="claude_code_manual_agent")
    add("T_metadata", "package_architecture", "package-metadata-worker", "worker", "package_metadata",
        ["T_grouping"], outs=["packages/package_metadata.json"], reviewer="package-architecture-queen",
        sgate="SG_metadata", authored_by="claude_code_manual_agent")

    # 4. content — per package: subworker group (formula/figure/glossary) + sub-gate, then lead; leads join gate
    lead_tasks = []
    for pk in pkgs:
        subs = []
        for spec, agent in (("formula", "formula-math-tutor-worker"),
                            ("figure", "figure-diagram-explainer-worker"),
                            ("glossary_exam", "glossary-example-exam-worker")):
            tid = f"T_content_{pk}_{spec}"
            subs.append(tid)
            add(tid, "content", agent, "subworker", spec, ["T_metadata"],
                pg=f"PG_content_{pk}", outs=[f"content_authored/{pk}.json#{spec}"],
                reviewer="package-content-company-queen", sgate=f"JG_content_{pk}",
                authored_by="claude_code_manual_agent")
        add(f"GATE_content_{pk}", "content", "package-content-company-queen", "supervisor", "join_gate",
            subs, pg=f"PG_content_{pk}", jg=f"JG_content_{pk}",
            reviewer="package-content-company-queen", sgate=f"JG_content_{pk}", authored_by="coordination")
        lead = f"T_content_lead_{pk}"
        lead_tasks.append(lead)
        add(lead, "content", "package-content-worker", "lead", "content_integration",
            [f"GATE_content_{pk}"], pg="PG_content_leads", outs=[f"content_authored/{pk}.json"],
            reviewer="package-content-company-queen", sgate="JG_content", authored_by="claude_code_manual_agent")
    add("GATE_content", "content", "package-content-company-queen", "supervisor", "join_gate",
        lead_tasks, pg="PG_content_leads", jg="JG_content",
        reviewer="package-content-company-queen", sgate="JG_content", authored_by="coordination")

    # 5. slides — per package (CLAUDE spec) + join gate (PYTHON render implied)
    slide_tasks = []
    for pk in pkgs:
        tid = f"T_slide_{pk}"
        slide_tasks.append(tid)
        add(tid, "slides", "slide-spec-worker", "worker", "slide_spec", ["GATE_content"],
            pg="PG_slides", outs=[f"slides/slide_spec.json#{pk}"], reviewer="rtl-typography-qa-worker",
            sgate="JG_slides", authored_by="claude_code_manual_agent")
    add("GATE_slides", "slides", "rtl-typography-qa-worker", "supervisor", "join_gate",
        slide_tasks, pg="PG_slides", jg="JG_slides", reviewer="rtl-typography-qa-worker",
        sgate="JG_slides", authored_by="coordination")

    # 6. narration — per package narration + tts (CLAUDE) + join gate
    narr_tasks = []
    for pk in pkgs:
        n = f"T_narr_{pk}"; tt = f"T_ttstext_{pk}"
        narr_tasks += [n, tt]
        add(n, "narration", "narration-worker", "worker", "transcript", ["GATE_slides"],
            pg="PG_narration", outs=[f"narration_scripts/full_narration.json#{pk}.narration"],
            reviewer="narration-company-queen", sgate="JG_narration", authored_by="claude_code_manual_agent")
        add(tt, "narration", "tts-text-worker", "worker", "tts_text", [n],
            pg="PG_narration", outs=[f"narration_scripts/full_narration.json#{pk}.tts_text"],
            reviewer="narration-company-queen", sgate="JG_narration", authored_by="claude_code_manual_agent")
    add("GATE_narration", "narration", "narration-company-queen", "supervisor", "join_gate",
        narr_tasks, pg="PG_narration", jg="JG_narration", reviewer="narration-company-queen",
        sgate="JG_narration", authored_by="coordination")

    # 7. audio — per package (GEMINI/Edge) + join gate
    audio_tasks = []
    for pk in pkgs:
        tid = f"T_audio_{pk}"
        audio_tasks.append(tid)
        add(tid, "audio", "gemini-audio-worker", "worker", "tts_synthesis", ["GATE_narration"],
            pg="PG_audio", outs=[f"audio/audio_manifest.json#{pk}"], reviewer="gemini-tts-audio-queen",
            sgate="JG_audio", authored_by="gemini_tts")
    add("GATE_audio", "audio", "gemini-tts-audio-queen", "supervisor", "join_gate",
        audio_tasks, pg="PG_audio", jg="JG_audio", reviewer="gemini-tts-audio-queen",
        sgate="JG_audio", authored_by="coordination")

    # 8. video — per package (PYTHON) + join gate + concat
    vid_tasks = []
    for pk in pkgs:
        tid = f"T_video_{pk}"
        vid_tasks.append(tid)
        add(tid, "video", "video-segment-worker", "worker", "video_segment", ["GATE_audio"],
            pg="PG_video", outs=[f"video_segments/{pk}.mp4"], reviewer="video-assembly-queen",
            sgate="JG_video", authored_by="python_executor")
    add("GATE_video", "video", "video-assembly-queen", "supervisor", "join_gate",
        vid_tasks, pg="PG_video", jg="JG_video", reviewer="video-assembly-queen",
        sgate="JG_video", authored_by="coordination")
    add("T_concat", "video", "video-assembly-queen", "queen", "final_concat", ["GATE_video"],
        outs=["output/<NNN>_final_educational_video.mp4"], authored_by="python_executor")

    # 9. final QA board + safety
    add("T_final_qa", "final_qa", "final-qa-board-queen", "queen", "final_acceptance", ["T_concat"],
        outs=["QA_REPORTS/final_validation_report.md"], authored_by="coordination")
    add("T_regression", "safety", "regression-safety-queen", "queen", "regression_safety", ["T_final_qa"],
        outs=["QA_REPORTS/regression_suite_report.md"], authored_by="python_executor")

    graph = {
        "run_id": run_dir.name, "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "dry_run": True, "tasks": T,
    }
    s = summary(graph)
    graph["parallel_groups"] = s["parallel_groups"]
    graph["join_gates"] = s["join_gates"]

    out = run_dir / "company_workflow" / "task_graph.json"
    save_graph(graph, out)
    ok, errs = validate_graph(graph)
    sok, smsg = validate_schema(graph, ROOT / "schemas" / "company_task_graph.schema.json")

    # dry-run report
    rep = run_dir / "company_workflow" / "dry_run_report.md"
    lines = ["# Company Task Graph — Dry Run", "",
             f"- run: {run_dir.name}", f"- tasks: {s['tasks']}",
             f"- parallel groups ({len(s['parallel_groups'])}): {s['parallel_groups']}",
             f"- join gates ({len(s['join_gates'])}): {s['join_gates']}",
             f"- roles: {s['by_role']}",
             f"- DAG + policy valid: **{ok}**" + ("" if ok else f" — {errs}"),
             f"- schema: {smsg}",
             "- **no pipeline executed (dry run).**", ""]
    rep.write_text("\n".join(lines), encoding="utf-8")
    print(f"[create_company_task_graph] tasks={s['tasks']} groups={len(s['parallel_groups'])} "
          f"gates={len(s['join_gates'])} valid={ok} schema={sok}")
    print(f"  wrote {out}")
    print(f"  wrote {rep}")
    if not ok:
        for e in errs:
            print("  ERR:", e)
    return 0 if (ok and sok) else 3


if __name__ == "__main__":
    sys.exit(main())
