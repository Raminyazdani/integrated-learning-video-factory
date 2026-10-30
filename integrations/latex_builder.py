"""latex_builder — XeLaTeX invocation, log capture, font detection.

Public surface:
    - detect_xelatex() -> dict
    - detect_persian_font(cascade: list[str]) -> str | None
    - run_xelatex(tex_path, work_dir, *, passes=2) -> BuildResult
    - find_log_errors(log_text: str) -> list[ErrorMatch]

This module is environment-only — it knows nothing about Persian content
packages or the curated content registry. Phase 5+ agents build on top.

Notes for the sandbox:
    Phase 5A is a smoke test. The environment may lack the configured
    Persian fonts (Vazirmatn, Tahoma, Arial). `detect_persian_font`
    iterates the cascade plus a final "DejaVu Sans" fallback (DejaVu has
    minimal but functional Arabic-script glyph coverage and is always
    available on Debian-family TeX Live installs). The chosen font is
    reported back so the build report can show what was actually used.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --- environment detection --------------------------------------------------


def detect_xelatex() -> dict:
    """Return a dict describing the local XeLaTeX environment.

    Keys:
        available: bool
        path: str | None
        version: str | None (first banner line if available)
        polyglossia: bool
        xepersian: bool
        fontspec: bool
        graphicx: bool
        geometry: bool
        hyperref: bool
    """
    info: dict = {
        "available": False,
        "path": None,
        "version": None,
    }
    xelatex_path = shutil.which("xelatex")
    info["available"] = xelatex_path is not None
    info["path"] = xelatex_path
    if xelatex_path:
        try:
            v = subprocess.run(
                [xelatex_path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            first_line = (v.stdout or v.stderr or "").splitlines()[:1]
            info["version"] = first_line[0] if first_line else None
        except Exception:
            info["version"] = None

    for pkg in ("polyglossia", "xepersian", "fontspec", "graphicx", "geometry", "hyperref"):
        info[pkg] = _has_tex_package(pkg)
    return info


def _has_tex_package(name: str) -> bool:
    """Return True iff ``kpsewhich <name>.sty`` resolves to a real file."""
    kp = shutil.which("kpsewhich")
    if kp is None:
        return False
    try:
        r = subprocess.run([kp, f"{name}.sty"], capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


def detect_persian_font(cascade: list[str], final_fallback: str = "DejaVu Sans") -> str:
    """Pick the first font from `cascade` that fontconfig actually resolves.

    Uses ``fc-match`` to test each candidate; returns the *requested* family
    name if fontconfig matches that name (a true hit) and otherwise tries
    the next candidate. If none of the candidates match, returns
    ``final_fallback`` unconditionally.

    The returned string is suitable for ``\\setmainfont{...}``.
    """
    fc = shutil.which("fc-match")
    if fc is None:
        return final_fallback
    for family in cascade + [final_fallback]:
        try:
            r = subprocess.run(
                [fc, "-f", "%{family[0]}\n", family],
                capture_output=True, text=True, timeout=5,
            )
            resolved = (r.stdout or "").strip()
            # fontconfig will substitute a default font when the requested family
            # is missing; we accept the family only if fontconfig returns it
            # case-insensitively, or if this is the final fallback (last loop).
            if resolved.lower() == family.lower():
                return family
        except Exception:
            continue
    return final_fallback


# --- build result ----------------------------------------------------------


@dataclass(frozen=True)
class ErrorMatch:
    kind: str          # one of: missing-font, missing-package, unicode, generic
    line: str
    detail: str = ""


@dataclass
class BuildResult:
    success: bool
    tex_path: Path
    pdf_path: Path | None
    log_path: Path | None
    return_codes: list[int] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    errors: list[ErrorMatch] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"success={self.success}",
            f"tex={self.tex_path.name}",
        ]
        if self.pdf_path:
            bits.append(f"pdf={self.pdf_path.name}({_safe_size(self.pdf_path)} bytes)")
        if self.log_path:
            bits.append(f"log={self.log_path.name}")
        bits.append(f"errors={len(self.errors)}")
        return " ".join(bits)


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except Exception:
        return 0


# --- runner ---------------------------------------------------------------


def run_xelatex(tex_path: Path, work_dir: Path, *, passes: int = 2, timeout_s: int = 90) -> BuildResult:
    """Run xelatex on `tex_path` inside `work_dir`. Returns a BuildResult.

    XeLaTeX is invoked in nonstop mode so a single fault doesn't hang.
    Two passes are the default (resolves references), but missing packages
    or fatal errors short-circuit subsequent passes.
    """
    tex_path = Path(tex_path).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    xelatex = shutil.which("xelatex")
    if xelatex is None:
        return BuildResult(
            success=False, tex_path=tex_path, pdf_path=None, log_path=None,
            errors=[ErrorMatch("environment", "xelatex not on PATH")],
            stderr_tail="xelatex not installed",
        )

    rcs: list[int] = []
    stdout_tail = ""
    stderr_tail = ""
    for i in range(passes):
        proc = subprocess.run(
            [
                xelatex,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-no-shell-escape",
                "-output-directory", str(work_dir),
                str(tex_path),
            ],
            capture_output=True, text=True, timeout=timeout_s,
            cwd=str(tex_path.parent),  # so relative \includegraphics paths resolve
        )
        rcs.append(proc.returncode)
        # Keep only the tail of each stream to limit memory and report size.
        stdout_tail = (proc.stdout or "")[-4000:]
        stderr_tail = (proc.stderr or "")[-2000:]
        if proc.returncode != 0:
            break  # short-circuit on first failure

    log_path = work_dir / (tex_path.stem + ".log")
    pdf_path = work_dir / (tex_path.stem + ".pdf")
    errors = []
    if log_path.exists():
        try:
            errors = find_log_errors(log_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            errors = [ErrorMatch("generic", "could not read .log")]

    pdf_ok = pdf_path.exists() and pdf_path.stat().st_size > 0
    success = (rcs[-1] == 0 if rcs else False) and pdf_ok

    return BuildResult(
        success=success,
        tex_path=tex_path,
        pdf_path=pdf_path if pdf_ok else None,
        log_path=log_path if log_path.exists() else None,
        return_codes=rcs,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        errors=errors,
    )


# --- log parsing ----------------------------------------------------------


_MISSING_FONT_RE = re.compile(
    r"!\s*fontspec error.*\n.*?\"([^\"]+)\".*?cannot be found", re.IGNORECASE
)
_GENERIC_FONT_RE = re.compile(
    r"Font\s+\\[A-Za-z]+/[a-z0-9]+/m/n/\d+(?:\.\d+)?\s+is not loadable", re.IGNORECASE
)
_MISSING_PACKAGE_RE = re.compile(
    r"!\s*LaTeX Error:\s*File\s+`([^`']+)'\s+not found", re.IGNORECASE
)
_UNICODE_RE = re.compile(
    r"Missing character: There is no .* in font", re.IGNORECASE
)
_GENERIC_ERR_RE = re.compile(r"^!\s+.*$", re.MULTILINE)


def find_log_errors(log_text: str) -> list[ErrorMatch]:
    """Scan a xelatex .log for known error classes."""
    out: list[ErrorMatch] = []
    for m in _MISSING_FONT_RE.finditer(log_text):
        out.append(ErrorMatch("missing-font", m.group(0), m.group(1)))
    for m in _GENERIC_FONT_RE.finditer(log_text):
        out.append(ErrorMatch("missing-font", m.group(0)))
    for m in _MISSING_PACKAGE_RE.finditer(log_text):
        out.append(ErrorMatch("missing-package", m.group(0), m.group(1)))
    for m in _UNICODE_RE.finditer(log_text):
        # Only record the first few unicode warnings, otherwise this floods.
        if sum(1 for e in out if e.kind == "unicode") < 5:
            out.append(ErrorMatch("unicode", m.group(0)))
    if not out:
        for m in _GENERIC_ERR_RE.finditer(log_text):
            line = m.group(0).strip()
            if line and "Emergency stop" not in line and len(out) < 10:
                out.append(ErrorMatch("generic", line))
    return out
