"""tts_providers.edge — Microsoft Edge TTS (anonymous, free).

This is the MVP provider per ``configs/tts_config.json`` (``provider_chain:
["edge"]``). It uses the open-source ``edge-tts`` Python package to talk to
the public Microsoft Edge TTS endpoint. No API key is required.

Persian voices known to exist on the Microsoft catalog (as of 2026):
    * ``fa-IR-FaridNeural``   (male, default)
    * ``fa-IR-DilaraNeural``  (female, fallback)

A *probe* with ``network=True`` lists voices from the live endpoint.
A *synthesize* call streams audio chunks to a tmp file and atomically renames
to ``out_path``.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from .base import ProbeResult, SynthResult, TTSProvider


class EdgeTTSProvider(TTSProvider):
    name = "edge"
    requires_env: tuple = ()  # anonymous

    DEFAULT_VOICE = "fa-IR-FaridNeural"
    FALLBACK_VOICES = ("fa-IR-DilaraNeural",)

    # ------------------------------------------------------------------
    # probe
    # ------------------------------------------------------------------

    def probe(self, *, network: bool = True) -> ProbeResult:
        result = ProbeResult(provider=self.name)
        try:
            import edge_tts  # type: ignore
        except Exception as exc:  # pragma: no cover - import error surface
            result.error = f"edge-tts not importable: {exc!r}"
            return result

        result.importable = True
        version = getattr(edge_tts, "__version__", None)
        if version:
            result.extra["library_version"] = version

        if not network:
            result.notes.append("network probe skipped (offline mode)")
            # We can still report the known Persian voice list.
            result.voices_persian = [self.DEFAULT_VOICE, *self.FALLBACK_VOICES]
            result.available = True  # best-effort; will fail at synth if offline
            return result

        # Live probe: list voices and filter Persian (fa-*).
        try:
            voices = asyncio.run(self._list_voices_async())
        except Exception as exc:
            result.error = f"voice-list call failed: {exc!r}"
            return result

        result.voices_total = len(voices)
        persian = sorted(
            {v["ShortName"] for v in voices if str(v.get("Locale", "")).startswith("fa-")}
        )
        result.voices_persian = persian

        if persian:
            result.available = True
        else:
            result.error = "no Persian voices returned by Edge endpoint"

        return result

    async def _list_voices_async(self) -> list:
        import edge_tts  # type: ignore

        return await edge_tts.list_voices()

    # ------------------------------------------------------------------
    # voices
    # ------------------------------------------------------------------

    def list_persian_voices(self) -> List[str]:
        try:
            voices = asyncio.run(self._list_voices_async())
        except Exception:
            return [self.DEFAULT_VOICE, *self.FALLBACK_VOICES]
        return sorted(
            {v["ShortName"] for v in voices if str(v.get("Locale", "")).startswith("fa-")}
        )

    # ------------------------------------------------------------------
    # synthesize
    # ------------------------------------------------------------------

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
        chosen_voice = (
            voice
            or self.config.get("voice")
            or self.DEFAULT_VOICE
        )
        chosen_rate = rate if rate is not None else self.config.get("rate", "+0%")
        chosen_vol = volume if volume is not None else self.config.get("volume", "+0%")
        chosen_pitch = pitch if pitch is not None else self.config.get("pitch", "+0Hz")

        if not text or not text.strip():
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error="empty text",
            )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            import edge_tts  # type: ignore
        except Exception as exc:
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error=f"edge-tts not importable: {exc!r}",
            )

        # Write to a tmp file in the same directory then os.replace.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=out_path.name + ".tmp.",
            suffix=".mp3",
            dir=str(out_path.parent),
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        try:
            asyncio.run(
                self._synthesize_async(
                    text=text,
                    voice=chosen_voice,
                    rate=chosen_rate,
                    volume=chosen_vol,
                    pitch=chosen_pitch,
                    tmp_path=tmp_path,
                )
            )
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error=f"edge-tts synthesis failed: {exc!r}",
            )

        # Atomic move into place.
        try:
            os.replace(tmp_path, out_path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error=f"atomic move failed: {exc!r}",
            )

        size = out_path.stat().st_size
        return SynthResult(
            provider=self.name,
            success=True,
            voice=chosen_voice,
            out_path=out_path,
            audio_bytes=size,
            encoding="mp3",
            extra={
                "rate": chosen_rate,
                "volume": chosen_vol,
                "pitch": chosen_pitch,
            },
        )

    async def _synthesize_async(
        self,
        *,
        text: str,
        voice: str,
        rate: str,
        volume: str,
        pitch: str,
        tmp_path: Path,
    ) -> None:
        import edge_tts  # type: ignore

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        with open(tmp_path, "wb") as fh:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    fh.write(chunk["data"])
