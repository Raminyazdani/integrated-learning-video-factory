"""tts_providers.registry — name → provider class lookup.

Adding a new provider:

    1. Implement ``TTSProvider`` in a sibling module.
    2. Register it in ``_REGISTRY`` below.
    3. Add the provider key to ``configs/tts_config.json`` ``providers``.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from .base import TTSProvider
from .edge import EdgeTTSProvider
from .gtts_provider import GTTSProvider
from .pyttsx3_provider import Pyttsx3Provider
from .gemini_provider import GeminiTTSProvider

_REGISTRY: Dict[str, Type[TTSProvider]] = {
    "edge": EdgeTTSProvider,
    "gtts": GTTSProvider,
    "pyttsx3": Pyttsx3Provider,
    "gemini_tts": GeminiTTSProvider,  # PAID HQ provider (fail-closed)
}


def is_paid_provider(name: str) -> bool:
    cls = _REGISTRY.get(name)
    return bool(getattr(cls, "paid", False))


def available_provider_names() -> List[str]:
    """All providers known to this code base (regardless of env availability)."""
    return list(_REGISTRY.keys())


def get_provider(name: str, config: Optional[dict] = None) -> TTSProvider:
    """Instantiate the provider class registered for ``name``.

    Raises ``KeyError`` if no provider is registered. The caller is expected
    to validate the name against ``configs/tts_config.json`` first.
    """
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown TTS provider: {name!r}. "
            f"known: {sorted(_REGISTRY.keys())}"
        ) from exc
    return cls(config=config or {})
