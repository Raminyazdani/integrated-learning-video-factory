"""tts_providers — pluggable TTS provider chain.

Public surface:

    from integrations.tts_providers import (
        TTSProvider, ProbeResult, SynthResult,
        get_provider, available_provider_names,
    )

Each provider in this package implements ``TTSProvider`` (see ``base.py``).
The registry in ``registry.py`` is the single entry point used by the
``tts_audio_agent`` and the ``check_tts_providers`` script.
"""
from __future__ import annotations

from .base import ProbeResult, SynthResult, TTSProvider
from .registry import available_provider_names, get_provider

__all__ = [
    "ProbeResult",
    "SynthResult",
    "TTSProvider",
    "available_provider_names",
    "get_provider",
]
