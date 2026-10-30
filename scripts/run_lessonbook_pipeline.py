"""run_lessonbook_pipeline — orchestrate the PYTHON stages of the lessonbook pipeline.

Owns ONLY the deterministic stages — folders, extraction, crop/annotation execution,
XeLaTeX render, validation, status. It STOPS at every Claude-authoring gate (source-page
inventory, page understanding, criticality + coverage matrix v2, package plan, visual-asset
plan, chapter narratives, depth review, outline) and tells you which artifact Claude must
author next. It:
  - never authors/decides educational content,
  - never fabricates inventory/coverage/depth artifacts (no empty placeholders),
  - never calls paid APIs, never runs TTS/ffmpeg/video,
  - never promotes (copying to output/ needs explicit user approval).

Strict source-exhaustive gating (default for any NEW PDF): `--render` refuses to render until the
Claude-authored strict artifacts exist AND the strict content validators pass. Legacy runs (e.g. 004/005,
authored before the v2 gates) render with `--allow-legacy` (clearly marked). Generic for ANY input PDF.
Contract: docs/FUTURE_PDF_LESSONBOOK_WORKFLOW_CONTRACT.md ; audit loop: docs/AUTOMATIC_SOURCE_COVERAGE_AUDIT_LOOP.md.

Usage:
  python scripts/run_lessonbook_pipeline.py --init input/<file>.pdf       # extract + scaffold, then STOP
  python scripts/run_lessonbook_pipeline.py --assets   runs/<id>          # crop/annotate from Claude's plan
  python scripts/run_lessonbook_pipeline.py --render   runs/<id>          # strict-gated XeLaTeX render
  python scripts/run_lessonbook_pipeline.py --render   runs/<id> --allow-legacy   # render a pre-v2 (legacy) run
  python scripts/run_lessonbook_pipeline.py --validate runs/<id>          # aggregate QA (auto strict/legacy)
  python scripts/run_lessonbook_pipeline.py --status   runs/<id>          # read-only strict stage checklist
  python scripts/run_lessonbook_pipeline.py --strict-status runs/<id>     # alias of --status
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.pdf_extractor import extract_pdf, sha256_of_file  # noqa: E402
from scripts.lessonbook_status import find_book_dir  # noqa: E402

PY = sys.executable


def _first(d: Path, *pats: str):
    for p in pats:
        hits = sorted(d.glob(p))
        if hits:
            return hits[0]
    return None


def _next_run_number() -> int:
    nums = []
    for d in (ROOT / "runs").glob("*"):
        m = re.match(r"(\d{3})_", d.name)
        if d.is_dir() and m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def run_init(pdf_str: str) -> int:
    pdf = Path(pdf_str).resolve()
    if not pdf.is_file():
        print(f"[init] PDF not found: {pdf}")
        return 3
    stem = re.sub(r"[^A-Za-z0-9]+", "_", pdf.stem).strip("_") or "lecture"
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    run_dir = ROOT / "runs" / f"{_next_run_number():03d}_{stem}_{ts}"
    pages_dir, text_dir = run_dir / "extracted" / "pages", run_dir / "extracted" / "text"
    print(f"[init] extracting {pdf.name} -> {run_dir.name}/extracted/ ...")
    results = extract_pdf(pdf, pages_dir, text_dir)
    try:
        src_rel = str(pdf.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        src_rel = pdf.name
    manifest = {
        "schema_version": "1.0.0", "run_id": run_dir.name, "source_file": src_rel,
        "source_sha256": sha256_of_file(pdf), "page_count": len(results),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {"authored_by": "python_executor"}, "pages": results,
    }
    (run_dir / "extracted" / "extraction_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "lessonbook").mkdir(parents=True, exist_ok=True)
    print(f"[init] extracted {len(results)} pages; scaffolded {run_dir.name}/lessonbook/")
    print("[init] STOP -- first Claude authoring gate (Python must NOT fabricate it):")
    print("       author lessonbook/source_page_inventory.json -- the INDEPENDENT enumeration of every")
    print("       meaningful element of every page (Source Page Inventory Lead), plus claude_page_review.json.")
    print("       This is the strict gate that must exist BEFORE package planning.")
    print(f"       strict status any time: {PY} scripts/lessonbook_status.py --run runs/{run_dir.name}")
    return 0


def _book_or_stop(run_dir: Path) -> Path | None:
    book = find_book_dir(run_dir) or (run_dir / "lessonbook")
    if not book.is_dir():
        print(f"[pipeline] no lessonbook/ in {run_dir}; run --init first.")
        return None
    return book


def run_assets(run_str: str) -> int:
    run_dir = Path(run_str).resolve()
    book = _book_or_stop(run_dir)
    if book is None:
        return 3
    plans = sorted(book.glob("*_visual_asset_plan.json"))
    if not plans:
        print(f"[assets] STOP -- no *_visual_asset_plan.json in {book}.")
        print("[assets] Claude must author the visual-asset plan first (which crops/annotations + crop boxes).")
        return 3
    rc_all = 0
    for plan in plans:
        print(f"[assets] executing plan: {plan.name}")
        rc = subprocess.call([PY, str(ROOT / "scripts" / "create_lessonbook_visual_assets.py"),
                              "--plan", str(plan), "--run-dir", str(run_dir),
                              "--out", str(book / "visual_assets")])
        rc_all = rc_all or rc
    return rc_all


def _render_legacy(book: Path, stem: str) -> int:
    print(f"[render] LEGACY mode (--allow-legacy): strict v2 gates NOT enforced for {book.name} "
          "(use only for pre-v2 runs such as 004/005).")
    if not (book / "lessonbook_outline.json").is_file():
        print(f"[render] STOP -- missing {book.name}/lessonbook_outline.json (Claude must author the outline).")
        return 3
    if not any(book.glob("chapter_*_narrative.json")):
        print(f"[render] STOP -- no chapter_*_narrative.json in {book} (Claude must author the chapters).")
        return 3
    print(f"[render] XeLaTeX render of {book.name} (stem={stem}) ...")
    return subprocess.call([PY, str(ROOT / "scripts" / "build_lessonbook_pdf.py"),
                            "--book", str(book), "--stem", stem])


def run_render(run_str: str, stem: str = "final_lessonbook", allow_legacy: bool = False, depth_pending: bool = False) -> int:
    run_dir = Path(run_str).resolve()
    book = _book_or_stop(run_dir)
    if book is None:
        return 3
    if allow_legacy:
        return _render_legacy(book, stem)

    # --- strict path (default for any new PDF): required Claude-authored artifacts must EXIST ---
    inv = _first(book, "source_page_inventory.json", "*source_page_inventory.json") or _first(run_dir, "source_page_inventory.json")
    cov2 = _first(book, "*coverage_matrix_v2.json")
    vplan = _first(book, "*_visual_asset_plan.json")
    vmanifest = _first(book, "*_visual_asset_manifest.json")
    outline = book / "lessonbook_outline.json"
    narratives = sorted(book.glob("chapter_*_narrative.json"))
    depth_files = sorted(book.glob("*depth_review.json"))
    extraction = run_dir / "extracted" / "extraction_manifest.json"

    gates = [
        ("lessonbook/source_page_inventory.json", bool(inv),
         "Source Page Inventory Lead -- enumerate every meaningful element of every page (before planning)"),
        ("lessonbook/*_source_element_coverage_matrix_v2.json", bool(cov2),
         "Criticality Assigner + Package Teaching Lead -- assign criticality + author coverage matrix v2"),
        ("lessonbook/*_visual_asset_plan.json", bool(vplan),
         "Visual-Asset Planning Lead -- which crops/annotations + crop boxes"),
        ("lessonbook/chapter_*_narrative.json", bool(narratives),
         "Package Teaching Lead + Narrative Tutor Writer -- chapter narratives"),
        ("lessonbook/lessonbook_outline.json", outline.is_file(),
         "Full-Lessonbook Assembler -- lessonbook_outline.json"),
    ]
    if not depth_pending:
        gates.append(("lessonbook/*_source_exhaustive_depth_review.json", bool(depth_files),
                      "Coverage Auditor -- per-chapter depth review (or pass --depth-pending for an interim render)"))

    missing = [(name, hint) for name, present, hint in gates if not present]
    if missing:
        print(f"[render] STOP -- strict source-exhaustive gates not satisfied for {book.name}.")
        print("[render] missing Claude-authored artifact(s) -- Python must NOT fabricate these:")
        for name, hint in missing:
            print(f"          - {name}\n              next gate: {hint}")
        print("[render] author the FIRST missing artifact next, then re-run --render.")
        print("[render] (for a legacy run such as 004/005, pass --allow-legacy to render without the v2 gates.)")
        return 3

    if vplan and not vmanifest:
        print(f"[render] STOP -- visual-asset plan present but not executed; run: --assets {run_dir.name} first.")
        return 3

    # --- strict CONTENT validation BEFORE render (no rendered PDF needed yet) ---
    print(f"[render] strict pre-render validation of {book.name} ...")
    checks: list[list] = [
        [ROOT / "scripts" / "validate_source_page_inventory.py", "--inventory", inv]
        + (["--extraction", extraction] if extraction.is_file() else []),
        [ROOT / "scripts" / "validate_coverage_matrix_v2.py", "--matrix", cov2, "--inventory", inv],
    ]
    if vmanifest and vplan:
        checks.append([ROOT / "scripts" / "validate_visual_asset_manifest.py", "--manifest", vmanifest, "--plan", vplan])
    if depth_files:
        checks.append([ROOT / "scripts" / "validate_source_exhaustive_depth.py", "--review", book, "--narrative", book])
    pre = 0
    for c in checks:
        rc = subprocess.call([PY] + [str(x) for x in c])
        pre = pre or rc
    if pre != 0:
        print("[render] STOP -- strict validation FAILED before render. Revise ONLY the affected "
              "package/section(s), re-run validation, then --render. Not rendering.")
        return 3

    print(f"[render] strict pre-render validation PASSED; XeLaTeX render of {book.name} (stem={stem}) ...")
    print("[render] reminder: run `--validate` again AFTER render (it adds the PDF/readability checks); "
          "promotion to output/ still needs explicit user approval (no auto-promote).")
    return subprocess.call([PY, str(ROOT / "scripts" / "build_lessonbook_pdf.py"),
                            "--book", str(book), "--stem", stem])


def _delegate(script: str, run_str: str, *extra: str) -> int:
    return subprocess.call([PY, str(ROOT / "scripts" / script), "--run", str(Path(run_str).resolve()), *extra])


def main() -> int:
    ap = argparse.ArgumentParser(description="Orchestrate the deterministic stages of the lessonbook pipeline (strict-gated).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", metavar="PDF", help="extract a new PDF into a fresh run + scaffold lessonbook/, then STOP")
    g.add_argument("--assets", metavar="RUN", help="execute Claude's visual-asset plan(s) (PIL crops/annotations)")
    g.add_argument("--render", metavar="RUN", help="strict-gated XeLaTeX render of the Claude-authored book")
    g.add_argument("--validate", metavar="RUN", help="aggregate QA (delegates to lessonbook_validate.py; auto strict/legacy)")
    g.add_argument("--status", metavar="RUN", help="read-only strict stage checklist (delegates to lessonbook_status.py)")
    g.add_argument("--strict-status", metavar="RUN", help="alias of --status (read-only strict stage checklist)")
    ap.add_argument("--stem", default="final_lessonbook", help="render stem for --render (e.g. final_lessonbook_polished_v2)")
    ap.add_argument("--allow-legacy", action="store_true", help="with --render: render a pre-v2 (legacy) run without the strict gates")
    ap.add_argument("--depth-pending", action="store_true", help="with --render: allow an interim render before the depth review exists")
    args = ap.parse_args()
    if args.init:
        return run_init(args.init)
    if args.assets:
        return run_assets(args.assets)
    if args.render:
        return run_render(args.render, args.stem, allow_legacy=args.allow_legacy, depth_pending=args.depth_pending)
    if args.validate:
        return _delegate("lessonbook_validate.py", args.validate)
    if args.status:
        return _delegate("lessonbook_status.py", args.status)
    if args.strict_status:
        return _delegate("lessonbook_status.py", args.strict_status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
