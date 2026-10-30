"""tts_providers.base — abstract base class and shared result dataclasses.

Every provider implementation lives in its own module under
``integrations/tts_providers/`` and registers itself in ``registry.py``.

Design decisions
----------------
* Provider methods return *result objects*, not exceptions, so the agent
  can decide between strict-fail and chain-failover behavior without
  catching broadly.
* Providers must never write outside the ``out_path`` they are handed.
* Providers must never log secret values.
* Probing must be cheap and idempotent — no network synthesis during a probe
  beyond what's required to confirm reachability (a tiny voice-catalog
  request is OK; full synthesis is NOT a probe).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class ProbeResult:
    """Outcome of probing a single provider.

    A provider that is *available* means it can synthesize given the
    current environment. A provider that is *importable* might still be
    unavailable if its API key is missing or the network is unreachable.
    """
    provider: str
    importable: bool = False
    available: bool = False
    voices_persian: List[str] = field(default_factory=list)
    voices_total: int = 0
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None
    # Free-form, non-secret metadata (library version, endpoint, etc.).
    extra: dict = field(default_factory=dict)


@dataclass
class SynthResult:
    """Outcome of a single synthesis call."""
    provider: str
    success: bool
    voice: Optional[str] = None
    out_path: Optional[Path] = None
    audio_bytes: int = 0
    duration_seconds: Optional[float] = None
    sample_rate_hz: Optional[int] = None
    bitrate_kbps: Optional[float] = None
    encoding: Optional[str] = None
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


class TTSProvider(abc.ABC):
    """Abstract TTS provider."""

    #: Stable provider id matching ``configs/tts_config.json`` keys.
    name: str = "base"
    #: Names of env vars required for the provider to be *available*.
    requires_env: tuple = ()

    def __init__(self, config: Optional[dict] = None):
        self.config: dict = dict(config or {})

    # ----- probing --------------------------------------------------------

    @abc.abstractmethod
    def probe(self, *, network: bool = True) -> ProbeResult:
        """Check if the provider can be used in the current environment.

        ``network=False`` skips any network call (offline-friendly probe).
        Implementations must return a populated ``ProbeResult`` even on
        failure (set ``error`` and ``available=False``).
        """

    # ----- voices ---------------------------------------------------------

    @abc.abstractmethod
    def list_persian_voices(self) -> List[str]:
        """Return Persian voice ids supported by this provider (best effort)."""

    def resolve_voice(self, preferred: Optional[str] = None) -> Optional[str]:
        """Pick a Persian voice. Prefers ``preferred``, then config default,
        then the first Persian voice from the catalog."""
        if preferred:
            return preferred
        cfg_voice = self.config.get("voice")
        if cfg_voice:
            return cfg_voice
        catalog = self.list_persian_voices()
        return catalog[0] if catalog else None

    # ----- synthesis ------------------------------------------------------

    @abc.abstractmethod
    def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        volume: Optional[str] = None,
        pitch: Optional[str] = None,
    ) -> SynthResult:
        """Synthesize ``text`` to ``out_path``.

        Implementations must write to a tmp file then atomically rename,
        or otherwise guarantee that ``out_path`` only exists if synthesis
        succeeded.
        """
