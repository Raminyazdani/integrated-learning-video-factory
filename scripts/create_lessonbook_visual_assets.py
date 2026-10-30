"""create_lessonbook_visual_assets — realize a Claude-authored visual asset plan.

Python executor only: crops/annotates/masks source page images per the plan and
writes a `lessonbook_visual_asset_manifest`. Decides no educational meaning.
No TTS / ffmpeg / video / paid / network. PIL only.

Usage:
  python scripts/create_lessonbook_visual_assets.py --plan runs/<id>/.../package_002_visual_asset_plan.json
  python scripts/create_lessonbook_visual_assets.py --smoke
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.lessonbook_visual_assets import build_assets_from_plan, smoke_assets  # noqa: E402


def _write(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_smoke_run() -> Path:
    """First run that has extracted page PNGs (project-agnostic smoke; no hardcoded run)."""
    for p in sorted((ROOT / "runs").glob("*/extracted/pages")):
        if p.is_dir():
            return p.parent.parent
    raise SystemExit("[visual-smoke] no run with extracted/pages found; pass --run-dir <run>")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build lessonbook visual assets from a plan (PIL executor).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", type=str, help="path to a lessonbook_visual_asset_plan.json")
    g.add_argument("--smoke", action="store_true", help="Part B visual smoke (page 6) -> lessonbook_visual_smoke/")
    ap.add_argument("--run-dir", type=str, default=None)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--manifest", type=str, default=None)
    args = ap.parse_args()

    if args.smoke:
        run_dir = Path(args.run_dir) if args.run_dir else _default_smoke_run()
        out_dir = Path(args.out) if args.out else (run_dir / "lessonbook_visual_smoke")
        manifest = smoke_assets(run_dir, out_dir)
        mpath = Path(args.manifest) if args.manifest else (out_dir / "visual_asset_manifest.json")
        _write(manifest, mpath)
        print(f"[visual-smoke] assets={len(manifest['assets'])} dir={out_dir}")
        print(f"[visual-smoke] manifest={mpath}")
        return 0

    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run_dir = Path(args.run_dir) if args.run_dir else plan_path.parent.parent
    out_dir = Path(args.out) if args.out else (plan_path.parent / "visual_assets")
    manifest = build_assets_from_plan(plan, run_dir, out_dir)
    mpath = Path(args.manifest) if args.manifest else (plan_path.parent / plan_path.name.replace("_plan.json", "_manifest.json"))
    if mpath == plan_path:
        mpath = plan_path.parent / "visual_asset_manifest.json"
    _write(manifest, mpath)
    print(f"[visual] assets={len(manifest['assets'])} dir={out_dir}")
    print(f"[visual] manifest={mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
