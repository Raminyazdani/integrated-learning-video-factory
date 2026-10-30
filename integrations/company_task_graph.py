"""company_task_graph - file-based DAG of company tasks (queens/leads/workers/supervisors).

Pure coordination/validation. No teaching logic, no pipeline execution. The graph is
the contract the Queen Orchestrator follows: parallel groups, join gates, supervisor
reviewers, dependencies, status, retries, provenance.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROLE_TYPES = {"queen", "lead", "worker", "subworker", "supervisor"}
STATUSES = {"pending", "running", "pass", "fail", "blocked", "retry"}
WORKER_ROLES = {"lead", "worker", "subworker"}


@dataclass
class Task:
    task_id: str
    department: str
    agent_name: str
    role_type: str
    specialty: str
    parent_task_id: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    parallel_group_id: Optional[str] = None
    join_gate_id: Optional[str] = None
    input_artifacts: List[str] = field(default_factory=list)
    output_artifacts: List[str] = field(default_factory=list)
    handoff_path: str = ""
    reviewer_agent: Optional[str] = None
    supervisor_gate: Optional[str] = None
    status: str = "pending"
    max_retries: int = 2
    uncertainty_notes: List[str] = field(default_factory=list)
    provenance: dict = field(default_factory=lambda: {"authored_by": "pending", "source_sha": None, "timestamp": None})

    def to_dict(self) -> dict:
        return asdict(self)


def save_graph(graph: dict, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def load_graph(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _has_cycle(tasks: List[dict]) -> Optional[list]:
    ids = {t["task_id"] for t in tasks}
    adj = {t["task_id"]: [d for d in t.get("dependencies", []) if d in ids] for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}
    stack = []

    def dfs(u):
        color[u] = GRAY; stack.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                return stack[stack.index(v):] + [v]
            if color[v] == WHITE:
                c = dfs(v)
                if c:
                    return c
        color[u] = BLACK; stack.pop()
        return None
    for i in ids:
        if color[i] == WHITE:
            c = dfs(i)
            if c:
                return c
    return None


def validate_graph(graph: dict) -> Tuple[bool, List[str]]:
    """Structural + policy validation. Returns (ok, errors)."""
    errors: List[str] = []
    tasks = graph.get("tasks", [])
    ids = [t["task_id"] for t in tasks]

    # unique ids
    dups = {i for i in ids if ids.count(i) > 1}
    if dups:
        errors.append(f"duplicate task_id(s): {sorted(dups)}")
    idset = set(ids)

    # role/status validity + dependency existence
    for t in tasks:
        if t.get("role_type") not in ROLE_TYPES:
            errors.append(f"{t['task_id']}: bad role_type {t.get('role_type')!r}")
        if t.get("status") not in STATUSES:
            errors.append(f"{t['task_id']}: bad status {t.get('status')!r}")
        for d in t.get("dependencies", []):
            if d not in idset:
                errors.append(f"{t['task_id']}: dependency {d!r} not found")
        # every worker-type task needs a reviewer + supervisor gate
        if t.get("role_type") in WORKER_ROLES:
            if not t.get("reviewer_agent"):
                errors.append(f"{t['task_id']}: worker has no reviewer_agent")
            if not t.get("supervisor_gate"):
                errors.append(f"{t['task_id']}: worker has no supervisor_gate")

    # DAG
    cyc = _has_cycle(tasks)
    if cyc:
        errors.append(f"dependency cycle: {' -> '.join(cyc)}")

    # every parallel group must have a join-gate supervisor task covering it
    groups = {t.get("parallel_group_id") for t in tasks if t.get("parallel_group_id")}
    gate_tasks = [t for t in tasks if t.get("role_type") == "supervisor" and t.get("join_gate_id")]
    covered = {g.get("parallel_group_id") for g in gate_tasks}
    for grp in groups:
        if grp not in covered:
            errors.append(f"parallel_group {grp!r} has no join-gate supervisor task")
    # each join gate must depend on ALL its group members
    by_group: Dict[str, List[str]] = {}
    for t in tasks:
        g = t.get("parallel_group_id")
        if g and t.get("role_type") != "supervisor":
            by_group.setdefault(g, []).append(t["task_id"])
    gate_by_group = {g["parallel_group_id"]: g for g in gate_tasks}
    for grp, members in by_group.items():
        gate = gate_by_group.get(grp)
        if gate:
            missing = [m for m in members if m not in gate.get("dependencies", [])]
            if missing:
                errors.append(f"join gate {gate['task_id']} missing member deps: {missing}")

    # no downstream task may depend directly on a parallel member (must depend on the gate)
    member_to_group = {m: g for g, ms in by_group.items() for m in ms}
    gate_ids = {g["task_id"] for g in gate_tasks}
    for t in tasks:
        if t["task_id"] in gate_ids:
            continue
        for d in t.get("dependencies", []):
            grp = member_to_group.get(d)
            if grp and t.get("parallel_group_id") != grp:
                errors.append(f"{t['task_id']} depends directly on parallel member {d} "
                              f"(must depend on group {grp}'s join gate)")

    return (len(errors) == 0, errors)


def validate_schema(graph: dict, schema_path) -> Tuple[bool, str]:
    try:
        import jsonschema
    except Exception:
        return True, "jsonschema not installed (schema check skipped)"
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(graph, schema)
        return True, "schema PASS"
    except Exception as e:
        return False, f"schema FAIL: {str(e)[:160]}"


def summary(graph: dict) -> dict:
    tasks = graph.get("tasks", [])
    groups = sorted({t.get("parallel_group_id") for t in tasks if t.get("parallel_group_id")})
    gates = sorted({t.get("join_gate_id") for t in tasks if t.get("join_gate_id")})
    by_role = {}
    for t in tasks:
        by_role[t["role_type"]] = by_role.get(t["role_type"], 0) + 1
    return {"tasks": len(tasks), "parallel_groups": groups, "join_gates": gates, "by_role": by_role}
