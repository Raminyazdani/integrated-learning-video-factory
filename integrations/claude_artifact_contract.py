"""claude_artifact_contract - Claude-authored artifacts are the source of truth.

For NON-file-1 inputs, the intelligence stages (page understanding, package plan,
package metadata, content, slide spec, narration, gemini tts text) MUST be produced
by Claude (provenance authored_by ∈ {CLAUDE_CODE_MANUAL_AGENT, CLAUDE_API}).
Python renderers may only CONSUME them and must fail closed if missing. Heuristic
outputs are candidates only — never the final artifact.

For the file-1 gold sample, the static curated content path remains gated/allowed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = PROJECT_ROOT / "schemas"
CLAUDE_PROVENANCE = {"CLAUDE_CODE_MANUAL_AGENT", "CLAUDE_API"}
_FILE1_NAMES = {"1.pdf", "l01_-_introduction_to_ml.pdf"}

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


def is_file1(run_dir) -> bool:
    for rel in ("packages/package_manifest.json", "extracted/extraction_manifest.json"):
        d = _read(Path(run_dir) / rel)
        if isinstance(d, dict):
            sf = (d.get("source_file") or "").replace("\\", "/").lower().rsplit("/", 1)[-1]
            if sf:
                return sf in _FILE1_NAMES
    return False


def _packages(run_dir) -> List[str]:
    d = _read(Path(run_dir) / "packages" / "package_manifest.json")
    if isinstance(d, dict):
        return [p["package_id"] for p in d.get("packages", [])]
    return []


def _schema_ok(obj, schema_name) -> tuple:
    try:
        import jsonschema
    except Exception:
        return True, "jsonschema not installed"
    sp = SCHEMAS / schema_name
    if not sp.is_file():
        return True, f"schema {schema_name} absent"
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
    # content_authored uses 'authored_by' or nothing
    return obj.get("authored_by")


def _check_one(run_dir: Path, path_rel: str, schema_name: str) -> dict:
    p = run_dir / path_rel
    st = {"path": path_rel, "exists": p.is_file(), "schema": None,
          "provenance_ok": False, "source_pages": False, "uncertainty": False, "claude_authored": False}
    if not p.is_file():
        return st
    obj = _read(p)
    sok, smsg = _schema_ok(obj, schema_name)
    st["schema"] = smsg
    ab = _provenance_authored_by(obj)
    st["provenance_ok"] = ab in CLAUDE_PROVENANCE
    st["source_pages"] = bool(isinstance(obj, dict) and obj.get("source_pages"))
    st["uncertainty"] = bool(isinstance(obj, dict) and (obj.get("uncertainty_notes") or obj.get("uncertainty")))
    st["claude_authored"] = bool(sok and st["provenance_ok"])
    return st


def artifact_status(run_dir) -> dict:
    run_dir = Path(run_dir).resolve()
    pkgs = _packages(run_dir)
    rows = []
    for stage, tmpl, schema, per_pkg in ARTIFACTS:
        if per_pkg:
            targets = [tmpl.format(pkg=pk) for pk in pkgs] or [tmpl.format(pkg="package_001")]
            for t in targets:
                r = _check_one(run_dir, t, schema); r["stage"] = stage; rows.append(r)
        else:
            r = _check_one(run_dir, tmpl, schema); r["stage"] = stage; rows.append(r)
    return {"run_id": run_dir.name, "is_file1": is_file1(run_dir), "packages": pkgs, "artifacts": rows}


def assert_claude_authored(stage: str, run_dir, *, package_id: str = None) -> str:
    """Fail-closed: raise unless the stage's Claude artifact exists and is Claude-authored.
    file-1 content stage is exempt (static curated fixture allowed). Returns provenance."""
    run_dir = Path(run_dir).resolve()
    entry = next((a for a in ARTIFACTS if a[0] == stage), None)
    if entry is None:
        raise ClaudeArtifactError(f"unknown intelligence stage {stage!r}")
    _, tmpl, schema, per_pkg = entry
    if stage == "content" and is_file1(run_dir):
        return "FILE1_CURATED_FIXTURE"  # gated exemption
    rel = tmpl.format(pkg=(package_id or "package_001")) if per_pkg else tmpl
    st = _check_one(run_dir, rel, schema)
    if not st["exists"]:
        raise ClaudeArtifactError(
            f"[{stage}] missing Claude artifact {rel!r}. Python may NOT invent it. "
            f"Author it (CLAUDE_CODE_MANUAL_AGENT) before this stage can run.")
    if not st["claude_authored"]:
        raise ClaudeArtifactError(
            f"[{stage}] {rel!r} exists but is not Claude-authored (provenance={st['provenance_ok']}, {st['schema']}). "
            f"Heuristic/Python output is a candidate only, not the final artifact.")
    return "CLAUDE"
