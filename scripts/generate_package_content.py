"""generate_package_content — Phase 4B CLI (K-page generic).

Usage:
  python scripts/generate_package_content.py --run runs/001_1_20260520_2128 --package package_001
  python scripts/generate_package_content.py --dry-run-template-check

The `--dry-run-template-check` flag exercises the K-page generic renderer
on synthetic 1/2/4-page packages in-memory and reports whether each renders
correctly and validates against the schema. No files are written, no
real package content is generated.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.persian_content_agent import (  # noqa: E402
    _CURATED_CONTENT,
    dry_run_template_check,
    run_content_generation,
)


def _print_dry_run(summary: dict) -> int:
    print("=== Dry-run template check ===")
    print()
    bad = 0
    for k_label in sorted(summary):
        if not k_label.startswith("k"):
            continue
        info = summary[k_label]
        ok_sections = info["actual_sections"] == info["expected_sections"]
        ok_anchors = info["image_anchors"] >= int(k_label[1:])
        ok_schema = info["schema_valid"]
        ok = ok_sections and ok_anchors and ok_schema
        if not ok:
            bad += 1
        print(f"  K={k_label[1:]:>1s}  sections={info['actual_sections']}/{info['expected_sections']} "
              f" anchors={info['image_anchors']}  schema_ok={ok_schema}  "
              f"=> {'pass' if ok else 'FAIL'}")
        if info.get("schema_errors") and info["schema_errors"] != ["validator-unavailable"]:
            for e in info["schema_errors"][:5]:
                print(f"     ! {e}")
    if summary.get("validator_unavailable"):
        print()
        print(f"  (jsonschema not available: {summary['validator_unavailable']})")
    return 0 if bad == 0 else 7


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_package_content",
        description="Phase 4B: K-page generic Persian extended-content generator.",
    )
    parser.add_argument(
        "--dry-run-template-check",
        action="store_true",
        help="Render synthetic 1/2/4-page packages in-memory and report validity. No files written.",
    )
    parser.add_argument("--run", help="Path to runs/<run_id>/")
    parser.add_argument(
        "--package",
        default=None,
        help="Package id to generate content for (must be in _CURATED_CONTENT). "
             f"Currently available: {sorted(_CURATED_CONTENT.keys())}.",
    )
    args = parser.parse_args(argv)

    if args.dry_run_template_check:
        return _print_dry_run(dry_run_template_check())

    if not args.run or not args.package:
        parser.error("--run and --package are required unless --dry-run-template-check is set")

    run_dir = (
        Path(args.run).resolve() if Path(args.run).is_absolute()
        else (PROJECT_ROOT / args.run).resolve()
    )
    if not run_dir.exists() or not run_dir.is_dir():
        print(f"[ERROR] run folder not found: {run_dir}", file=sys.stderr)
        return 2

    # Accept a package if it has run-specific authored content (generic path) OR
    # is a file-1 curated sample. run_content_generation enforces the rest.
    _authored = run_dir / "content_authored" / f"{args.package}.json"
    if args.package not in _CURATED_CONTENT and not _authored.exists():
        parser.error(
            f"No content source for {args.package!r}: provide "
            f"content_authored/{args.package}.json or use a file-1 curated package "
            f"({sorted(_CURATED_CONTENT.keys())})."
        )

    print(f"[Stage 4] Persian extended content for {args.package} in: {run_dir}")
    try:
        r = run_content_generation(run_dir, args.package)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 3
    except Exception:
        print("[ERROR] Unexpected failure:", file=sys.stderr)
        traceback.print_exc()
        return 1

    print()
    print("=== Phase 4 summary ===")
    print(f"markdown : {r.md_path}")
    print(f"json     : {r.json_path}")
    print(f"qa report: {r.report_path}")
    print()
    print(f"K (pages)     : {r.page_count}")
    print(f"sections (## ): {r.section_count}  (expected: {10 + r.page_count})")
    print(f"image anchors : {r.image_anchor_count}")
    print(f"md token count: {r.word_count_fa}")
    print()
    print(f"Stopped cleanly. Available packages: {sorted(_CURATED_CONTENT.keys())}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
