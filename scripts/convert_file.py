"""convert_file — Phase 2 CLI entry point.

Usage:
  python scripts/convert_file.py 1
  python scripts/convert_file.py 1 --stop-after extraction
  python scripts/convert_file.py 1 --stop-after intake --dpi 150

In Phase 2 the pipeline can only run Stage 0 (input_intake) and Stage 1
(pdf_extraction); --stop-after is effectively required and defaults to
'extraction'. Future phases will accept stop-after values like
'classification', 'packages', 'content', 'pdfs', 'slides', 'narration',
'audio', 'video'.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# Make the project root importable as the "project root" without packaging.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.orchestrator import Orchestrator  # noqa: E402


PHASE2_STOP_CHOICES = ["intake", "extraction"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="convert_file",
        description=(
            "Phase 2 CLI: locate input/N.pdf, allocate a run, render pages "
            "to PNG and extract text. No content/slides/narration/TTS/video."
        ),
    )
    parser.add_argument(
        "file_arg",
        help="File number or name, e.g. '1' to resolve input/1.pdf.",
    )
    parser.add_argument(
        "--stop-after",
        default="extraction",
        choices=PHASE2_STOP_CHOICES,
        help="Halt after this stage. Phase 2 only supports intake|extraction. "
             "(Default: extraction.)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="PNG render DPI (default 200, matches configs/pipeline_config.yaml).",
    )
    args = parser.parse_args(argv)

    orch = Orchestrator(PROJECT_ROOT)
    try:
        result = orch.run(
            args.file_arg,
            stop_after=args.stop_after,
            dpi=args.dpi,
        )
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        # e.g. PyMuPDF missing
        print(f"[ERROR] {e}", file=sys.stderr)
        return 4
    except Exception:
        print("[ERROR] Unexpected failure:", file=sys.stderr)
        traceback.print_exc()
        return 1

    print()
    print("=== Phase 2 summary ===")
    print(f"run_id       : {result.intake.run_id}")
    print(f"run_dir      : {result.intake.run_dir}")
    print(f"source       : {result.intake.source_path}")
    print(f"source_copy  : {result.intake.source_copy}")
    if result.extraction is not None:
        print(f"page_count   : {result.extraction.page_count}")
        print(f"manifest     : {result.extraction.manifest_path}")
        print(f"qa_report    : {result.extraction.report_path}")
    print(f"run_state    : {result.run_state_path}")
    print(f"project_ptr  : {result.project_state_path}")
    print()
    print("Stopped cleanly. Phase 3 is not implemented; do NOT pass "
          "--stop-after values beyond 'extraction' yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
