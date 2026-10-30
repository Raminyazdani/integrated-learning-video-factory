"""company_gate - join-gate logic. A downstream stage cannot unlock until its
upstream join gate is APPROVED (collected == expected AND supervisor PASS).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from integrations.company_handoff import read_done


def gates_from_graph(graph: dict) -> List[dict]:
    """Derive gate objects from supervisor tasks that carry a join_gate_id."""
    tasks = graph.get("tasks", [])
    by_group: Dict[str, List[str]] = {}
    for t in tasks:
        g = t.get("parallel_group_id")
        if g and t.get("role_type") != "supervisor":
            by_group.setdefault(g, []).append(t["task_id"])
    gates = []
    for t in tasks:
        if t.get("role_type") == "supervisor" and t.get("join_gate_id"):
            grp = t.get("parallel_group_id")
            members = by_group.get(grp, [])
            unlocks = [x["task_id"] for x in tasks if t["task_id"] in x.get("dependencies", [])]
            gates.append({
                "gate_id": t["join_gate_id"], "kind": "join_gate",
                "parallel_group_id": grp, "member_task_ids": members,
                "expected_count": len(members), "collected_count": 0,
                "status": "open", "reviewer_agent": t.get("agent_name"),
                "unlocks_tasks": unlocks, "decision": None, "reasons": [],
            })
    return gates


def evaluate_gate(run_dir, gate: dict) -> dict:
    """Count done.json for members; set status. Does NOT fabricate approvals —
    a real supervisor must record PASS/FAIL in each member's done.json."""
    members = gate.get("member_task_ids", [])
    dones = [read_done(run_dir, m) for m in members]
    collected = [d for d in dones if d is not None]
    gate["collected_count"] = len(collected)
    if len(collected) < len(members):
        gate["status"] = "collecting"
        gate["reasons"] = [f"{len(collected)}/{len(members)} member done.json present"]
        return gate
    statuses = [d.get("status") for d in collected]
    if all(s == "pass" for s in statuses):
        gate["status"] = "ready"  # ready for supervisor PASS; orchestrator promotes to pass
        gate["reasons"] = ["all members done + pass"]
    else:
        gate["status"] = "fail"
        gate["reasons"] = [f"members not all pass: {statuses}"]
    return gate


def can_unlock(gate: dict) -> bool:
    """Downstream may start ONLY when the gate is PASS."""
    return gate.get("status") == "pass"


def save_gates(run_dir, gates: list) -> Path:
    p = Path(run_dir) / "company_workflow" / "gates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(gates, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
