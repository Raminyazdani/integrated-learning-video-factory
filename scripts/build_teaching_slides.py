"""build_teaching_slides - Phase 6 CLI for the PPTX/slides builder.

Phase 6A: --smoke-test
Phase 6B: --package <id>
Phase 6C: --packages <id1> <id2> ...
Phase 6D: --all [--merge]
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.slide_designer_agent import (  # noqa: E402
    run_smoke_slides, run_package_slides, run_all_packages_slides,
)
from integrations.slide_image_exporter import export_run_slide_images  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_teaching_slides",
        description="Phase 6 teaching-slides builder.",
    )
    parser.add_argument("--run", required=True, help="Path to runs/<run_id>/")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Phase 6A: minimal smoke PPTX.")
    parser.add_argument("--package", help="Phase 6B: build one per-package deck.")
    parser.add_argument("--packages", nargs="+",
                        help="Phase 6C: build per-package decks for multiple package_ids.")
    parser.add_argument("--all", action="store_true",
                        help="Phase 6D: build decks for every content_md/package_NNN_*.md.")
    parser.add_argument("--merge", action="store_true",
                        help="Phase 6D: also merge all decks into slides/full_teaching_deck.pptx.")
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="Phase 6D: rebuild per-package decks even if present.")
    parser.add_argument("--export-images", action="store_true",
                        help="Native slide-image export: render the merged deck to "
                             "slide_images/full_teaching_deck-NNN.png and relink slide_manifest.json. "
                             "Runs standalone (no deck rebuild), and also runs automatically after --all --merge.")
    parser.add_argument("--no-export-images", action="store_true",
                        help="Skip the automatic slide-image export after --all --merge.")
    parser.add_argument("--force", action="store_true",
                        help="Force slide-image re-render even if existing images match slide_count.")
    parser.add_argument("--dpi", type=int, default=200, help="Slide-image render DPI (default 200; Q1 readability).")
    args = parser.parse_args(argv)

    modes = sum([bool(args.smoke_test), bool(args.package), bool(args.packages),
                 bool(args.all), bool(args.export_images)])
    if modes == 0:
        parser.error("Specify --smoke-test, --package, --packages, --all, or --export-images.")
    # --export-images may stand alone OR modify --all; it is not exclusive with --all.
    build_modes = sum([bool(args.smoke_test), bool(args.package), bool(args.packages), bool(args.all)])
    if build_modes > 1:
        parser.error("--smoke-test / --package / --packages / --all are mutually exclusive.")
    if args.merge and not args.all:
        parser.error("--merge only applies with --all.")

    run_dir = Path(args.run).resolve() if Path(args.run).is_absolute() else (PROJECT_ROOT / args.run).resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        print("[ERROR] run folder not found: " + str(run_dir), file=sys.stderr)
        return 2

    if args.smoke_test:
        return _run_smoke(run_dir)
    if args.package:
        return _run_package(run_dir, args.package)
    if args.packages:
        return _run_packages(run_dir, args.packages)
    if args.all:
        rc = _run_all(run_dir, merge=args.merge, skip_existing=not args.no_skip_existing)
        # Native slide-image export is a first-class part of building the merged deck.
        if rc == 0 and args.merge and not args.no_export_images:
            rc = _run_export(run_dir, dpi=args.dpi, force=args.force)
        return rc
    # standalone --export-images
    return _run_export(run_dir, dpi=args.dpi, force=args.force)


def _run_export(run_dir, *, dpi=150, force=False):
    print("[Phase 6E] Native slide-image export in: " + str(run_dir))
    try:
        r = export_run_slide_images(run_dir, dpi=dpi, force=force)
    except Exception:
        traceback.print_exc(); return 1
    print()
    print("=== Phase 6E slide-image export ===")
    print("  backend          : {}".format(r.backend or "(none)"))
    print("  pdf              : {}".format(r.pdf_path))
    print("  images_dir       : {}".format(r.images_dir))
    print("  slide_count      : {}".format(r.slide_count))
    print("  image_count      : {}".format(r.image_count))
    print("  reused           : {}".format(r.reused))
    print("  relinked         : {} / {}".format(r.relinked, r.slide_count))
    if r.missing_image_paths:
        print("  missing          : {}".format(r.missing_image_paths[:5]))
    print("  report           : {}".format(r.report_path))
    if r.error:
        print("  error            : {}".format(r.error))
    if r.success:
        print("[Phase 6E] SLIDE-IMAGE EXPORT: pass"); return 0
    print("[Phase 6E] SLIDE-IMAGE EXPORT: FAIL"); return 3


def _run_smoke(run_dir):
    print("[Phase 6A] PPTX smoke test in: " + str(run_dir))
    try:
        r = run_smoke_slides(run_dir, PROJECT_ROOT)
    except Exception:
        traceback.print_exc(); return 1
    print()
    if r.success:
        print("[Phase 6A] SMOKE TEST: pass (pptx_bytes={}, pdf_bytes={})".format(r.pptx_bytes, r.pdf_bytes))
        return 0
    print("[Phase 6A] SMOKE TEST: FAIL"); return 3


def _run_package(run_dir, package_id):
    print("[Phase 6B] Build " + package_id)
    try:
        r = run_package_slides(run_dir, PROJECT_ROOT, package_id)
    except FileNotFoundError as e:
        print("[ERROR] " + str(e), file=sys.stderr); return 2
    except Exception:
        traceback.print_exc(); return 1
    return _print_package_result(r, phase="6B")


def _print_package_result(r, *, phase="6B"):
    print()
    print("=== Phase {} summary ({}) ===".format(phase, r.package_id))
    print("slide count         : {}".format(r.slide_count))
    print("pptx                : {} ({} bytes)".format(r.pptx_path, r.pptx_bytes))
    print("zip valid / req     : {} / {}".format(r.pptx_zip_ok, r.pptx_required_members_present))
    print("manifest            : {} (schema_ok={})".format(r.manifest_path, r.manifest_schema_valid))
    print("pdf                 : {} ({} bytes)".format(r.pdf_path, r.pdf_bytes))
    if r.notes:
        for n in r.notes: print("  note: " + n)
    if r.success and r.manifest_schema_valid:
        print("[Phase {}] BUILD {}: pass".format(phase, r.package_id)); return 0
    print("[Phase {}] BUILD {}: FAIL".format(phase, r.package_id)); return 3


def _run_packages(run_dir, package_ids):
    print("[Phase 6C] Build {} packages".format(len(package_ids)))
    results = []
    n_fail = 0
    for pid in package_ids:
        try:
            r = run_package_slides(run_dir, PROJECT_ROOT, pid)
        except Exception:
            traceback.print_exc(); n_fail += 1; continue
        results.append(r)
        rc = _print_package_result(r, phase="6C")
        if rc != 0: n_fail += 1
    print()
    print("=== Phase 6C aggregate ===")
    for r in results:
        flag = "pass" if (r.success and r.manifest_schema_valid) else "FAIL"
        print("  {}: slides={}  pptx={}b  pdf={}b  {}".format(
            r.package_id, r.slide_count, r.pptx_bytes, r.pdf_bytes, flag))
    print("[Phase 6C] {}/{} ok".format(len(results) - n_fail, len(package_ids)))
    return 0 if n_fail == 0 else 3


def _run_all(run_dir, *, merge, skip_existing):
    print("[Phase 6D] All-packages slide build (merge={}, skip_existing={})".format(merge, skip_existing))
    try:
        r = run_all_packages_slides(run_dir, PROJECT_ROOT,
                                    merge=merge, skip_existing=skip_existing)
    except Exception:
        traceback.print_exc(); return 1

    print()
    print("=== Phase 6D per-package ===")
    for pr in r.per_package_results:
        flag = "built" if pr.get("built") else "skipped (existed)"
        print("  {}: {}  slides={}".format(pr["package_id"], flag, pr["slide_count"]))
    print()
    print("=== Phase 6D merged deck ===")
    print("  packages           : {}".format(r.n_packages))
    print("  newly built        : {}".format(r.n_built))
    print("  merged pptx        : {} ({} bytes)".format(r.merged_pptx_path, r.merged_pptx_bytes))
    print("  merged slide count : {}".format(r.merged_slide_count))
    print("  combined manifest  : {}".format(r.combined_manifest_path))
    print("  manifest schema OK : {}".format(r.combined_manifest_schema_valid))
    if r.combined_manifest_schema_errors:
        for e in r.combined_manifest_schema_errors: print("    schema err: " + e)
    print("  merged pdf         : {} ({} bytes)".format(r.merged_pdf_path, r.merged_pdf_bytes))
    if r.notes:
        for n in r.notes: print("  note: " + n)
    print()
    if r.success:
        print("[Phase 6D] ALL+MERGE: pass"); return 0
    print("[Phase 6D] ALL+MERGE: FAIL"); return 3


if __name__ == "__main__":
    sys.exit(main())
