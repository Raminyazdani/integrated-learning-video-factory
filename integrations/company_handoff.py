"""company_handoff - durable handoff + done.json files for company tasks.

Each task writes a handoff (what it used/produced/uncertainty/provenance) and a
done.json (status + sha) that join gates count. No teaching logic here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def handoff_path(run_dir, task_id: str) -> Path:
    return Path(run_dir) / "company_workflow" / "handoffs" / f"{task_id}.handoff.json"


def done_path(run_dir, task_id: str) -> Path:
    return Path(run_dir) / "company_workflow" / "done" / f"{task_id}.done.json"


def write_handoff(run_dir, *, task_id, agent_name, role_type, status, inputs_used,
                  outputs, authored_by, uncertainty=None, reviewer=None,
                  gate_decision=None, source_sha=None) -> Path:
    obj = {
        "task_id": task_id, "agent_name": agent_name, "role_type": role_type,
        "status": status, "inputs_used": list(inputs_used or []),
        "outputs": list(outputs or []), "uncertainty": list(uncertainty or []),
        "reviewer": reviewer, "gate_decision": gate_decision, "retries_used": 0,
        "provenance": {"authored_by": authored_by, "source_sha": source_sha, "timestamp": _iso()},
    }
    p = handoff_path(run_dir, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_done(run_dir, *, task_id, status, sha=None, uncertainty=None) -> Path:
    obj = {"task_id": task_id, "status": status, "sha": sha,
           "uncertainty": list(uncertainty or []), "timestamp": _iso()}
    p = done_path(run_dir, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_done(run_dir, task_id) -> Optional[dict]:
    p = done_path(run_dir, task_id)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def validate_handoff(obj: dict, schema_path) -> tuple:
    try:
        import jsonschema
        jsonschema.validate(obj, json.loads(Path(schema_path).read_text(encoding="utf-8")))
        return True, "PASS"
    except ImportError:
        return True, "jsonschema not installed"
    except Exception as e:
        return False, str(e)[:160]
