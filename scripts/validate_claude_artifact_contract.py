"""validate_claude_artifact_contract - honestly report which Claude artifacts
exist / are missing for a run. Read-only. No pipeline, no fabrication.

Usage: python scripts/validate_claude_artifact_contract.py --run runs/<id>
Exit 0 always (it is a status report, not a gate).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.claude_artifact_contract import artifact_status  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="validate_claude_artifact_contract")
    ap.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    run_dir = Path(args.run) if Path(args.run).is_absolute() else (ROOT / args.run)
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run not found: {run_dir}", file=sys.stderr); return 2

    st = artifact_status(run_dir)
    print(f"=== Claude artifact contract — {st['run_id']} ===")
    present = missing = 0
    for r in st["artifacts"]:
        if r["exists"]:
            present += 1
            flag = "OK" if r["claude_authored"] else "EXISTS (not Claude-authored)"
            extra = f"schema={r['schema']} prov={r['provenance_ok']} src_pages={r['source_pages']} unc={r['uncertainty']}"
            print(f"  [{flag:>26}] {r['stage']:<16} {r['path']}  ({extra})")
        else:
            missing += 1
            print(f"  [{'MISSING':>26}] {r['stage']:<16} {r['path']}")
    print(f"SUMMARY: {present} present / {missing} missing (of {len(st['artifacts'])}).")
    print("Note: Python may NOT invent missing intelligence artifacts; author them as CLAUDE_CODE_MANUAL_AGENT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
