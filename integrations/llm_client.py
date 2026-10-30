"""llm_client - provider-agnostic LLM/Vision client (SKELETON; dry-run first).

Safety design (no paid call possible by default):
  - provider defaults to "manual" (no-op → returns None → callers fall back to
    authored files). Manual mode never touches the network.
  - dry-run (pipeline mode `llm_dry_run` or config mode `dry_run`) only RECORDS
    planned calls + cost estimates; returns None; no network.
  - paid providers (anthropic/openai/gemini) are PLACEHOLDERS: even when reached,
    the adapter raises (not implemented) — and `paid_allowed()` is False unless
    ALL of: pipeline_mode==llm_paid, paid provider, approval, API key present,
    known budget. Missing API key never crashes manual/dry-run.
  - API keys are read from env and NEVER printed; reports show presence only.

Capabilities: vision_page_review, generate_content, package_titles,
leakage_check, qa_review. Each returns None in manual/dry-run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from integrations.llm_cache import LLMCache, compute_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "llm_config.yaml"

_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
_PAID_PROVIDERS = ("anthropic", "openai", "gemini")
_CAP_DEFAULT_TOKENS = {
    "visual_review": (1200, 500),
    "content_generation": (2500, 2000),
    "package_titles": (800, 200),
    "leakage_check": (1500, 200),
    "qa_review": (2000, 600),
}


SCHEMA_VERSION = "1.0.0"

# capability -> cache subfolder under runs/<run_id>/cache/llm/
STAGE_FOLDERS = {
    "visual_review": "page_review",
    "package_titles": "package_metadata",
    "content_generation": "package_content",
    "leakage_check": "qa",
    "qa_review": "qa",
}

# Python SDK module names per provider (optional import; never required for dry-run).
_PROVIDER_SDK = {"anthropic": "anthropic", "openai": "openai", "gemini": "google.generativeai"}


def provider_sdk_available(provider: str) -> bool:
    import importlib.util
    mod = _PROVIDER_SDK.get(provider)
    if not mod:
        return False
    try:
        return importlib.util.find_spec(mod.split(".")[0]) is not None
    except Exception:
        return False


class LLMClientError(RuntimeError):
    pass


def load_llm_config(path=None) -> dict:
    p = Path(path) if path else _DEFAULT_CONFIG
    if not p.is_file():
        raise LLMClientError(f"llm_config.yaml not found at {p}")
    try:
        import yaml
    except Exception as exc:  # pragma: no cover
        raise LLMClientError(f"PyYAML required for llm_config.yaml: {exc!r}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg


def _mask_key(secret: str) -> str:
    """Never reveal the secret. Report presence + length only."""
    if not secret:
        return "absent"
    return f"present(len={len(secret)})"


@dataclass
class PlannedCall:
    stage: str
    capability: str
    cache_key: str
    cached: bool
    est_input_tokens: int
    est_output_tokens: int
    est_cost_usd: float
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    cache_folder: str = ""
    target_id: str = ""


class LLMClient:
    def __init__(self, config: dict, *, run_dir=None, pipeline_mode: str = None, approved: bool = False):
        self.cfg = dict(config or {})
        self.provider = str(self.cfg.get("provider", "manual"))
        self.model = str(self.cfg.get("model", "") or "")
        self.client_mode = str(self.cfg.get("mode", "manual"))  # manual | dry_run | live
        self.pipeline_mode = pipeline_mode
        self.approved = bool(approved)
        self.max_cost = float(self.cfg.get("max_cost_per_run", 0) or 0)
        self.fail_closed_if_budget_unknown = bool(self.cfg.get("fail_closed_if_budget_unknown", True))
        self.redact = bool(self.cfg.get("redact_api_keys_in_reports", True))
        self.allowed_stages = self.cfg.get("allowed_stages", {}) or {}
        self.prompt_versions = self.cfg.get("prompt_versions", {}) or {}
        self.rate_in = float(self.cfg.get("rate_per_1k_input_usd", 0.003) or 0)
        self.rate_out = float(self.cfg.get("rate_per_1k_output_usd", 0.015) or 0)

        self.run_dir = Path(run_dir).resolve() if run_dir else None
        self.run_id = self.run_dir.name if self.run_dir else (self.cfg.get("run_id") or "")
        self.input_sha = ""
        if self.run_dir:
            self.input_sha = self._compute_input_sha(self.run_dir)
        # per-run cache root: runs/<run_id>/cache/llm/<stage_folder>/
        if self.run_dir:
            self._cache_root = self.run_dir / "cache" / "llm"
        else:
            self._cache_root = Path(self.cfg.get("cache_dir", "llm_cache"))
        self._cache_enabled = bool(self.cfg.get("cache_enabled", True))
        self._stage_caches = {}
        # legacy attribute kept for usage_report compatibility
        self.cache = LLMCache(self._cache_root, enabled=self._cache_enabled)

        env_var = _PROVIDER_ENV.get(self.provider)
        self._api_key = os.environ.get(env_var, "") if env_var else ""

        self.planned: List[PlannedCall] = []
        self.spent_usd = 0.0
        self.failures: List[str] = []

    @staticmethod
    def _compute_input_sha(run_dir: Path) -> str:
        import hashlib
        # source copy under runs/<id>/source/, else the extraction manifest source_file
        for cand in sorted((run_dir / "source").glob("*")) if (run_dir / "source").exists() else []:
            try:
                return "sha256:" + hashlib.sha256(cand.read_bytes()).hexdigest()[:16]
            except Exception:
                pass
        return ""

    def _cache_for(self, capability: str) -> "LLMCache":
        folder = STAGE_FOLDERS.get(capability, capability)
        if folder not in self._stage_caches:
            self._stage_caches[folder] = LLMCache(self._cache_root / folder, enabled=self._cache_enabled)
        return self._stage_caches[folder]

    # ---- safety / policy ----
    def is_paid_provider(self) -> bool:
        return self.provider in _PAID_PROVIDERS

    def is_dry_run(self) -> bool:
        return self.client_mode == "dry_run" or self.pipeline_mode == "llm_dry_run"

    def stage_enabled(self, stage: str) -> bool:
        return bool(self.allowed_stages.get(stage, True))

    def paid_allowed(self) -> bool:
        """Paid call possible ONLY if every guard passes. Default config => False."""
        try:
            from integrations.pipeline_mode import mode_allows_paid
            if self.pipeline_mode is not None and not mode_allows_paid(self.pipeline_mode):
                return False
        except Exception:
            return False
        if not self.is_paid_provider():
            return False
        if self.is_dry_run():
            return False
        if self.cfg.get("require_user_approval_before_paid_calls", True) and not self.approved:
            return False
        if not self._api_key:
            return False
        if self.max_cost <= 0 and self.fail_closed_if_budget_unknown:
            return False
        return True

    def assert_paid_allowed(self, stage: str) -> None:
        if not self.paid_allowed():
            raise LLMClientError(
                f"Paid LLM call for {stage!r} is NOT allowed (fail-closed): "
                f"provider={self.provider}, pipeline_mode={self.pipeline_mode}, "
                f"approved={self.approved}, key={_mask_key(self._api_key)}, "
                f"max_cost={self.max_cost}. No network call attempted."
            )

    def key_status(self) -> str:
        return _mask_key(self._api_key)

    # ---- cost ----
    def _estimate_cost(self, in_tok: int, out_tok: int) -> float:
        return round(in_tok / 1000.0 * self.rate_in + out_tok / 1000.0 * self.rate_out, 4)

    def _cache_key(self, stage: str, capability: str, payload: dict) -> str:
        return compute_key({
            "run_id": self.run_id,
            "input_sha": self.input_sha,
            "schema_version": SCHEMA_VERSION,
            "stage": stage, "capability": capability,
            "provider": self.provider, "model": self.model,
            "prompt_version": self.prompt_versions.get(capability, "v1"),
            "payload": payload,
        })

    # ---- core dispatch ----
    def _invoke(self, stage: str, capability: str, payload: dict, *, target_id: str = "") -> Optional[dict]:
        if not self.stage_enabled(stage):
            return None
        cache = self._cache_for(capability)
        key = self._cache_key(stage, capability, payload)
        cached = cache.get(key)
        in_tok, out_tok = _CAP_DEFAULT_TOKENS.get(capability, (1500, 800))
        est = self._estimate_cost(in_tok, out_tok)
        self.planned.append(PlannedCall(
            stage, capability, key, cached is not None, in_tok, out_tok, est,
            provider=self.provider, model=self.model or "(unset)",
            prompt_version=self.prompt_versions.get(capability, "v1"),
            cache_folder=STAGE_FOLDERS.get(capability, capability), target_id=target_id,
        ))

        if cached is not None:
            return cached
        if self.provider == "manual":
            return None  # no-op → caller falls back to authored files
        if self.is_dry_run():
            return None  # plan only, no network
        # paid path — fail-closed BEFORE any network/SDK use
        self.assert_paid_allowed(stage)
        if self.spent_usd + est > self.max_cost:
            raise LLMClientError(f"budget exceeded: spent {self.spent_usd}+{est} > {self.max_cost}")
        if not self.model:
            raise LLMClientError("no model selected (config.model empty); fail-closed.")
        if not provider_sdk_available(self.provider):
            raise LLMClientError(
                f"{self.provider!r} SDK not installed (optional). Install it for paid runs; "
                f"no paid call attempted."
            )
        # Real provider call (only reachable when ALL gates pass — never in this phase).
        result = self._adapter_complete(capability, payload)
        if result is not None:
            cache.set(key, result)
            self.spent_usd += est
        return result

    def _adapter_complete(self, capability: str, payload: dict) -> Optional[dict]:
        """Provider call. Lazily imports the SDK. Only reached after all paid gates
        pass (provider+model+key+budget+approval+SDK). NOT exercised in dry-run/this phase."""
        # Implemented as a guarded structure; concrete request bodies are filled in 11B
        # once a key+budget are approved. Until then this raises clearly (no silent stub).
        raise LLMClientError(
            f"{self.provider!r} adapter request body is implemented in Phase 11B (paid pilot). "
            f"All dry-run/manual paths are complete; no paid call is made now."
        )

    # ---- capabilities (all return None in manual/dry-run) ----
    def vision_page_review(self, *, image_path=None, page_text="", page_id=None) -> Optional[dict]:
        return self._invoke("visual_review", "visual_review",
                            {"image_path": str(image_path) if image_path else None,
                             "page_text": page_text, "page_id": page_id},
                            target_id=page_id or "")

    def generate_content(self, *, package_id=None, package_evidence=None) -> Optional[dict]:
        return self._invoke("content_generation", "content_generation",
                            {"package_id": package_id, "evidence": package_evidence},
                            target_id=package_id or "")

    def package_titles(self, *, package_id=None, evidence=None) -> Optional[dict]:
        return self._invoke("package_titles", "package_titles",
                            {"package_id": package_id, "evidence": evidence},
                            target_id=package_id or "")

    def leakage_check(self, *, text="", banned_terms=None, target_id="") -> Optional[dict]:
        return self._invoke("qa_review", "leakage_check",
                            {"text": text, "banned_terms": banned_terms or []}, target_id=target_id)

    def qa_review(self, *, artifact=None, target_id="") -> Optional[dict]:
        return self._invoke("qa_review", "qa_review", {"artifact": artifact}, target_id=target_id)

    # ---- required capability aliases (Phase 11A naming) ----
    def package_metadata(self, *, package_id=None, evidence=None):
        return self.package_titles(package_id=package_id, evidence=evidence)

    def persian_package_content(self, *, package_id=None, package_evidence=None):
        return self.generate_content(package_id=package_id, package_evidence=package_evidence)

    def content_leakage_check(self, *, text="", banned_terms=None, target_id=""):
        return self.leakage_check(text=text, banned_terms=banned_terms, target_id=target_id)

    def optional_qa_review(self, *, artifact=None, target_id=""):
        return self.qa_review(artifact=artifact, target_id=target_id)

    # ---- dry-run plan ----
    def dry_run_plan(self) -> dict:
        """Structured plan of what WOULD be called (no network). Call capabilities
        first (they append to self.planned), then read this."""
        items = [{
            "stage": p.stage, "capability": p.capability, "target_id": p.target_id,
            "provider": p.provider, "model": p.model, "prompt_version": p.prompt_version,
            "cache_folder": p.cache_folder, "cache_key": p.cache_key[:16] + "…",
            "cache_expectation": "HIT (reuse, no call)" if p.cached else "MISS (would call)",
            "est_input_tokens": p.est_input_tokens, "est_output_tokens": p.est_output_tokens,
            "est_cost_usd": p.est_cost_usd,
        } for p in self.planned]
        uncached = [p for p in self.planned if not p.cached]
        return {
            "run_id": self.run_id, "input_sha": self.input_sha or "(none)",
            "provider": self.provider, "model": self.model or "(unset)",
            "pipeline_mode": self.pipeline_mode, "client_mode": self.client_mode,
            "paid_allowed": self.paid_allowed(),
            "provider_sdk_available": provider_sdk_available(self.provider),
            "api_key": self.key_status(),
            "budget_max_cost_per_run": self.max_cost,
            "budget_status": ("set" if self.max_cost > 0 else "UNKNOWN (fail-closed)"),
            "planned_calls": len(items), "cache_misses_would_call": len(uncached),
            "est_total_cost_usd": round(sum(p.est_cost_usd for p in uncached), 4),
            "cache_root": str(self._cache_root),
            "items": items,
            "no_paid_calls_made": True,
        }

    # ---- reporting ----
    def usage_report(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model or "(unset)",
            "client_mode": self.client_mode,
            "pipeline_mode": self.pipeline_mode,
            "approved": self.approved,
            "api_key": self.key_status() if self.redact else self._api_key,
            "paid_allowed": self.paid_allowed(),
            "max_cost_per_run": self.max_cost,
            "fail_closed_if_budget_unknown": self.fail_closed_if_budget_unknown,
            "run_id": self.run_id,
            "input_sha": self.input_sha or "(none)",
            "provider_sdk_available": provider_sdk_available(self.provider),
            "planned_calls": len(self.planned),
            "cache_hits_planned": sum(1 for p in self.planned if p.cached),
            "est_total_cost_usd": round(sum(p.est_cost_usd for p in self.planned if not p.cached), 4),
            "spent_usd": self.spent_usd,
            "cache_root": str(self._cache_root),
            "failures": self.failures,
            "no_paid_calls_made": self.spent_usd == 0.0,
        }

    def write_dry_run_report(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pl = self.dry_run_plan()
        lines = [
            "# LLM dry-run plan", "",
            f"- run_id: {pl['run_id']}  · input_sha: {pl['input_sha']}",
            f"- provider/model: {pl['provider']} / {pl['model']}  · SDK available: {pl['provider_sdk_available']}",
            f"- pipeline_mode: {pl['pipeline_mode']}  · paid_allowed: **{pl['paid_allowed']}**",
            f"- api_key: {pl['api_key']}  (never printed in clear)",
            f"- budget: {pl['budget_status']} (max_cost_per_run={pl['budget_max_cost_per_run']} USD)",
            f"- planned calls: {pl['planned_calls']}  · cache MISS (would call): {pl['cache_misses_would_call']}",
            f"- estimated cost (uncached): {pl['est_total_cost_usd']} USD",
            f"- cache root: {pl['cache_root']}",
            f"- **no paid calls made: {pl['no_paid_calls_made']}**",
            "", "| stage | capability | target | model | prompt | cache | cache expectation | est$ |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for it in pl["items"]:
            lines.append(f"| {it['stage']} | {it['capability']} | {it['target_id']} | {it['model']} | "
                         f"{it['prompt_version']} | {it['cache_folder']} | {it['cache_expectation']} | {it['est_cost_usd']} |")
        lines += ["", "## Exact paid-pilot command (Phase 11B)",
                  "```",
                  "set ANTHROPIC_API_KEY=...   (do not commit)",
                  "python scripts/run_pipeline.py convert <token> --mode llm_paid --approve-paid",
                  "# requires: configs/llm_config.yaml provider+model set, max_cost_per_run>0, SDK installed",
                  "```", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def write_usage_report(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rep = self.usage_report()
        lines = ["# LLM usage report", "",
                 f"- provider: {rep['provider']} · model: {rep['model']}",
                 f"- client_mode: {rep['client_mode']} · pipeline_mode: {rep['pipeline_mode']}",
                 f"- api_key: {rep['api_key']}  (never printed in clear when redact=true)",
                 f"- paid_allowed: **{rep['paid_allowed']}**",
                 f"- max_cost_per_run: {rep['max_cost_per_run']} USD",
                 f"- planned_calls: {rep['planned_calls']} (cache hits: {rep['cache_hits_planned']})",
                 f"- est_total_cost (uncached): {rep['est_total_cost_usd']} USD",
                 f"- spent: {rep['spent_usd']} USD",
                 f"- cache: {rep['cache']}",
                 f"- failures: {rep['failures'] or 'none'}",
                 ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
