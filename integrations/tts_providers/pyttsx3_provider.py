"""tts_providers.pyttsx3_provider — offline SAPI/eSpeak fallback.

This provider exists ONLY as a last-ditch offline fallback for environments
without internet access. Persian voice support depends entirely on what is
installed locally (Windows SAPI, macOS NSSpeechSynthesizer, or eSpeak on
Linux). It is *not* recommended for production narration but it is useful
during environment probing.

Persian voice quality is generally poor here; do not use for the final video.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional

from .base import ProbeResult, SynthResult, TTSProvider


def _looks_persian_voice(voice) -> bool:
    """Heuristic: a voice mentions Persian/Farsi/fa-IR in any metadata field."""
    needles = ("persian", "farsi", "fa-ir", "fa_ir", "fa-")
    blobs = []
    for attr in ("name", "id", "languages"):
        val = getattr(voice, attr, None)
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            blobs.extend(str(v).lower() for v in val)
        else:
            blobs.append(str(val).lower())
    for n in needles:
        if any(n in b for b in blobs):
            return True
    return False


class Pyttsx3Provider(TTSProvider):
    name = "pyttsx3"
    requires_env: tuple = ()

    # ------------------------------------------------------------------
    # probe
    # ------------------------------------------------------------------

    def probe(self, *, network: bool = True) -> ProbeResult:
        result = ProbeResult(provider=self.name)
        try:
            import pyttsx3  # type: ignore
        except Exception as exc:
            result.error = f"pyttsx3 not importable: {exc!r}"
            return result

        result.importable = True
        result.extra["library_version"] = getattr(pyttsx3, "__version__", None) or "unknown"

        try:
            engine = pyttsx3.init()
        except Exception as exc:
            result.error = f"pyttsx3 engine init failed: {exc!r}"
            return result

        try:
            voices = engine.getProperty("voices") or []
        except Exception as exc:
            result.error = f"pyttsx3 getProperty('voices') failed: {exc!r}"
            return result

        result.voices_total = len(voices)
        persian = [getattr(v, "id", "?") for v in voices if _looks_persian_voice(v)]
        result.voices_persian = persian
        result.available = bool(persian)
        if not persian:
            result.notes.append(
                "no Persian voice detected; Persian narration will mispronounce"
            )

        return result

    def list_persian_voices(self) -> List[str]:
        try:
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            return [getattr(v, "id", "?") for v in (engine.getProperty("voices") or []) if _looks_persian_voice(v)]
        except Exception:
            return []

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
        if not text or not text.strip():
            return SynthResult(
                provider=self.name,
                success=False,
                voice=voice,
                error="empty text",
            )

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            import pyttsx3  # type: ignore
        except Exception as exc:
            return SynthResult(
                provider=self.name,
                success=False,
                voice=voice,
                error=f"pyttsx3 not importable: {exc!r}",
            )

        try:
            engine = pyttsx3.init()
        except Exception as exc:
            return SynthResult(
                provider=self.name,
                success=False,
                voice=voice,
                error=f"pyttsx3 engine init failed: {exc!r}",
            )

        if voice:
            try:
                engine.setProperty("voice", voice)
            except Exception:
                pass

        # pyttsx3 only writes .wav reliably; rename to whatever extension was
        # requested but mark encoding so callers know.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=out_path.name + ".tmp.",
            suffix=".wav",
            dir=str(out_path.parent),
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        try:
            engine.save_to_file(text, str(tmp_path))
            engine.runAndWait()
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SynthResult(
                provider=self.name,
                success=False,
                voice=voice,
                error=f"pyttsx3 synthesis failed: {exc!r}",
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
                voice=voice,
                error=f"atomic move failed: {exc!r}",
            )

        return SynthResult(
            provider=self.name,
            success=True,
            voice=voice,
            out_path=out_path,
            audio_bytes=out_path.stat().st_size,
            encoding="wav",
        )
