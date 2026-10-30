"""env_loader — lightweight .env-style loader for optional API keys.

Purpose
-------
TTS providers other than ``edge`` (Microsoft Edge TTS, anonymous) need API
keys (GEMINI_API_KEY, OPENAI_API_KEY, AZURE_SPEECH_KEY,
GOOGLE_APPLICATION_CREDENTIALS, ELEVENLABS_API_KEY).

This loader looks up env vars in this priority order:

    1. ``os.environ`` (already-exported shell vars win)
    2. ``integrated_learning_video_factory/.env`` (project-local, gitignore-able)
    3. ``configs/secrets.env`` (alternative location)

It is a *pure-Python* implementation — no `python-dotenv` dependency, so the
TTS provider chain stays portable.

The module never writes secrets to disk and never logs the value of a key.
Calling code may log whether a key is *present* but not its content.

Public API
----------
``load_env_files(project_root: Path, extra_paths: Iterable[Path] = ()) -> dict[str, str]``
    Read every ``.env``-style file we know about and return the merged keys.
    Does **not** mutate ``os.environ`` by default.

``get_secret(name: str, *, project_root: Path | None = None) -> Optional[str]``
    Resolve a single secret from ``os.environ`` first, then from .env files.

``check_required_envs(names: Iterable[str], *, project_root: Path | None = None) -> dict[str, bool]``
    Return ``{name: present}`` map without exposing values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional


_DEFAULT_ENV_FILENAMES = (
    ".env.local",   # ml_videos convention (preferred for API keys; gitignored)
    ".env",
    "configs/secrets.env",
)


def _parse_env_text(text: str) -> Dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` env file.

    Supported:
      - Lines like ``FOO=bar``
      - Leading ``export FOO=bar``
      - Quoted values ``FOO="bar"`` or ``FOO='bar'``
      - Blank lines and ``# comment`` lines
    Unsupported (silently ignored):
      - Variable expansion ``$OTHER``
      - Multiline values
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Strip surrounding quotes (single or double) if balanced
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _project_root_default() -> Path:
    # integrations/env_loader.py  ->  project root is parent of "integrations"
    return Path(__file__).resolve().parent.parent


def load_env_files(
    project_root: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
) -> Dict[str, str]:
    """Read all known .env-style files and return merged values.

    Later files override earlier files. ``os.environ`` is NOT consulted here.
    """
    root = (project_root or _project_root_default()).resolve()
    merged: Dict[str, str] = {}
    candidates = [root / name for name in _DEFAULT_ENV_FILENAMES]
    candidates.extend(Path(p) for p in extra_paths)

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        merged.update(_parse_env_text(text))

    return merged


def get_secret(
    name: str,
    *,
    project_root: Optional[Path] = None,
    fallback_files: Iterable[Path] = (),
) -> Optional[str]:
    """Resolve ``name`` from os.environ first, then from .env files.

    Returns ``None`` if not found. Never logs the value.
    """
    value = os.environ.get(name)
    if value:
        return value
    file_values = load_env_files(project_root, extra_paths=fallback_files)
    return file_values.get(name)


def check_required_envs(
    names: Iterable[str],
    *,
    project_root: Optional[Path] = None,
) -> Dict[str, bool]:
    """Return a ``{name: present}`` map without exposing values.

    A name counts as *present* if a non-empty value is found in either
    ``os.environ`` or a .env file.
    """
    return {name: bool(get_secret(name, project_root=project_root)) for name in names}


def load_env_local(project_root: Optional[Path] = None, verbose: bool = False):
    """Merge `.env.local` (then `.env`, `configs/secrets.env`) into os.environ.

    Existing OS env vars win. Returns the list of key names applied. Never prints
    values. Use this so providers can read GEMINI_API_KEY/GOOGLE_API_KEY from the
    gitignored .env.local without exporting them in the shell.
    """
    merged = load_env_files(project_root)
    applied = []
    for k, v in merged.items():
        if os.environ.get(k, "").strip() or not v:
            continue
        os.environ[k] = v
        applied.append(k)
    if verbose:
        print(f"[env_loader] applied {len(applied)} key(s) from env files: {applied or 'none'}")
    return applied


def redact(value, keep: int = 4) -> str:
    """Safe-to-display fragment of a secret (never the full value)."""
    if not value:
        return "(unset)"
    return "***" if len(value) <= keep else f"{value[:keep]}…(redacted)"
