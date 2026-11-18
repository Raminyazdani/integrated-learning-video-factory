"""Create and extract an original, one-page lesson in a fresh local directory."""

import argparse
from pathlib import Path
import sys
from tempfile import mkdtemp

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New directory for the lesson and output")
    args = parser.parse_args()
    if args.output is None:
        workspace = Path(mkdtemp(prefix="lesson-extraction-"))
    else:
        workspace = args.output.resolve()
        try:
            workspace.mkdir(parents=True)
        except FileExistsError:
            parser.error("Choose a new output directory.")

    source = workspace / "input" / "tile-lesson.pdf"
    source.parent.mkdir()
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((48, 60), "Counting a rectangle")
        page.insert_text((48, 84), "Three rows of four tiles cover twelve squares.")
        for row in range(3):
            for column in range(4):
                x = 48 + 24 * column
                y = 105 + 24 * row
                page.draw_rect(pymupdf.Rect(x, y, x + 22, y + 22), color=(0.1, 0.3, 0.5))
        document.save(source)

    result = Orchestrator(workspace).run(source.name, stop_after="extraction", dpi=72)
    extracted = result.intake.run_dir / "extracted" / "page_text" / "page_001.txt"
    print(extracted.read_text(encoding="utf-8").strip())
    return 0 if result.extraction.pages_with_text == 1 and not result.extraction.pages_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
