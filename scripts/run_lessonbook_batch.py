"""run_lessonbook_batch — batch driver for the strict source-exhaustive lessonbook workflow.

Orchestrates MANY PDFs through the existing per-PDF `/convert-lecture-pdf` workflow, ONE AT A TIME,
for course-level convenience. **It does NOT lower quality and it does NOT merge PDFs by default**:
each PDF gets its own run folder, its own strict Claude-authored artifacts, its own draft PDF + strict
QA, and its own user-review gate. Python only orchestrates files/runs/status/bookkeeping; Claude does the
educational understanding/authoring per PDF, and the human approves each promotion.

Doctrine / safety (non-negotiable):
- **No merge by default.** `--merge` only writes a *merge-feasibility report* and recommends separate
  lessonbooks + a course index unless the user explicitly confirms the PDFs are one continuous lecture.
- **Real processing is gated behind `--execute`.** Without `--execute` (or with `--dry-run`) the script only
  PLANS and writes batch bookkeeping; it opens no PDF, extracts nothing, renders nothing, promotes nothing.
- **Failure isolation:** one failed PDF never destroys the batch. Default `--continue-on-safe-fail`; use
  `--stop-on-fail` to halt on the first failure.
- **Sequential by default** (reduces context/overload risk). Each PDF STOPs at its Claude authoring gate
  (the script extracts, then hands off to Claude) and again at the user-review gate (no auto-promotion).
- No TTS / ffmpeg / video / paid APIs. Canonical outputs are never modified. `.env.local` is never read.

Usage:
  python scripts/run_lessonbook_batch.py input/batch_001/ --dry-run        # plan only (default-safe)
  python scripts/run_lessonbook_batch.py input/batch_001/ --execute        # real: extract each PDF -> STOP at Claude gate
  python scripts/run_lessonbook_batch.py input/batch_001/ --merge          # write a merge-feasibility report only

Modes / flags:
  --dry-run                 plan + bookkeeping only; no extraction/render/promotion (default if neither given)
  --execute                 perform the deterministic per-PDF extraction (then STOP at each Claude gate)
  --sequential              process one PDF at a time (default; flag is explicit/no-op)
  --stop-on-fail            halt the batch on the first failure
  --continue-on-safe-fail   skip a failed PDF and continue (default)
  --max-files N             only the first N PDFs (after sorting/start-from)
  --start-from FILENAME     begin at this filename (natural-sorted position)
  --resume                  reuse the latest batch folder for this input and skip already-done PDFs
  --merge                   produce a merge-feasibility report instead of processing (never merges)
  --batch-name NAME         label for the batch folder (default: input folder name)
Exit: 0 ok, 2 bad input, 3 batch stopped on failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ----------------------------- helpers -----------------------------
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


def _natural_key(name: str):
    """Natural sort: 001 < 002 < 010 < 100; lecture2 < lecture10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _run_id(stem: str, ts: str) -> str:
    """Mirror run_lessonbook_pipeline._run_id so per-PDF runs match the existing convention."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")
    return f"{s}_{ts}"


def _scan(batch_dir: Path) -> list[Path]:
    pdfs = [p for p in batch_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    return sorted(pdfs, key=lambda p: _natural_key(p.name))


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


# ----------------------------- merge feasibility -----------------------------
def _merge_feasibility(batch_dir: Path, pdfs: list[Path], batch_dir_out: Path) -> Path:
    lines = [
        "# Merge feasibility report",
        "",
        f"> Input: `{_rel(batch_dir)}`  ·  PDFs: {len(pdfs)}  ·  Generated: (batch driver, read-only)",
        "",
        "## Default recommendation: **DO NOT MERGE — separate lessonbooks + a course index.**",
        "",
        "The batch workflow keeps each PDF a standalone source-exhaustive lessonbook by default. Merging is only",
        "appropriate when the PDFs are **one continuous lecture split across files** (e.g. parts of the same deck).",
        "If they are distinct lectures of a course, separate lessonbooks linked by `course_index.md` is better:",
        "each stays self-contained, depth is not diluted, and the user reviews/promotes each independently.",
        "",
        "## Files considered (natural-sorted)",
        "",
        "| # | file | size (B) |",
        "|---:|---|---:|",
    ]
    for i, p in enumerate(pdfs, 1):
        lines.append(f"| {i} | `{p.name}` | {p.stat().st_size} |")
    lines += [
        "",
        "## When merge IS justified (all must hold)",
        "- The user explicitly says: \"merge these PDFs into one lessonbook\".",
        "- The PDFs are confirmed to be one continuous lecture (shared numbering/topic flow), not distinct lectures.",
        "- A single coherent narrative arc across the combined slides is pedagogically better than separate books.",
        "",
        "## If merge is approved (future, separate prompt)",
        "Concatenate the source decks into one logical input, run the SAME strict source-exhaustive workflow on the",
        "combined deck (image-grounded inventory across all pages, one coverage matrix, one woven narrative), and",
        "STOP at the user-review gate. No quality shortcut. This report alone performs **no** merge.",
    ]
    out = batch_dir_out / "merge_feasibility_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ----------------------------- per-PDF planning / processing -----------------------------
def _plan_entry(pdf: Path, batch_ts: str, index: int) -> dict:
    rid = _run_id(pdf.stem, batch_ts)
    run_dir = ROOT / "runs" / rid
    return {
        "index": index,
        "input_pdf": _rel(pdf),
        "file_name": pdf.name,
        "size_bytes": pdf.stat().st_size,
        "planned_run_id": rid,
        "planned_run_dir": _rel(run_dir),
        "draft_pdf_path": f"runs/{rid}/lessonbook/final_lessonbook.pdf",
        "status": "planned",          # planned -> extracted -> awaiting_claude_authoring -> drafted -> reviewed | failed
        "strict_qa_verdict": "pending",
        "page_count": None, "chapter_count": None, "visual_asset_count": None,
        "review_status": "not_started",
        "issues": [],
    }


def _execute_one(entry: dict, pdf: Path) -> dict:
    """REAL deterministic step for one PDF: extraction only (run_lessonbook_pipeline --init equivalent),
    then STOP at the Claude authoring gate. Never authors content, renders, or promotes. Failure-isolated."""
    try:
        from integrations.pdf_extractor import extract_pdf  # heavy dep, local import
        run_dir = ROOT / entry["planned_run_dir"]
        pages_dir = run_dir / "extracted" / "pages"
        text_dir = run_dir / "extracted" / "text"
        pages_dir.mkdir(parents=True, exist_ok=True)
        text_dir.mkdir(parents=True, exist_ok=True)
        meta = extract_pdf(str(pdf), str(pages_dir), str(text_dir),
                           str(run_dir / "extracted" / "extraction_manifest.json"))
        (run_dir / "SOURCE.txt").write_text(
            f"source_pdf={pdf}\nsha256={meta.get('source_sha256','')}\n", encoding="utf-8")
        entry["status"] = "awaiting_claude_authoring"
        entry["page_count"] = meta.get("page_count")
        entry["issues"].append("extracted; STOP at Claude authoring gate (run the /convert-lecture-pdf authoring stages for this run, then the user-review gate).")
    except Exception as e:  # noqa: BLE001 — failure isolation is the point
        entry["status"] = "failed"
        entry["issues"].append(f"extraction failed (non-destructive; other PDFs unaffected): {type(e).__name__}: {e}")
    return entry


# ----------------------------- batch artifacts -----------------------------
def _write_batch_artifacts(batch_dir_out: Path, manifest: dict) -> None:
    batch_dir_out.mkdir(parents=True, exist_ok=True)
    (batch_dir_out / "batch_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    e = manifest["entries"]
    mode = manifest["mode"]
    # batch_status.md
    st = [
        f"# Batch status — {manifest['batch_name']}",
        "",
        f"> mode: **{mode}**  ·  input: `{manifest['input_dir']}`  ·  batch folder: `{manifest['batch_run_dir']}`",
        f"> PDFs: {len(e)}  ·  generated by run_lessonbook_batch.py (Python orchestrator; no authoring/render/promotion)",
        "",
        "| # | file | run folder | status | strict QA | review |",
        "|---:|---|---|---|---|---|",
    ]
    for it in e:
        st.append(f"| {it['index']} | `{it['file_name']}` | `{it['planned_run_dir']}` | {it['status']} | {it['strict_qa_verdict']} | {it['review_status']} |")
    st += ["", "## Per-PDF gates (each PDF, in order)",
           "1. Python `--execute` extracts pages+text+manifest → STOP at the Claude **inventory** gate.",
           "2. Claude authors the strict artifacts (inventory → criticality → coverage_matrix_v2 → visual plan → narratives → depth) — image-grounded, source-exhaustive.",
           "3. Python crops + XeLaTeX render → strict QA.",
           "4. **User-review gate** — no promotion without explicit approval.",
           "", "No merge performed. No quality shortcut. Failure of one PDF does not stop the batch (unless --stop-on-fail)."]
    (batch_dir_out / "batch_status.md").write_text("\n".join(st) + "\n", encoding="utf-8")

    # batch_qa_report.md
    qa = [
        f"# Batch QA report — {manifest['batch_name']}",
        "",
        f"> mode: **{mode}**. Per-PDF strict QA is run by the existing validators inside each run; this is the roll-up.",
        "",
        "| # | file | strict QA verdict | page | chapters | visuals | issues |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for it in e:
        qa.append(f"| {it['index']} | `{it['file_name']}` | {it['strict_qa_verdict']} | {it['page_count'] or '-'} | {it['chapter_count'] or '-'} | {it['visual_asset_count'] or '-'} | {'; '.join(it['issues']) or '-'} |")
    if mode == "dry_run":
        qa += ["", "**DRY-RUN:** no PDF was opened, no extraction/render/QA/promotion happened. The table above is the plan."]
    (batch_dir_out / "batch_qa_report.md").write_text("\n".join(qa) + "\n", encoding="utf-8")

    # course_index.md
    ci = [
        f"# Course index — {manifest['batch_name']}",
        "",
        f"> {len(e)} lessonbooks (one per source PDF; NOT merged). input: `{manifest['input_dir']}`.",
        "",
        "| # | input PDF | run folder | draft PDF | pages | chapters | visuals | strict QA | review | issues |",
        "|---:|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for it in e:
        ci.append(f"| {it['index']} | `{it['input_pdf']}` | `{it['planned_run_dir']}` | `{it['draft_pdf_path']}` | "
                  f"{it['page_count'] or '-'} | {it['chapter_count'] or '-'} | {it['visual_asset_count'] or '-'} | "
                  f"{it['strict_qa_verdict']} | {it['review_status']} | {'; '.join(it['issues']) or '-'} |")
    (batch_dir_out / "course_index.md").write_text("\n".join(ci) + "\n", encoding="utf-8")


def _find_latest_batch(batch_name: str):
    cands = sorted((ROOT / "runs").glob(f"batch_*_{batch_name}"))
    return cands[-1] if cands else None


# ----------------------------- main -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Batch lessonbook driver (Python orchestrator; STOPs at Claude + user gates).")
    ap.add_argument("batch_dir", help="input folder containing the PDFs (e.g. input/batch_001/)")
    ap.add_argument("--dry-run", action="store_true", help="plan + bookkeeping only; no extraction/render/promotion")
    ap.add_argument("--execute", action="store_true", help="perform the deterministic per-PDF extraction, then STOP at each Claude gate")
    ap.add_argument("--sequential", action="store_true", help="one PDF at a time (default)")
    ap.add_argument("--stop-on-fail", action="store_true", help="halt the batch on the first failure")
    ap.add_argument("--continue-on-safe-fail", action="store_true", help="skip a failed PDF and continue (default)")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--start-from", default=None, help="begin at this filename")
    ap.add_argument("--resume", action="store_true", help="reuse the latest batch folder and skip done PDFs")
    ap.add_argument("--merge", action="store_true", help="write a merge-feasibility report only (never merges)")
    ap.add_argument("--batch-name", default=None)
    args = ap.parse_args()

    batch_dir = Path(args.batch_dir)
    if not batch_dir.is_dir():
        print(f"[batch] input folder not found: {batch_dir}")
        return 2
    pdfs = _scan(batch_dir)
    if not pdfs:
        print(f"[batch] no .pdf files in {batch_dir}")
        return 2

    batch_name = args.batch_name or re.sub(r"[^A-Za-z0-9]+", "_", batch_dir.name).strip("_") or "batch"

    # --start-from / --max-files
    if args.start_from:
        names = [p.name for p in pdfs]
        if args.start_from in names:
            pdfs = pdfs[names.index(args.start_from):]
        else:
            print(f"[batch] --start-from '{args.start_from}' not found; using full sorted list")
    if args.max_files is not None:
        pdfs = pdfs[: args.max_files]

    # merge policy: report only, never merge
    if args.merge:
        out_dir = ROOT / "runs" / f"batch_{_ts()}_{batch_name}"
        out_dir.mkdir(parents=True, exist_ok=True)
        rep = _merge_feasibility(batch_dir, pdfs, out_dir)
        print(f"[batch] merge-feasibility report written: {_rel(rep)}")
        print("[batch] NO MERGE performed. Default recommendation: separate lessonbooks + course index.")
        return 0

    # mode resolution: default-safe (dry-run unless --execute)
    real = bool(args.execute) and not args.dry_run
    mode = "execute" if real else "dry_run"
    if not args.execute and not args.dry_run:
        print("[batch] no --execute given -> DEFAULT-SAFE DRY-RUN (plan only). Pass --execute to do real extraction.")
    stop_on_fail = bool(args.stop_on_fail)  # default is continue-on-safe-fail

    batch_ts = _ts()
    # resume: reuse latest batch folder + its manifest
    prior = _find_latest_batch(batch_name) if args.resume else None
    if prior is not None:
        batch_run_dir = prior
        try:
            prev = json.loads((batch_run_dir / "batch_manifest.json").read_text(encoding="utf-8"))
            done = {x["file_name"]: x for x in prev.get("entries", []) if x.get("status") in ("reviewed", "drafted", "awaiting_claude_authoring")}
        except Exception:
            done = {}
        print(f"[batch] --resume: reusing {_rel(batch_run_dir)} ({len(done)} PDFs already advanced)")
    else:
        batch_run_dir = ROOT / "runs" / f"batch_{batch_ts}_{batch_name}"
        done = {}

    entries = []
    failed = 0
    for i, pdf in enumerate(pdfs, 1):
        if pdf.name in done:
            e = done[pdf.name]; e["index"] = i; entries.append(e)
            print(f"[batch] {i}/{len(pdfs)} {pdf.name}: skip (resume; status={e.get('status')})")
            continue
        entry = _plan_entry(pdf, batch_ts, i)
        if real:
            entry = _execute_one(entry, pdf)
            if entry["status"] == "failed":
                failed += 1
                print(f"[batch] {i}/{len(pdfs)} {pdf.name}: FAILED — {entry['issues'][-1]}")
                if stop_on_fail:
                    entries.append(entry)
                    manifest = _mk_manifest(batch_name, batch_dir, batch_run_dir, batch_ts, mode, entries, stopped=True)
                    _write_batch_artifacts(batch_run_dir, manifest)
                    print(f"[batch] STOPPED on failure (--stop-on-fail). Bookkeeping at {_rel(batch_run_dir)}")
                    return 3
            else:
                print(f"[batch] {i}/{len(pdfs)} {pdf.name}: extracted -> {entry['planned_run_dir']} (STOP at Claude gate)")
        else:
            print(f"[batch] {i}/{len(pdfs)} {pdf.name}: PLANNED -> {entry['planned_run_dir']} (dry-run; not processed)")
        entries.append(entry)

    manifest = _mk_manifest(batch_name, batch_dir, batch_run_dir, batch_ts, mode, entries, stopped=False, failed=failed)
    _write_batch_artifacts(batch_run_dir, manifest)
    print(f"[batch] {mode}: {len(entries)} PDFs ({failed} failed). Bookkeeping at {_rel(batch_run_dir)}")
    print("[batch] artifacts: batch_manifest.json, batch_status.md, batch_qa_report.md, course_index.md")
    if mode == "dry_run":
        print("[batch] DRY-RUN — no PDF opened, no extraction/render/promotion. Per-PDF authoring is Claude's job under /convert-lecture-pdf.")
    else:
        print("[batch] NEXT: for each 'awaiting_claude_authoring' run, do the /convert-lecture-pdf authoring stages, then the user-review gate. No auto-promotion.")
    return 0


def _mk_manifest(batch_name, batch_dir, batch_run_dir, batch_ts, mode, entries, *, stopped=False, failed=0) -> dict:
    return {
        "schema_version": "1.0.0",
        "batch_name": batch_name,
        "input_dir": _rel(batch_dir),
        "batch_run_dir": _rel(batch_run_dir),
        "batch_timestamp": batch_ts,
        "mode": mode,
        "merge": False,
        "merge_policy": "no_merge_by_default; explicit user request + feasibility report required",
        "sequential": True,
        "stopped_on_failure": stopped,
        "failed_count": failed,
        "pdf_count": len(entries),
        "provenance": {"authored_by": "python_executor"},
        "doctrine": "each PDF = its own run + strict source-exhaustive artifacts + draft + QA + user-review gate; no quality shortcut",
        "entries": entries,
    }


if __name__ == "__main__":
    sys.exit(main())
