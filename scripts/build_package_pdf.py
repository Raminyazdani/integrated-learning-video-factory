"""build_package_pdf - Phase 5 CLI for the LaTeX/PDF builder.

Phase 5A (smoke test):
    python scripts/build_package_pdf.py --run runs/001_1_20260520_2128 --smoke-test

Phase 5B (single package):
    python scripts/build_package_pdf.py --run runs/001_1_20260520_2128 --package package_001

Phase 5C (all packages):
    python scripts/build_package_pdf.py --run runs/001_1_20260520_2128 --all
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.pdf_builder_agent import run_smoke_test, run_package_build, run_all_packages  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_package_pdf",
        description="Phase 5 LaTeX/PDF builder. 5A: smoke. 5B: single package. 5C: --all.",
    )
    parser.add_argument("--run", required=True, help="Path to runs/<run_id>/")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Phase 5A: minimal smoke-test PDF.")
    parser.add_argument("--package", help="Phase 5B: build one package PDF.")
    parser.add_argument("--all", action="store_true",
                        help="Phase 5C: build PDFs for every content_md/package_NNN_*.md.")
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="Phase 5C: rebuild PDFs that already exist (default skips).")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="xelatex compile attempts per package (default 3).")
    args = parser.parse_args(argv)

    modes = sum([bool(args.smoke_test), bool(args.package), bool(args.all)])
    if modes == 0:
        parser.error("Specify one of --smoke-test, --package, or --all.")
    if modes > 1:
        parser.error("--smoke-test / --package / --all are mutually exclusive.")

    run_dir = Path(args.run).resolve() if Path(args.run).is_absolute() else (PROJECT_ROOT / args.run).resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        print("[ERROR] run folder not found: " + str(run_dir), file=sys.stderr)
        return 2

    if args.smoke_test:
        return _run_smoke(run_dir)
    if args.package:
        return _run_package(run_dir, args.package, args.max_attempts)
    return _run_all(run_dir, args.max_attempts, not args.no_skip_existing)


def _run_smoke(run_dir):
    print("[Phase 5A] LaTeX/PDF smoke test in: " + str(run_dir))
    try:
        result = run_smoke_test(run_dir, PROJECT_ROOT)
    except Exception:
        print("[ERROR] Unexpected failure during smoke test:", file=sys.stderr)
        traceback.print_exc()
        return 1
    return _print_smoke_result(result)


def _print_smoke_result(result):
    print()
    print("=== Phase 5A summary ===")
    env = result.env_info
    print("xelatex      : {} ({})".format(env.get("path"), env.get("version")))
    for pkg in ("polyglossia", "xepersian", "fontspec", "graphicx", "geometry", "hyperref"):
        print("{:13s}: {}".format(pkg, "yes" if env.get(pkg) else "no"))
    print("cascade      : {}  (final fallback: {})".format(result.cascade, result.final_fallback))
    print("chosen font  : {}".format(result.chosen_font))
    print("image used   : {}".format(result.image_used))
    print("attempts     : {}".format(result.attempts))
    if result.fix_notes:
        for n in result.fix_notes: print("  fix: " + n)
    print(".tex written : {}".format(result.tex_path))
    print("PDF path     : {}".format(result.pdf_path))
    print("PDF bytes    : {}".format(result.pdf_bytes))
    if result.success:
        print("[Phase 5A] SMOKE TEST: pass"); return 0
    print("[Phase 5A] SMOKE TEST: FAIL")
    return 3


def _run_package(run_dir, package_id, max_attempts):
    print("[Phase 5B] LaTeX/PDF build for {} in: {}".format(package_id, run_dir))
    try:
        result = run_package_build(run_dir, PROJECT_ROOT, package_id, max_attempts=max_attempts)
    except FileNotFoundError as e:
        print("[ERROR] " + str(e), file=sys.stderr); return 2
    except Exception:
        print("[ERROR] Unexpected failure for {}:".format(package_id), file=sys.stderr)
        traceback.print_exc(); return 1
    return _print_package_result(result)


def _print_package_result(result):
    print()
    print("=== Phase 5B summary ({}) ===".format(result.package_id))
    env = result.env_info
    print("xelatex          : {} ({})".format(env.get("path"), env.get("version")))
    for pkg in ("polyglossia", "xepersian", "fontspec", "graphicx", "geometry", "hyperref"):
        print("{:17s}: {}".format(pkg, "yes" if env.get(pkg) else "no"))
    print("cascade          : {}  (final fallback: {})".format(result.cascade, result.final_fallback))
    print("chosen font      : {}".format(result.chosen_font))
    print("expected images  : {}".format(result.expected_images))
    print("image anchors MD : {}".format(result.image_anchors_md))
    print("image anchors TeX: {}".format(result.image_anchors_tex))
    print("image-first OK   : {}".format(result.image_anchors_first_in_block))
    print("attempts         : {}/{}".format(result.attempts, result.max_attempts))
    if result.fix_notes:
        for n in result.fix_notes: print("  fix: " + n)
    print(".tex written     : {}".format(result.tex_path))
    print("PDF path         : {}".format(result.pdf_path))
    print("PDF bytes        : {}".format(result.pdf_bytes))
    if result.success:
        print("[Phase 5B] BUILD {}: pass".format(result.package_id)); return 0
    print("[Phase 5B] BUILD {}: FAIL".format(result.package_id))
    for br in result.build_history:
        print("  build rcs={}  errors={}".format(br.return_codes, len(br.errors)))
        for e in br.errors[:5]: print("    {}: {}".format(e.kind, e.line[:160]))
    return 3


def _run_all(run_dir, max_attempts, skip_existing):
    print("[Phase 5C] Build all packages in: {}".format(run_dir))
    print("  skip_existing: {}".format(skip_existing))
    print("  max_attempts : {}".format(max_attempts))
    print()
    try:
        agg = run_all_packages(run_dir, PROJECT_ROOT,
                               max_attempts=max_attempts,
                               skip_existing=skip_existing)
    except Exception:
        print("[ERROR] Unexpected failure in --all run:", file=sys.stderr)
        traceback.print_exc(); return 1
    print()
    print("=== Phase 5C per-package summary ===")
    for r in agg.results:
        flag = "pass" if r.success else "FAIL"
        notes = ""
        if r.fix_notes:
            notes = "  fix: " + " | ".join(n[:80] for n in r.fix_notes)
        print("  {}: K={}  attempts={}/{}  bytes={}  {}{}".format(
            r.package_id, r.image_anchors_md, r.attempts, r.max_attempts,
            r.pdf_bytes, flag, notes,
        ))
    print()
    print("[Phase 5C] {}/{} packages built successfully".format(agg.n_success, agg.n_packages))
    return 0 if agg.success else 3


if __name__ == "__main__":
    sys.exit(main())
