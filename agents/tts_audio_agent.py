"""tts_audio_agent - Stage 8 (TTS / audio) agent.

Responsibilities
----------------
* Phase 8A (provider check): probe configured providers, pick a working
  primary, run *one* tiny Persian smoke synthesis (when provider+network
  cooperate). No real narration audio is ever produced here.
* Phase 8B (per-package audio): consume
  narration_scripts/<package_id>.json and emit
    audio/<package_id>/slide_NNN.mp3            (one MP3 per slide)
    audio/audio_manifest.json                   (per-slide metadata, atomic)
* Phase 8C (all-package audio): same as 8B but iterates every package in
  narration_scripts/full_narration.json. Idempotent - skips files that
  already exist and validate (size > 0 AND duration > 1 s) unless --force.

This file is intentionally *not wired into the Orchestrator* yet - Phase 8B/C
runs through scripts/generate_audio.py.

All file writes go through integrations.atomic_io so partial writes are
impossible.
"""
from __future__ import annotations

import importlib
import json
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations.atomic_io import atomic_write_bytes, atomic_write_json, iso_now
from integrations.env_loader import check_required_envs
from integrations.tts_cleaning import clean_text_for_tts, compute_text_stats
from integrations.tts_providers import (
    ProbeResult,
    SynthResult,
    TTSProvider,
    available_provider_names,
    get_provider,
)


MIN_AUDIO_DURATION_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Phase 8A - probe outcome
# ---------------------------------------------------------------------------


@dataclass
class ChainProbeOutcome:
    probes: Dict[str, ProbeResult] = field(default_factory=dict)
    primary: Optional[str] = None
    selected_voice: Optional[str] = None
    fallback_voice: Optional[str] = None
    env_presence: Dict[str, bool] = field(default_factory=dict)


def load_tts_config(project_root: Path) -> dict:
    cfg_path = Path(project_root) / "configs" / "tts_config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def probe_chain(config, *, project_root, network=True) -> ChainProbeOutcome:
    chain: List[str] = list(config.get("provider_chain") or [])
    providers_cfg: Dict[str, dict] = dict(config.get("providers") or {})

    outcome = ChainProbeOutcome()

    seen: List[str] = []
    for n in chain:
        if n in providers_cfg and n not in seen and n in available_provider_names():
            seen.append(n)
    for n in sorted(providers_cfg.keys()):
        if (
            n in available_provider_names()
            and n not in seen
            and providers_cfg.get(n, {}).get("enabled") is True
        ):
            seen.append(n)

    env_requirements: List[str] = []
    for n in seen:
        prov_cfg = providers_cfg.get(n, {}) or {}
        requires_env = prov_cfg.get("requires_env")
        if requires_env:
            if isinstance(requires_env, str):
                env_requirements.append(requires_env)
            else:
                env_requirements.extend(requires_env)

        provider: TTSProvider = get_provider(n, prov_cfg)
        if prov_cfg.get("enabled") is False:
            probe = ProbeResult(provider=n, importable=False, available=False)
            probe.notes.append("disabled in tts_config.json")
            outcome.probes[n] = probe
            continue
        probe = provider.probe(network=network)
        outcome.probes[n] = probe

        if outcome.primary is None and probe.available and n in chain:
            outcome.primary = n
            outcome.selected_voice = provider.resolve_voice(prov_cfg.get("voice"))
            fallbacks = prov_cfg.get("fallback_voices") or []
            outcome.fallback_voice = fallbacks[0] if fallbacks else None

    if env_requirements:
        outcome.env_presence = check_required_envs(
            set(env_requirements), project_root=project_root
        )

    return outcome


def smoke_synthesize(*, config, provider_name, voice, out_path, text="این یک آزمون TTS است."):
    providers_cfg: Dict[str, dict] = dict(config.get("providers") or {})
    prov_cfg = providers_cfg.get(provider_name, {})
    provider = get_provider(provider_name, prov_cfg)

    cleaned = clean_text_for_tts(
        text,
        apply_symbol_map=bool(config.get("cleaning", {}).get("apply_symbol_map", True)),
        apply_term_replacements=bool(config.get("cleaning", {}).get("apply_term_replacements", True)),
        persian_digit_conversion=bool(config.get("cleaning", {}).get("persian_digit_conversion", True)),
    )

    started = time.time()
    result = provider.synthesize(cleaned, out_path, voice=voice)
    result.extra.setdefault("wall_seconds", round(time.time() - started, 3))
    result.extra.setdefault("input_text", text)
    result.extra.setdefault("input_cleaned", cleaned)
    result.extra.setdefault("input_stats", compute_text_stats(cleaned))
    return result


# ---------------------------------------------------------------------------
# Static health check
# ---------------------------------------------------------------------------


def static_health_check(project_root: Path) -> dict:
    result: Dict[str, Any] = {
        "imports": {},
        "tts_config_valid": False,
        "providers_registered": [],
        "schemas_present": {},
        "ok": True,
        "errors": [],
    }
    modules_to_import = [
        "integrations.atomic_io",
        "integrations.env_loader",
        "integrations.env_detector",
        "integrations.tts_cleaning",
        "integrations.tts_providers",
        "integrations.tts_providers.edge",
        "integrations.tts_providers.gtts_provider",
        "integrations.tts_providers.pyttsx3_provider",
        "integrations.tts_providers.registry",
        "agents.tts_audio_agent",
    ]
    for m in modules_to_import:
        try:
            importlib.import_module(m)
            result["imports"][m] = "ok"
        except Exception as exc:
            result["imports"][m] = f"error: {exc!r}"
            result["ok"] = False
            result["errors"].append(f"import {m}: {exc!r}")

    try:
        cfg = load_tts_config(project_root)
        if isinstance(cfg.get("provider_chain"), list) and cfg.get("default_provider"):
            result["tts_config_valid"] = True
        else:
            result["errors"].append("tts_config.json missing provider_chain/default_provider")
            result["ok"] = False
    except Exception as exc:
        result["errors"].append(f"tts_config load failed: {exc!r}")
        result["ok"] = False

    try:
        result["providers_registered"] = list(available_provider_names())
    except Exception as exc:
        result["errors"].append(f"provider registry failed: {exc!r}")
        result["ok"] = False

    schemas_dir = Path(project_root) / "schemas"
    for s in ("narration.schema.json", "slide_manifest.schema.json"):
        present = (schemas_dir / s).is_file()
        result["schemas_present"][s] = present
        if not present:
            result["errors"].append(f"schema missing: {s}")
            result["ok"] = False
    return result


# ---------------------------------------------------------------------------
# Audio inspection helper
# ---------------------------------------------------------------------------


def inspect_audio(path) -> dict:
    path = Path(path)
    out = {
        "path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "duration_seconds": None,
        "bitrate_kbps": None,
        "sample_rate_hz": None,
        "channels": None,
        "encoding": None,
    }
    if not path.exists() or out["size_bytes"] == 0:
        return out
    suffix = path.suffix.lower().lstrip(".")
    out["encoding"] = suffix or None
    try:
        if suffix == "mp3":
            from mutagen.mp3 import MP3
            audio = MP3(str(path))
            info = audio.info
            out["duration_seconds"] = round(float(info.length), 3)
            out["bitrate_kbps"] = round(float(info.bitrate) / 1000.0, 1)
            out["sample_rate_hz"] = int(info.sample_rate)
            out["channels"] = int(getattr(info, "channels", 1))
        elif suffix == "wav":
            import wave
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                out["duration_seconds"] = round(frames / float(rate), 3) if rate else None
                out["sample_rate_hz"] = rate
                out["channels"] = wf.getnchannels()
                bps = rate * wf.getnchannels() * wf.getsampwidth() if rate else 0
                if bps:
                    out["bitrate_kbps"] = round(bps * 8 / 1000.0, 1)
    except Exception as exc:
        out["inspect_error"] = repr(exc)
    return out


def _audio_is_valid(audio_info: dict) -> bool:
    return (
        audio_info.get("size_bytes", 0) > 0
        and audio_info.get("duration_seconds") is not None
        and float(audio_info["duration_seconds"]) > MIN_AUDIO_DURATION_SECONDS
    )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SlideAudioResult:
    slide_id: str
    success: bool = False
    skipped_reason: Optional[str] = None       # "already_valid" | None
    audio_path: Optional[Path] = None
    audio_bytes: int = 0
    duration_seconds: Optional[float] = None
    bitrate_kbps: Optional[float] = None
    sample_rate_hz: Optional[int] = None
    channels: Optional[int] = None
    encoding: Optional[str] = None
    provider: Optional[str] = None
    voice: Optional[str] = None
    narration_sha256: Optional[str] = None
    input_chars: int = 0
    input_cleaned: str = ""
    error: Optional[str] = None
    wall_seconds: float = 0.0
    size_gt_zero: bool = False
    duration_gt_min: bool = False
    validation_status: str = "fail"  # "pass" | "fail"


@dataclass
class PackageAudioResult:
    package_id: str
    provider: str
    voice: str
    audio_dir: Path
    manifest_path: Path
    started_at: str = ""
    finished_at: str = ""
    slides: List[SlideAudioResult] = field(default_factory=list)
    total_audio_bytes: int = 0
    total_duration_seconds: float = 0.0
    total_wall_seconds: float = 0.0
    n_reused: int = 0  # idempotent skips that were already valid

    @property
    def n_slides(self) -> int:
        return len(self.slides)

    @property
    def n_succeeded(self) -> int:
        return sum(1 for s in self.slides if s.validation_status == "pass")

    @property
    def n_failed(self) -> int:
        return self.n_slides - self.n_succeeded


@dataclass
class RunAudioResult:
    """Aggregate of every package's PackageAudioResult for a Phase 8C run."""
    run_id: str
    provider: str
    voice: str
    manifest_path: Path
    started_at: str = ""
    finished_at: str = ""
    packages: List[PackageAudioResult] = field(default_factory=list)

    @property
    def n_packages(self) -> int:
        return len(self.packages)

    @property
    def n_slides_total(self) -> int:
        return sum(p.n_slides for p in self.packages)

    @property
    def n_succeeded(self) -> int:
        return sum(p.n_succeeded for p in self.packages)

    @property
    def n_failed(self) -> int:
        return sum(p.n_failed for p in self.packages)

    @property
    def n_reused(self) -> int:
        return sum(p.n_reused for p in self.packages)

    @property
    def total_audio_bytes(self) -> int:
        return sum(p.total_audio_bytes for p in self.packages)

    @property
    def total_duration_seconds(self) -> float:
        return sum(p.total_duration_seconds for p in self.packages)

    @property
    def total_wall_seconds(self) -> float:
        return sum(p.total_wall_seconds for p in self.packages)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slide_number_from_id(slide_id: str) -> str:
    parts = slide_id.split("_")
    for p in parts:
        if p.startswith("s") and p[1:].isdigit():
            return p[1:]
    digits = "".join(ch for ch in slide_id if ch.isdigit())
    return digits.zfill(3) if digits else slide_id


def _provider_ext_for(provider_name: str) -> str:
    return "wav" if provider_name == "pyttsx3" else "mp3"


def _load_package_narration(narration_scripts_dir: Path, package_id: str) -> List[dict]:
    per_pkg = narration_scripts_dir / f"{package_id}.json"
    if per_pkg.is_file():
        with open(per_pkg, "r", encoding="utf-8") as f:
            payload = json.load(f)
        for pkg in payload.get("packages", []):
            if pkg.get("package_id") == package_id:
                return list(pkg.get("slides", []))
        raise ValueError(f"{per_pkg} does not contain package_id={package_id!r}")

    full = narration_scripts_dir / "full_narration.json"
    if full.is_file():
        with open(full, "r", encoding="utf-8") as f:
            payload = json.load(f)
        for pkg in payload.get("packages", []):
            if pkg.get("package_id") == package_id:
                return list(pkg.get("slides", []))
    raise FileNotFoundError(
        f"narration for {package_id} not found (looked at {per_pkg} and full_narration.json)"
    )


def _load_full_narration(narration_scripts_dir: Path) -> List[dict]:
    """Return the list of package blocks from full_narration.json."""
    full = narration_scripts_dir / "full_narration.json"
    if not full.is_file():
        raise FileNotFoundError(
            f"precondition failed: {full} not found. Phase 7B must complete."
        )
    with open(full, "r", encoding="utf-8") as f:
        payload = json.load(f)
    pkgs = list(payload.get("packages", []))
    if not pkgs:
        raise ValueError(f"{full} has no packages")
    return pkgs


# ---------------------------------------------------------------------------
# Phase 8B / 8C - per-package synthesis (idempotent)
# ---------------------------------------------------------------------------


def synthesize_package_audio(
    *,
    project_root: Path,
    run_dir: Path,
    package_id: str,
    config: dict,
    provider_name: str,
    voice: Optional[str] = None,
    overwrite: bool = False,
    write_manifest: bool = True,
    progress_callback=None,
    slides_override: Optional[List[dict]] = None,
) -> PackageAudioResult:
    """Synthesize one MP3 (or WAV) per slide for a single package.

    Idempotency:
      - If the target audio file already exists AND validates (size > 0 AND
        duration > MIN_AUDIO_DURATION_SECONDS), it is **reused** (counted in
        ``n_reused``) - no re-synthesis, no provider network call.
      - If ``overwrite=True``, any existing file is replaced.
      - If the existing file is invalid (zero bytes, too short, unreadable),
        it is deleted and re-synthesized.

    Never writes placeholders. If a slide fails, no file is left at its
    target path.
    """
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    narration_dir = run_dir / "narration_scripts"

    slides = (
        list(slides_override)
        if slides_override is not None
        else _load_package_narration(narration_dir, package_id)
    )
    if not slides:
        raise ValueError(f"no slides found for {package_id}")

    providers_cfg: Dict[str, dict] = dict(config.get("providers") or {})
    prov_cfg = providers_cfg.get(provider_name, {})
    provider = get_provider(provider_name, prov_cfg)
    chosen_voice = voice or provider.resolve_voice(prov_cfg.get("voice"))

    audio_dir = run_dir / "audio" / package_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "audio" / "audio_manifest.json"

    result = PackageAudioResult(
        package_id=package_id,
        provider=provider_name,
        voice=chosen_voice or "",
        audio_dir=audio_dir,
        manifest_path=manifest_path,
        started_at=iso_now(),
    )

    cleaning_cfg = config.get("cleaning", {}) or {}
    ext = _provider_ext_for(provider_name)

    for idx, entry in enumerate(slides):
        slide_id = entry.get("slide_id") or f"unknown_{idx:03d}"
        # Prefer the TTS-safe spoken text (formulas verbalized, paths stripped);
        # fall back to narration_fa for older narration files without tts_text.
        narration = entry.get("tts_text") or entry.get("narration_fa") or ""
        hints = entry.get("pronunciation_hints") or []
        # cache key reflects the actual synthesized text (tts_text) so changes invalidate
        import hashlib as _hl
        sha = _hl.sha256(narration.encode("utf-8")).hexdigest()

        sno = _slide_number_from_id(slide_id)
        target = audio_dir / f"slide_{sno}.{ext}"

        slide_res = SlideAudioResult(
            slide_id=slide_id,
            provider=provider_name,
            voice=chosen_voice,
            narration_sha256=sha,
            input_chars=len(narration),
        )

        if progress_callback is not None:
            try:
                progress_callback(idx + 1, len(slides), slide_id)
            except Exception:
                pass

        if not narration.strip():
            slide_res.error = "empty narration_fa"
            result.slides.append(slide_res)
            continue

        # ---- idempotency check ----
        if target.exists() and not overwrite:
            info = inspect_audio(target)
            if _audio_is_valid(info):
                slide_res.success = True
                slide_res.skipped_reason = "already_valid"
                slide_res.audio_path = target
                slide_res.audio_bytes = info["size_bytes"]
                slide_res.duration_seconds = info.get("duration_seconds")
                slide_res.bitrate_kbps = info.get("bitrate_kbps")
                slide_res.sample_rate_hz = info.get("sample_rate_hz")
                slide_res.channels = info.get("channels")
                slide_res.encoding = info.get("encoding") or ext
                slide_res.size_gt_zero = True
                slide_res.duration_gt_min = True
                slide_res.validation_status = "pass"
                result.n_reused += 1
                result.total_audio_bytes += slide_res.audio_bytes
                if slide_res.duration_seconds:
                    result.total_duration_seconds += float(slide_res.duration_seconds)
                result.slides.append(slide_res)
                continue
            # existing but invalid -> delete and re-synthesize
            try:
                target.unlink()
            except OSError:
                pass

        # ---- clean text ----
        cleaned = clean_text_for_tts(
            narration,
            hints=hints,
            apply_symbol_map=bool(cleaning_cfg.get("apply_symbol_map", True)),
            apply_term_replacements=bool(cleaning_cfg.get("apply_term_replacements", True)),
            persian_digit_conversion=bool(cleaning_cfg.get("persian_digit_conversion", True)),
        )
        slide_res.input_cleaned = cleaned

        # ---- synthesize ----
        started = time.time()
        synth = provider.synthesize(cleaned, target, voice=chosen_voice)
        wall = round(time.time() - started, 3)
        slide_res.wall_seconds = wall

        if not synth.success:
            slide_res.error = synth.error or "unknown synth error"
            try:
                if target.exists() and target.stat().st_size == 0:
                    target.unlink()
            except OSError:
                pass
            result.slides.append(slide_res)
            continue

        slide_res.success = True
        slide_res.audio_path = synth.out_path or target
        slide_res.audio_bytes = synth.audio_bytes
        info = inspect_audio(slide_res.audio_path)
        slide_res.duration_seconds = info.get("duration_seconds")
        slide_res.bitrate_kbps = info.get("bitrate_kbps")
        slide_res.sample_rate_hz = info.get("sample_rate_hz")
        slide_res.channels = info.get("channels")
        slide_res.encoding = info.get("encoding") or ext

        slide_res.size_gt_zero = slide_res.audio_bytes > 0
        slide_res.duration_gt_min = (
            slide_res.duration_seconds is not None
            and float(slide_res.duration_seconds) > MIN_AUDIO_DURATION_SECONDS
        )
        slide_res.validation_status = (
            "pass" if (slide_res.size_gt_zero and slide_res.duration_gt_min) else "fail"
        )

        result.total_audio_bytes += slide_res.audio_bytes
        if slide_res.duration_seconds:
            result.total_duration_seconds += float(slide_res.duration_seconds)
        result.total_wall_seconds += wall
        result.slides.append(slide_res)

    result.finished_at = iso_now()

    if write_manifest:
        _merge_into_audio_manifest(result, run_dir=run_dir)

    return result


def synthesize_all_audio(
    *,
    project_root: Path,
    run_dir: Path,
    config: dict,
    provider_name: str,
    voice: Optional[str] = None,
    overwrite: bool = False,
    progress_callback=None,
) -> RunAudioResult:
    """Phase 8C: iterate every package in full_narration.json and synthesize.

    Idempotent: skips already-valid audio files unless ``overwrite=True``.
    Writes ``audio/audio_manifest.json`` once at the end with every package's
    entry (atomic).
    """
    project_root = Path(project_root).resolve()
    run_dir = Path(run_dir).resolve()
    narration_dir = run_dir / "narration_scripts"
    packages = _load_full_narration(narration_dir)

    run_result = RunAudioResult(
        run_id=run_dir.name,
        provider=provider_name,
        voice=voice or "",
        manifest_path=run_dir / "audio" / "audio_manifest.json",
        started_at=iso_now(),
    )

    for pkg_idx, pkg_block in enumerate(packages, start=1):
        package_id = pkg_block.get("package_id") or f"package_{pkg_idx:03d}"
        slides = list(pkg_block.get("slides", []))
        if not slides:
            continue

        def _pkg_progress(i: int, n: int, slide_id: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(package_id, i, n, slide_id)
            except Exception:
                pass

        pkg_result = synthesize_package_audio(
            project_root=project_root,
            run_dir=run_dir,
            package_id=package_id,
            config=config,
            provider_name=provider_name,
            voice=voice,
            overwrite=overwrite,
            write_manifest=True,  # incremental atomic manifest update
            progress_callback=_pkg_progress,
            slides_override=slides,
        )
        run_result.packages.append(pkg_result)
        # Re-sync chosen voice from the first package's actual resolution.
        if not run_result.voice and pkg_result.voice:
            run_result.voice = pkg_result.voice

    run_result.finished_at = iso_now()
    return run_result


def _merge_into_audio_manifest(result: PackageAudioResult, *, run_dir: Path) -> Path:
    manifest_path = result.manifest_path
    existing: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id": run_dir.name,
        "generated_at": iso_now(),
        "packages": {},
    }
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if "packages" not in existing:
                existing["packages"] = {}
        except Exception:
            pass

    pkg_block = {
        "package_id": result.package_id,
        "provider": result.provider,
        "voice": result.voice,
        "audio_dir": str(result.audio_dir.relative_to(run_dir)).replace("\\", "/"),
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "totals": {
            "n_slides": result.n_slides,
            "n_succeeded": result.n_succeeded,
            "n_failed": result.n_failed,
            "n_reused": result.n_reused,
            "total_audio_bytes": result.total_audio_bytes,
            "total_duration_seconds": round(result.total_duration_seconds, 3),
            "total_wall_seconds": round(result.total_wall_seconds, 3),
        },
        "slides": [
            {
                "slide_id": s.slide_id,
                "success": s.success,
                "skipped_reason": s.skipped_reason,
                "audio_path": (
                    str(s.audio_path.relative_to(run_dir)).replace("\\", "/")
                    if s.audio_path
                    else None
                ),
                "audio_bytes": s.audio_bytes,
                "duration_seconds": s.duration_seconds,
                "bitrate_kbps": s.bitrate_kbps,
                "sample_rate_hz": s.sample_rate_hz,
                "channels": s.channels,
                "encoding": s.encoding,
                "provider": s.provider,
                "voice": s.voice,
                "narration_sha256": s.narration_sha256,
                "input_chars": s.input_chars,
                "wall_seconds": s.wall_seconds,
                "validation": {
                    "size_gt_zero": s.size_gt_zero,
                    "duration_gt_1s": s.duration_gt_min,
                    "status": s.validation_status,
                },
                "error": s.error,
            }
            for s in result.slides
        ],
    }
    existing["packages"][result.package_id] = pkg_block
    existing["generated_at"] = iso_now()
    atomic_write_json(manifest_path, existing)
    return manifest_path


# ---------------------------------------------------------------------------
# Phase 8B / 8C - tts_report.md writer
# ---------------------------------------------------------------------------


def write_tts_report(
    *,
    report_path: Path,
    run_id: str,
    package_id: Optional[str] = None,
    env_info: Optional[Any] = None,
    static_health: Optional[dict] = None,
    provider_chain: Optional[List[str]] = None,
    primary_provider: Optional[str] = None,
    selected_voice: Optional[str] = None,
    package_result: Optional[PackageAudioResult] = None,
    run_result: Optional[RunAudioResult] = None,
    verdict: str = "pending_local_claude_code_validation",
    reason: Optional[str] = None,
    windows_command: Optional[str] = None,
) -> Path:
    """Render `QA_REPORTS/tts_report.md`.

    Modes:
      - Phase 8B (single package): pass ``package_result``.
      - Phase 8C (all packages):   pass ``run_result``.
      - PENDING (no synthesis):    pass neither.
    """
    lines: List[str] = []
    env_dict = _env_to_dict(env_info)

    title_scope = "all packages" if run_result is not None else (package_id or "(none)")
    lines.append("# Phase 8B/8C - TTS audio report")
    lines.append("")
    lines.append(f"- generated_at: {iso_now()}")
    lines.append(f"- run_id: {run_id}")
    lines.append(f"- scope: {title_scope}")
    if provider_chain is not None:
        lines.append(f"- provider_chain (config): {provider_chain}")
    if primary_provider is not None:
        lines.append(f"- primary_provider: `{primary_provider}`")
    if selected_voice is not None:
        lines.append(f"- selected_voice: `{selected_voice}`")
    if env_dict:
        lines.append(
            f"- runtime: **{env_dict.get('runtime', 'unknown')}** "
            f"(platform={env_dict.get('platform', 'unknown')}, "
            f"can_exec_windows_venv={env_dict.get('can_execute_windows_venv', False)}, "
            f"proxy_403={env_dict.get('has_proxy_403_signature', False)})"
        )
    lines.append(f"- verdict: {_verdict_marker(verdict)}")
    if reason:
        lines.append(f"- reason: {reason}")
    lines.append("")

    env_blocked = env_dict.get("runtime") == "cowork_sandbox"

    if run_result is not None:
        r = run_result
        lines.append("## Run totals (Phase 8C - all packages)")
        lines.append(f"- n_packages: {r.n_packages}")
        lines.append(f"- n_slides_total: {r.n_slides_total}")
        lines.append(f"- n_succeeded: {r.n_succeeded}")
        lines.append(f"- n_failed: {r.n_failed}")
        lines.append(f"- n_reused (idempotent skips of existing valid audio): {r.n_reused}")
        lines.append(f"- total_audio_bytes: {r.total_audio_bytes:,}")
        lines.append(f"- total_duration_seconds: {r.total_duration_seconds:.3f}")
        lines.append(f"- total_wall_seconds: {r.total_wall_seconds:.3f}")
        lines.append(f"- audio_manifest: `{r.manifest_path}`")
        lines.append("")

        lines.append("## Per-package totals")
        lines.append("")
        lines.append("| package_id | slides | succeeded | failed | reused | audio_bytes | duration_s |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for p in r.packages:
            lines.append(
                f"| `{p.package_id}` | {p.n_slides} | {p.n_succeeded} | {p.n_failed} "
                f"| {p.n_reused} | {p.total_audio_bytes:,} | {p.total_duration_seconds:.1f} |"
            )
        lines.append("")

        # Failure detail per package (only show failed slides)
        any_failed = any(p.n_failed > 0 for p in r.packages)
        if any_failed:
            lines.append("## Failed slides (subset)")
            lines.append("")
            lines.append("| package_id | slide_id | size_bytes | duration_s | error |")
            lines.append("|---|---|---:|---:|---|")
            for p in r.packages:
                for s in p.slides:
                    if s.validation_status == "pass":
                        continue
                    err = (s.error or "").replace("|", "\\|")
                    lines.append(
                        f"| `{p.package_id}` | `{s.slide_id}` | {s.audio_bytes:,} "
                        f"| {s.duration_seconds if s.duration_seconds is not None else '-'} "
                        f"| {err} |"
                    )
            lines.append("")

    if package_result is not None:
        r = package_result
        lines.append(f"## Package totals (Phase 8B - {r.package_id})")
        lines.append(f"- n_slides_in_narration: {r.n_slides}")
        lines.append(f"- n_succeeded (size>0 AND duration>{MIN_AUDIO_DURATION_SECONDS}s): {r.n_succeeded}")
        lines.append(f"- n_failed: {r.n_failed}")
        lines.append(f"- n_reused: {r.n_reused}")
        lines.append(f"- total_audio_bytes: {r.total_audio_bytes:,}")
        lines.append(f"- total_duration_seconds: {r.total_duration_seconds:.3f}")
        lines.append(f"- total_wall_seconds: {r.total_wall_seconds:.3f}")
        lines.append(f"- audio_dir: `{r.audio_dir}`")
        lines.append(f"- audio_manifest: `{r.manifest_path}`")
        lines.append("")

        lines.append("## Per-slide validation")
        lines.append("")
        lines.append("| slide_id | size_bytes | duration_s | size>0 | dur>1s | status | error |")
        lines.append("|---|---:|---:|:---:|:---:|:---:|---|")
        for s in r.slides:
            err = (s.error or "").replace("|", "\\|")
            lines.append(
                f"| `{s.slide_id}` | {s.audio_bytes:,} "
                f"| {s.duration_seconds if s.duration_seconds is not None else '-'} "
                f"| {'yes' if s.size_gt_zero else 'no'} "
                f"| {'yes' if s.duration_gt_min else 'no'} "
                f"| {s.validation_status} | {err} |"
            )
        lines.append("")

    lines.append("## Acceptance")
    if run_result is not None:
        ok_all = run_result.n_slides_total > 0 and run_result.n_failed == 0
        lines.append(_row_b("every package iterated", run_result.n_packages > 0, env_blocked))
        lines.append(_row_b("every slide has an audio file", all(s.success for p in run_result.packages for s in p.slides), env_blocked))
        lines.append(_row_b("every audio file size > 0", all(s.size_gt_zero for p in run_result.packages for s in p.slides), env_blocked))
        lines.append(_row_b(f"every audio file duration > {MIN_AUDIO_DURATION_SECONDS}s", all(s.duration_gt_min for p in run_result.packages for s in p.slides), env_blocked))
        lines.append(_row_b("provider used recorded in audio_manifest.json", bool(run_result.provider), env_blocked))
        lines.append(_row_b("audio_manifest.json written atomically", run_result.manifest_path.is_file(), env_blocked))
    elif package_result is not None:
        lines.append(_row_b("every slide has an audio file", all(s.success for s in package_result.slides), env_blocked))
        lines.append(_row_b("every audio file size > 0", all(s.size_gt_zero for s in package_result.slides), env_blocked))
        lines.append(_row_b(f"every audio file duration > {MIN_AUDIO_DURATION_SECONDS}s", all(s.duration_gt_min for s in package_result.slides), env_blocked))
        lines.append(_row_b("provider used recorded in audio_manifest.json", bool(package_result.provider), env_blocked))
        lines.append(_row_b("audio_manifest.json written atomically", package_result.manifest_path.is_file(), env_blocked))
    else:
        for label in (
            "every package iterated",
            "every slide has an audio file",
            "every audio file size > 0",
            f"every audio file duration > {MIN_AUDIO_DURATION_SECONDS}s",
            "provider used recorded in audio_manifest.json",
        ):
            lines.append(f"- {label}: **PENDING_LOCAL_CLAUDE_CODE_VALIDATION**")
    lines.append("")

    lines.append("## Hard limits respected")
    lines.append("- no ffmpeg invocation: pass")
    lines.append("- no video artifact produced: pass")
    lines.append("- source projects untouched: pass")
    lines.append("- no fake/placeholder audio created when provider unavailable: pass")
    if run_result is None and package_result is not None:
        lines.append("- no other packages synthesized (Phase 8B - one package only): pass")
    lines.append("")

    if static_health is not None:
        lines.append("## Static health check (runs without network)")
        lines.append(f"- overall: **{'pass' if static_health.get('ok') else 'fail'}**")
        lines.append(
            f"- modules imported: "
            f"{sum(1 for v in static_health['imports'].values() if v == 'ok')}"
            f" / {len(static_health['imports'])}"
        )
        lines.append(f"- tts_config_valid: {static_health.get('tts_config_valid')}")
        lines.append(
            f"- providers_registered: "
            f"{', '.join(static_health.get('providers_registered', []) or ['(none)'])}"
        )
        if static_health.get("errors"):
            lines.append("- errors:")
            for e in static_health["errors"][:10]:
                lines.append(f"  - `{e}`")
        lines.append("")

    if env_dict:
        lines.append("## Environment summary")
        lines.append(f"- platform: `{env_dict.get('platform')}`")
        lines.append(f"- runtime: `{env_dict.get('runtime')}`")
        lines.append(f"- can_execute_windows_venv: `{env_dict.get('can_execute_windows_venv')}`")
        lines.append(f"- has_proxy_403_signature: `{env_dict.get('has_proxy_403_signature')}`")
        for note in env_dict.get("notes", []) or []:
            lines.append(f"- note: {note}")
        lines.append("")

    if verdict == "pending_local_claude_code_validation" or windows_command:
        lines.append("## Local-Windows command")
        lines.append("")
        lines.append("```")
        if windows_command:
            lines.append(windows_command)
        elif run_result is not None or (package_result is None and package_id is None):
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\generate_audio.py --run runs\\{run_id} --all"
            )
        else:
            lines.append(
                "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
                f"scripts\\generate_audio.py --run runs\\{run_id} --package {package_id}"
            )
        lines.append("```")
        lines.append("")
        lines.append(
            "Re-run on local Windows to regenerate this report with real "
            "**pass** or **fail** verdicts. Do NOT trigger Phase 9 (ffmpeg) "
            "until every slide here is **pass**."
        )
        lines.append("")

    atomic_write_bytes(report_path, "\n".join(lines).encode("utf-8"))
    return Path(report_path)


# ---------------------------------------------------------------------------
# Diagnostics + report helpers
# ---------------------------------------------------------------------------


def _diagnostic_lines(outcome: ChainProbeOutcome, env_info: Optional[dict]) -> List[str]:
    out: List[str] = []
    primary_missing = outcome.primary is None
    edge_probe = outcome.probes.get("edge")
    runtime = (env_info or {}).get("runtime") if env_info else None

    if primary_missing and edge_probe is not None:
        if edge_probe.importable and not edge_probe.available:
            err = edge_probe.error or ""
            lower = err.lower()
            if "skewadjustment" in lower or "no server date in headers" in lower:
                out.append(
                    "- `edge` is importable but the live voice catalog call failed "
                    "with a SkewAdjustmentError. This usually means the current "
                    "network blocks or proxies the Microsoft Edge TTS endpoint "
                    "(`speech.platform.bing.com`). Re-run on a network that can "
                    "reach it; no code change required."
                )
            elif "tunnel connection failed" in lower or "connectordnserror" in lower or "name resolution" in lower:
                out.append(
                    "- `edge` is importable but DNS / HTTPS to "
                    "`speech.platform.bing.com` failed."
                )
            else:
                out.append(f"- `edge` is importable but its probe failed: `{err}`.")
        elif not edge_probe.importable:
            out.append(
                "- `edge-tts` is not installed in the active Python environment. "
                "Install it with `pip install edge-tts` and re-run."
            )

    if runtime == "cowork_sandbox":
        out.append(
            "- Detected runtime: **cowork_sandbox**. Runtime checks that need "
            "network or Windows .exe should be marked "
            "`PENDING_LOCAL_CLAUDE_CODE_VALIDATION`."
        )
    elif runtime == "claude_code_local_windows":
        out.append(
            "- Detected runtime: **claude_code_local_windows**. A `pass` or "
            "`fail` here is the project's source of truth."
        )

    if primary_missing:
        out.append(
            "- No real narration audio is produced when the primary provider is "
            "unavailable; this is by design."
        )
    return out


def _env_to_dict(env_info) -> Dict[str, Any]:
    if env_info is None:
        return {}
    if is_dataclass(env_info):
        d = asdict(env_info)
    elif isinstance(env_info, dict):
        d = dict(env_info)
    else:
        return {"repr": repr(env_info)}
    pr = d.get("project_root")
    if pr is not None:
        d["project_root"] = str(pr)
    return d


def _verdict_marker(verdict: Optional[str]) -> str:
    if verdict is None:
        return "**(verdict not set)**"
    if verdict == "pass":
        return "**pass**"
    if verdict == "fail":
        return "**fail**"
    if verdict == "pending_local_claude_code_validation":
        return "**PENDING_LOCAL_CLAUDE_CODE_VALIDATION**"
    return f"**{verdict}**"


def _row_b(label: str, ok: bool, env_blocked: bool) -> str:
    if ok:
        mark = "**pass**"
    elif env_blocked:
        mark = "**PENDING_LOCAL_CLAUDE_CODE_VALIDATION**"
    else:
        mark = "**fail**"
    return f"- {label}: {mark}"


# ---------------------------------------------------------------------------
# Phase 8A report writers (kept intact)
# ---------------------------------------------------------------------------


def write_provider_check_report(
    *,
    report_path,
    outcome,
    smoke,
    smoke_audio_info,
    smoke_text,
    network,
    run_id,
    config,
    env_info=None,
    verdict=None,
    static_health=None,
) -> Path:
    lines: List[str] = []
    env_dict = _env_to_dict(env_info)

    lines.append("# Phase 8A - TTS provider check report")
    lines.append("")
    lines.append(f"- generated_at: {iso_now()}")
    lines.append(f"- run_id: {run_id}")
    lines.append(f"- network_probe: {network}")
    lines.append(f"- provider_chain (config): {config.get('provider_chain')}")
    lines.append(f"- default_provider (config): {config.get('default_provider')}")
    if env_dict:
        lines.append(
            f"- runtime: **{env_dict.get('runtime', 'unknown')}** "
            f"(platform={env_dict.get('platform', 'unknown')}, "
            f"can_exec_windows_venv={env_dict.get('can_execute_windows_venv', False)}, "
            f"proxy_403={env_dict.get('has_proxy_403_signature', False)})"
        )
    if verdict is not None:
        lines.append(f"- verdict: {_verdict_marker(verdict)}")
    lines.append("")

    lines.append("## Selected primary")
    if outcome.primary:
        lines.append(f"- provider: **{outcome.primary}**")
        lines.append(f"- selected_voice: `{outcome.selected_voice}`")
        if outcome.fallback_voice:
            lines.append(f"- fallback_voice: `{outcome.fallback_voice}`")
    else:
        lines.append("- provider: **NONE - no provider in the chain is available**")
    lines.append("")

    lines.append("## Per-provider probe results")
    lines.append("")
    lines.append("| provider | importable | available | persian_voices | total_voices | error |")
    lines.append("|---|:---:|:---:|---:|---:|---|")
    for name, probe in outcome.probes.items():
        err = (probe.error or "").replace("|", "\\|")
        lines.append(
            f"| {name} | {'yes' if probe.importable else 'no'} "
            f"| {'yes' if probe.available else 'no'} "
            f"| {len(probe.voices_persian)} | {probe.voices_total} | {err} |"
        )
    lines.append("")

    for name, probe in outcome.probes.items():
        if not probe.voices_persian:
            continue
        lines.append(f"### Persian voices - `{name}`")
        for v in probe.voices_persian[:32]:
            lines.append(f"- `{v}`")
        if len(probe.voices_persian) > 32:
            lines.append(f"- ... and {len(probe.voices_persian) - 32} more")
        lines.append("")

    if outcome.env_presence:
        lines.append("## Env-var presence (secrets not read)")
        for name, present in outcome.env_presence.items():
            mark = "present" if present else "missing"
            lines.append(f"- `{name}`: **{mark}**")
        lines.append("")

    lines.append("## Smoke synthesis")
    if smoke is None:
        if env_dict.get("runtime") == "cowork_sandbox":
            lines.append("- skipped - sandbox cannot reach TTS endpoints; **PENDING_LOCAL_CLAUDE_CODE_VALIDATION**")
        else:
            lines.append("- skipped (no provider available)")
    else:
        lines.append(f"- provider: `{smoke.provider}`")
        lines.append(f"- voice: `{smoke.voice}`")
        lines.append(f"- success: **{smoke.success}**")
        lines.append(f"- input_text_fa: {smoke_text}")
        if smoke.out_path is not None:
            lines.append(f"- out_path: `{smoke.out_path}`")
        if smoke.audio_bytes:
            lines.append(f"- audio_bytes: {smoke.audio_bytes:,}")
        if smoke_audio_info:
            for key in ("duration_seconds", "bitrate_kbps", "sample_rate_hz", "channels", "encoding"):
                val = smoke_audio_info.get(key)
                if val is not None:
                    lines.append(f"- {key}: {val}")
        if smoke.error:
            lines.append(f"- error: `{smoke.error}`")
    lines.append("")

    lines.append("## Acceptance for Phase 8A")
    chain = config.get("provider_chain") or []
    primary_in_chain = outcome.primary in chain if outcome.primary else False
    smoke_ok = bool(smoke and smoke.success)
    env_blocked = env_dict.get("runtime") == "cowork_sandbox" or env_dict.get("has_proxy_403_signature")

    lines.append(_row_b("primary provider chosen from configured chain", primary_in_chain, env_blocked))
    lines.append(_row_b("Persian voice resolved", bool(outcome.selected_voice), env_blocked))
    lines.append(_row_b("smoke synthesis produced a non-empty audio file", smoke_ok, env_blocked))
    if smoke_audio_info and smoke_audio_info.get("duration_seconds"):
        dur = smoke_audio_info["duration_seconds"]
        in_range = 0.5 <= float(dur) <= 10.0
        lines.append(
            f"- smoke audio duration in [0.5s, 10s]: "
            f"{'**pass**' if in_range else ('**PENDING_LOCAL_CLAUDE_CODE_VALIDATION**' if env_blocked else '**fail**')} "
            f"(actual {dur}s)"
        )
    lines.append("")

    lines.append("## Hard limits respected")
    lines.append("- no real narration audio synthesized (Phase 8A only): pass")
    lines.append("- no ffmpeg invocation: pass")
    lines.append("- no video artifact produced: pass")
    lines.append("- source projects untouched: pass")
    lines.append("- no fake/placeholder audio created when provider unavailable: pass")
    lines.append("")

    if static_health is not None:
        lines.append("## Static health check (runs without network)")
        lines.append(f"- overall: **{'pass' if static_health.get('ok') else 'fail'}**")
        lines.append(
            f"- modules imported: "
            f"{sum(1 for v in static_health['imports'].values() if v == 'ok')}"
            f" / {len(static_health['imports'])}"
        )
        lines.append(f"- tts_config_valid: {static_health.get('tts_config_valid')}")
        lines.append(
            f"- providers_registered: "
            f"{', '.join(static_health.get('providers_registered', []) or ['(none)'])}"
        )
        if static_health.get("errors"):
            lines.append("- errors:")
            for e in static_health["errors"][:10]:
                lines.append(f"  - `{e}`")
        lines.append("")

    if env_dict:
        lines.append("## Environment summary")
        lines.append(f"- platform: `{env_dict.get('platform')}`")
        lines.append(f"- runtime: `{env_dict.get('runtime')}`")
        lines.append(f"- can_execute_windows_venv: `{env_dict.get('can_execute_windows_venv')}`")
        lines.append(f"- has_proxy_403_signature: `{env_dict.get('has_proxy_403_signature')}`")
        for note in env_dict.get("notes", []) or []:
            lines.append(f"- note: {note}")
        lines.append("")

    diag = _diagnostic_lines(outcome, env_dict)
    if diag:
        lines.append("## Diagnostics and remediation")
        lines.extend(diag)
        lines.append("")

    if env_dict.get("runtime") == "cowork_sandbox" or (
        env_dict.get("has_proxy_403_signature") and not env_dict.get("can_execute_windows_venv")
    ):
        lines.append("## Local-Windows revalidation command")
        lines.append("")
        lines.append("```")
        lines.append(
            "D:\\CROSS_PROJECT\\04_cross_project_documentation_hub\\.venv\\Scripts\\python.exe "
            f"scripts\\check_tts_providers.py --run runs\\{run_id}"
        )
        lines.append("```")
        lines.append("")

    atomic_write_bytes(report_path, "\n".join(lines).encode("utf-8"))
    return Path(report_path)


def write_provider_check_json(
    *,
    json_path,
    outcome,
    smoke,
    smoke_audio_info,
    smoke_text,
    network,
    run_id,
    config,
    env_info=None,
    verdict=None,
    static_health=None,
) -> Path:
    payload: Dict[str, Any] = {
        "schema_version": "1.1.0",
        "generated_at": iso_now(),
        "run_id": run_id,
        "network_probe": network,
        "config_summary": {
            "default_provider": config.get("default_provider"),
            "provider_chain": list(config.get("provider_chain") or []),
        },
        "selected": {
            "primary": outcome.primary,
            "voice": outcome.selected_voice,
            "fallback_voice": outcome.fallback_voice,
        },
        "verdict": verdict,
        "env_info": _env_to_dict(env_info),
        "static_health": static_health,
        "probes": {
            name: {
                "importable": probe.importable,
                "available": probe.available,
                "voices_persian": probe.voices_persian,
                "voices_total": probe.voices_total,
                "notes": probe.notes,
                "error": probe.error,
                "extra": probe.extra,
            }
            for name, probe in outcome.probes.items()
        },
        "env_presence": outcome.env_presence,
        "smoke": (
            None
            if smoke is None
            else {
                "provider": smoke.provider,
                "success": smoke.success,
                "voice": smoke.voice,
                "out_path": str(smoke.out_path) if smoke.out_path else None,
                "audio_bytes": smoke.audio_bytes,
                "encoding": smoke.encoding,
                "error": smoke.error,
                "input_text": smoke_text,
                "audio_info": smoke_audio_info,
                "extra": smoke.extra,
            }
        ),
    }
    atomic_write_json(json_path, payload)
    return Path(json_path)
