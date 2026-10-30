"""tts_providers.gtts_provider — Google Translate TTS via the ``gTTS`` lib.

Optional fallback only. The Persian voice quality is noticeably lower than
Edge, and the endpoint is rate-limited by Google. We include it because it
is anonymous and runs everywhere with ``pip install gTTS``.

Notes
-----
* No API key.
* gTTS does not expose multiple Persian voices — there is one ``lang="fa"``
  voice. The ``slow`` flag toggles pacing.
* Failures are surfaced via the result objects, never raised.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional

from .base import ProbeResult, SynthResult, TTSProvider


class GTTSProvider(TTSProvider):
    name = "gtts"
    requires_env: tuple = ()

    DEFAULT_VOICE = "fa"  # gTTS uses language codes, not voice ids
    FALLBACK_VOICES = ()

    # ------------------------------------------------------------------
    # probe
    # ------------------------------------------------------------------

    def probe(self, *, network: bool = True) -> ProbeResult:
        result = ProbeResult(provider=self.name)
        try:
            import gtts  # type: ignore
        except Exception as exc:
            result.error = f"gTTS not importable: {exc!r}"
            return result

        result.importable = True
        version = getattr(gtts, "__version__", None)
        if version:
            result.extra["library_version"] = version

        # gTTS doesn't expose a "list voices" endpoint; the supported set is
        # a static lang list inside the library.
        try:
            from gtts.lang import tts_langs  # type: ignore
            langs = tts_langs()
            result.voices_total = len(langs)
            if "fa" in langs:
                result.voices_persian = ["fa"]
                result.available = True
            else:
                result.error = "gTTS lang map missing 'fa'"
        except Exception as exc:
            result.error = f"gTTS lang lookup failed: {exc!r}"

        if not network:
            result.notes.append("network probe skipped (offline mode)")

        return result

    def list_persian_voices(self) -> List[str]:
        return ["fa"]

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
        chosen_voice = voice or self.config.get("voice") or self.DEFAULT_VOICE
        slow = bool(self.config.get("slow", False))

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
            from gtts import gTTS  # type: ignore
        except Exception as exc:
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error=f"gTTS not importable: {exc!r}",
            )

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=out_path.name + ".tmp.",
            suffix=".mp3",
            dir=str(out_path.parent),
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        try:
            tts = gTTS(text=text, lang=chosen_voice, slow=slow)
            tts.save(str(tmp_path))
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SynthResult(
                provider=self.name,
                success=False,
                voice=chosen_voice,
                error=f"gTTS synthesis failed: {exc!r}",
            )

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
            extra={"slow": slow},
        )
