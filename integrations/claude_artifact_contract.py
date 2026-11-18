"""Validate schema and declared Claude provenance for run-specific artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = PROJECT_ROOT / "schemas"
CLAUDE_PROVENANCE = {"CLAUDE_CODE_MANUAL_AGENT", "CLAUDE_API"}

# stage -> (relative path template, schema, per_package)
ARTIFACTS = [
    ("page_understanding", "classification/claude_page_review.json", "claude_page_review.schema.json", False),
    ("package_plan", "packages/claude_package_plan.json", "claude_package_plan.schema.json", False),
    ("package_metadata", "packages/claude_package_metadata.json", "claude_package_metadata.schema.json", False),
    ("content", "content_authored/{pkg}.json", "content_authored.schema.json", True),
    ("slide_spec", "slide_specs/{pkg}_slide_spec.json", "slide_spec.schema.json", True),
    ("narration", "narration_scripts/claude_narration.json", "claude_narration.schema.json", False),
    ("gemini_tts_text", "narration_scripts/gemini_tts_text.json", "gemini_tts_text.schema.json", False),
]


class ClaudeArtifactError(RuntimeError):
    pass


def _read(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _packages(run_dir) -> List[str]:
    d = _read(Path(run_dir) / "packages" / "package_manifest.json")
    if isinstance(d, dict):
        return [p["package_id"] for p in d.get("packages", [])]
    return []


def _schema_ok(obj, schema_name) -> tuple:
    try:
        import jsonschema
    except Exception:
        return False, "jsonschema unavailable"
    sp = SCHEMAS / schema_name
    if not sp.is_file():
        return False, f"schema {schema_name} absent"
    try:
        jsonschema.validate(obj, json.loads(sp.read_text(encoding="utf-8")))
        return True, "schema PASS"
    except Exception as e:
        return False, f"schema FAIL: {str(e)[:120]}"


def _provenance_authored_by(obj) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    prov = obj.get("provenance")
    if isinstance(prov, dict):
        return prov.get("authored_by")
    if isinstance(prov, str):
        return prov
    return obj.get("authored_by")


def _inspect(run_dir: Path, path_rel: str, schema_name: str) -> tuple[dict, object]:
    p = run_dir / path_rel
    st = {"path": path_rel, "exists": p.is_file(), "schema": None,
          "provenance_ok": False, "source_pages": False, "uncertainty": False, "claude_authored": False}
    if not st["exists"]:
        return st, None
    obj = _read(p)
    sok, smsg = _schema_ok(obj, schema_name)
    st["schema"] = smsg
    ab = _provenance_authored_by(obj)
    st["provenance_ok"] = isinstance(ab, str) and ab in CLAUDE_PROVENANCE
    st["source_pages"] = bool(isinstance(obj, dict) and obj.get("source_pages"))
    st["uncertainty"] = bool(isinstance(obj, dict) and (obj.get("uncertainty_notes") or obj.get("uncertainty")))
    st["claude_authored"] = bool(sok and st["provenance_ok"])
    return st, obj


def artifact_status(run_dir) -> dict:
    run_dir = Path(run_dir).resolve()
    pkgs = _packages(run_dir)
    rows = []
    for stage, tmpl, schema, per_pkg in ARTIFACTS:
        targets = [tmpl.format(pkg=pk) for pk in (pkgs or ["package_001"])] if per_pkg else [tmpl]
        for target in targets:
            status, _ = _inspect(run_dir, target, schema)
            rows.append({**status, "stage": stage})
    return {"run_id": run_dir.name, "packages": pkgs, "artifacts": rows}


def load_claude_authored(stage: str, run_dir, *, package_id: str = None) -> dict:
    """Return the validated artifact so renderers consume the data that was checked."""
    run_dir = Path(run_dir).resolve()
    entry = next((a for a in ARTIFACTS if a[0] == stage), None)
    if entry is None:
        raise ClaudeArtifactError(f"unknown intelligence stage {stage!r}")
    _, tmpl, schema, per_pkg = entry
    rel = tmpl.format(pkg=(package_id or "package_001")) if per_pkg else tmpl
    st, obj = _inspect(run_dir, rel, schema)
    if not st["exists"]:
        raise ClaudeArtifactError(
            f"[{stage}] missing Claude artifact {rel!r}. Python may NOT invent it. "
            f"Author it (CLAUDE_CODE_MANUAL_AGENT) before this stage can run.")
    if not st["claude_authored"]:
        raise ClaudeArtifactError(
            f"[{stage}] {rel!r} exists but is not Claude-authored (provenance={st['provenance_ok']}, {st['schema']}). "
            f"Heuristic/Python output is a candidate only, not the final artifact.")
    return obj


def assert_claude_authored(stage: str, run_dir, *, package_id: str = None) -> str:
    """Raise when the artifact is missing or fails its schema or provenance check."""
    load_claude_authored(stage, run_dir, package_id=package_id)
    return "CLAUDE"
