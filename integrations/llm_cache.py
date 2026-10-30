"""llm_cache - content-addressed cache for LLM/vision results.

Key = sha256 of {stage, capability, provider, model, prompt_version, payload}.
A cache hit means NO paid call is needed. Changing model/prompt_version changes
the key (deliberate invalidation).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional


def compute_key(parts: dict) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class LLMCache:
    def __init__(self, cache_dir, *, enabled: bool = True):
        self.dir = Path(cache_dir)
        self.enabled = bool(enabled)
        self.hits = 0
        self.misses = 0
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                self.enabled = False

    def get(self, key: str) -> Optional[dict]:
        if not self.enabled:
            return None
        p = self.dir / (key + ".json")
        if p.is_file():
            self.hits += 1
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        self.misses += 1
        return None

    def has(self, key: str) -> bool:
        return self.enabled and (self.dir / (key + ".json")).is_file()

    def set(self, key: str, value: dict) -> None:
        if not self.enabled:
            return
        p = self.dir / (key + ".json")
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))

    def stats(self) -> dict:
        return {"enabled": self.enabled, "dir": str(self.dir),
                "hits": self.hits, "misses": self.misses}
