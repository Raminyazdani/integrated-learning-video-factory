"""env_detector — execution-environment heuristics.

Used by `scripts/check_tts_providers.py` (Phase 8A) and any later phase that
needs to decide between two execution contexts:

* **Cowork / Linux sandbox** — read/write to the project files works, but
  Windows `.exe` cannot run and most outbound network is proxy-blocked.
* **Claude Code / local Windows** — Windows venv available, normal network.

The decision matters because the Phase 8A verdict differs:
  - On local Windows, a failed smoke synth = real failure (block Phase 8B).
  - In the Cowork sandbox, a failed smoke synth is usually an environment
    constraint, not a code defect, and should be marked
    ``PENDING_LOCAL_CLAUDE_CODE_VALIDATION`` per
    ``docs/LOCAL_CLAUDE_CODE_EXECUTION_POLICY.md``.

Public API
----------
``detect_environment(project_root: Path) -> EnvInfo``
    Returns a small dataclass with:
      - ``platform``: "windows" | "linux" | "darwin" | "other"
      - ``runtime``: "claude_code_local_windows" | "cowork_sandbox" | "unknown"
      - ``can_execute_windows_venv``: bool
      - ``has_proxy_403_signature``: bool (best-effort)
      - ``notes``: list[str]

``decide_verdict(*, env: EnvInfo, primary_provider: Optional[str],
                 smoke_ok: bool, smoke_duration: Optional[float]) -> Verdict``
    Returns one of: ``"pass"`` | ``"pending_local_claude_code_validation"`` |
    ``"fail"``.
"""
from __future__ import annotations

import os
import platform as _platform
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

Runtime = Literal["claude_code_local_windows", "cowork_sandbox", "unknown"]
Verdict = Literal["pass", "pending_local_claude_code_validation", "fail"]


@dataclass
class EnvInfo:
    platform: str = "unknown"
    runtime: Runtime = "unknown"
    can_execute_windows_venv: bool = False
    has_proxy_403_signature: bool = False
    project_root: Optional[Path] = None
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def _has_sandbox_mount_marker() -> bool:
    """Heuristic: the Cowork Linux mount path contains ``/sessions/<id>/mnt/``.

    We don't assert on the exact path — we just look for the substring in
    ``__file__`` or ``cwd``. This is a *best-effort* signal.
    """
    candidates = [os.getcwd()]
    try:
        candidates.append(str(Path(__file__).resolve()))
    except Exception:
        pass
    needle = "/sessions/"
    for c in candidates:
        if needle in c and "/mnt/" in c:
            return True
    return False


def _windows_venv_executable_present(project_root: Path) -> bool:
    """Check if the Windows venv python exists at the canonical project path.

    Existence does NOT prove executability — on Linux you can `stat` an `.exe`
    over a mount, but you cannot run it. Use this together with ``platform``.
    """
    # Walk up to find a `.venv\Scripts\python.exe` near the project root or
    # one directory up (because the hub layout puts .venv at the parent of
    # `integrated_learning_video_factory/`).
    candidates = [
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root.parent / ".venv" / "Scripts" / "python.exe",
    ]
    return any(p.is_file() for p in candidates)


def _outbound_blocked_signature() -> bool:
    """Best-effort: DNS lookup for a benign host. Failure = restricted env."""
    try:
        socket.setdefaulttimeout(3)
        socket.gethostbyname("speech.platform.bing.com")
        return False
    except Exception:
        # DNS failed — likely sandbox or offline.
        return True


def detect_environment(project_root: Path, *, probe_network: bool = False) -> EnvInfo:
    """Decide if we're in Cowork sandbox or Claude Code on local Windows."""
    env = EnvInfo(project_root=Path(project_root).resolve())
    env.platform = _platform.system().lower() or "unknown"

    venv_present = _windows_venv_executable_present(env.project_root)

    if env.platform == "windows":
        env.runtime = "claude_code_local_windows"
        env.can_execute_windows_venv = venv_present
        if not venv_present:
            env.notes.append(
                "Windows host, but D:\\...\\.venv\\Scripts\\python.exe not "
                "found — set up the project venv before running TTS."
            )
    elif env.platform == "linux" and _has_sandbox_mount_marker():
        env.runtime = "cowork_sandbox"
        env.can_execute_windows_venv = False
        env.notes.append(
            "Linux sandbox with /sessions/<id>/mnt/ mount; cannot execute "
            "Windows .exe binaries."
        )
        if venv_present:
            env.notes.append(
                "Windows venv (.venv\\Scripts\\python.exe) is visible through "
                "the mount but cannot be invoked from Linux."
            )
    else:
        env.runtime = "unknown"
        env.notes.append(
            f"Platform '{env.platform}' — neither obviously Cowork sandbox nor "
            "local Windows. Treating verdict conservatively."
        )

    # Status, dry-run, and ordinary CLI inspection stay offline by default.
    env.has_proxy_403_signature = _outbound_blocked_signature() if probe_network else False
    if env.has_proxy_403_signature:
        env.notes.append(
            "DNS resolution for speech.platform.bing.com failed; outbound "
            "network appears restricted in this environment."
        )

    return env


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def decide_verdict(
    *,
    env: EnvInfo,
    primary_provider: Optional[str],
    smoke_ok: bool,
    smoke_duration: Optional[float],
) -> Verdict:
    """Map probe + smoke outcomes to a single verdict.

    Rules
    -----
    * Provider available + smoke OK + duration in [0.5, 10] s   -> ``pass``.
    * Provider NOT available (and we're in sandbox / restricted) -> ``pending_local_claude_code_validation``.
    * Provider NOT available (on local Windows with no proxy)    -> ``fail``.
    * Provider available but smoke failed (any env)              -> ``fail`` if local-windows,
                                                                   else ``pending_local_claude_code_validation``.
    """
    duration_ok = (
        smoke_duration is not None
        and 0.5 <= float(smoke_duration) <= 10.0
    )

    if primary_provider and smoke_ok and duration_ok:
        return "pass"

    is_environment_blocked = (
        env.runtime == "cowork_sandbox"
        or env.has_proxy_403_signature
        or (env.platform == "linux" and not env.can_execute_windows_venv)
    )

    if not primary_provider:
        return (
            "pending_local_claude_code_validation"
            if is_environment_blocked
            else "fail"
        )

    # Provider available but smoke failed/short
    return (
        "pending_local_claude_code_validation"
        if is_environment_blocked
        else "fail"
    )


def verdict_marker(verdict: Verdict) -> str:
    """Render a verdict as a human-friendly bold string for markdown."""
    return {
        "pass": "**pass**",
        "fail": "**fail**",
        "pending_local_claude_code_validation": "**PENDING_LOCAL_CLAUDE_CODE_VALIDATION**",
    }[verdict]
