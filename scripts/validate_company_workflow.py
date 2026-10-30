"""validate_company_workflow - validate a company task graph (schema + DAG + gates).

Checks: schema; DAG (no cycles); every parallel group has a join gate; every worker
has a reviewer + supervisor gate; no downstream task depends directly on a parallel
member (must depend on the join gate). Read-only. Exit 0 = PASS, 3 = FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.company_task_graph import load_graph, validate_graph, validate_schema, summary  # noqa: E402
from integrations.company_gate import gates_from_graph  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="validate_company_workflow")
    ap.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    run_dir = Path(args.run) if Path(args.run).is_absolute() else (ROOT / args.run)
    run_dir = run_dir.resolve()
    gpath = run_dir / "company_workflow" / "task_graph.json"
    if not gpath.is_file():
        print(f"[ERROR] task_graph.json not found: {gpath}", file=sys.stderr); return 2

    graph = load_graph(gpath)
    s = summary(graph)
    ok, errs = validate_graph(graph)
    sok, smsg = validate_schema(graph, ROOT / "schemas" / "company_task_graph.schema.json")
    gates = gates_from_graph(graph)

    # extra explicit checks for the report
    groups = set(s["parallel_groups"])
    gated_groups = {g["parallel_group_id"] for g in gates}
    ungated = sorted(groups - gated_groups)
    workers = [t for t in graph["tasks"] if t["role_type"] in ("lead", "worker", "subworker")]
    no_review = [t["task_id"] for t in workers if not t.get("reviewer_agent") or not t.get("supervisor_gate")]

    print("=== company workflow validation ===")
    print(f"  tasks={s['tasks']}  parallel_groups={len(groups)}  join_gates={len(s['join_gates'])}  derived_gates={len(gates)}")
    print(f"  schema: {smsg}")
    print(f"  DAG + policy valid: {ok}")
    print(f"  every parallel group has a join gate: {not ungated}" + (f" (ungated: {ungated})" if ungated else ""))
    print(f"  every worker has reviewer+supervisor: {not no_review}" + (f" (missing: {no_review})" if no_review else ""))
    if not ok:
        for e in errs:
            print("  ERR:", e)
    verdict = ok and sok and not ungated and not no_review
    print(f"VERDICT: {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 3


if __name__ == "__main__":
    sys.exit(main())
