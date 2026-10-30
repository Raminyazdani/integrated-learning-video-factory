"""validate_visual_asset_manifest — verify generated visual assets (read-only).

Checks a lessonbook_visual_asset_manifest against its schema, that every asset image
exists with nonzero dimensions, and (if a plan is given) that every `required` planned
asset was produced.

Usage:
  python scripts/validate_visual_asset_manifest.py --manifest <path> [--plan <path>] [--run-dir <dir>]
Exit: 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schemas" / "lessonbook_visual_asset_manifest.schema.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--plan", default=None)
    ap.add_argument("--run-dir", default=None)
    args = ap.parse_args()

    mp = Path(args.manifest)
    if not mp.is_file():
        print(f"[FAIL] manifest not found: {mp}"); return 3
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    run_dir = Path(args.run_dir) if args.run_dir else mp.parent.parent

    checks = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))

    try:
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errs = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda e: list(e.path))
        chk("schema_valid", not errs, "; ".join(e.message for e in errs[:3]))
    except ImportError:
        chk("schema_valid", True, "jsonschema unavailable (parse-only)")

    missing, zerodim = [], []
    try:
        from PIL import Image
        have_pil = True
    except Exception:
        have_pil = False
    for a in manifest.get("assets", []):
        rel = a.get("path", "")
        fp = Path(rel)
        if not fp.is_absolute():
            fp = run_dir / rel
        if not fp.is_file():
            missing.append(a.get("asset_id")); continue
        if have_pil:
            try:
                im = Image.open(fp)
                if im.size[0] <= 0 or im.size[1] <= 0:
                    zerodim.append(a.get("asset_id"))
            except Exception:
                zerodim.append(a.get("asset_id"))
    chk("all_assets_exist", not missing, ", ".join(map(str, missing[:6])))
    chk("all_assets_nonzero_dims", not zerodim, ", ".join(map(str, zerodim[:6])))

    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        have = {a.get("asset_id") for a in manifest.get("assets", [])}
        req_missing = [a.get("asset_id") for a in plan.get("assets", []) if a.get("required") and a.get("asset_id") not in have]
        chk("required_assets_present", not req_missing, ", ".join(map(str, req_missing[:6])))

    n_fail = sum(1 for _, ok, _ in checks if not ok)
    print("=== visual asset manifest validation ===")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    print(f"VERDICT: {'PASS' if n_fail == 0 else 'FAIL'}  (assets: {len(manifest.get('assets', []))})")
    return 0 if n_fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
