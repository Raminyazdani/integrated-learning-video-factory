"""tts_providers.gemini_provider — Gemini native TTS (PAID, target final voice).

Ported from the working ml_videos provider. Uses the modern **google-genai** SDK
(`from google import genai`), NOT google-generativeai. Credentials come from
GEMINI_API_KEY (preferred) or GOOGLE_API_KEY (fallback), loaded from .env.local
via integrations/env_loader. Gemini returns raw 16-bit/24kHz mono PCM, wrapped to WAV.

Fail-closed: requires SDK + API key present. Never prints/stores the key (redacted
in errors). Model fallback only on model-not-found; other errors surface.
"""
from __future__ import annotations

import importlib.util
import os
import wave
from pathlib import Path
from typing import List, Optional, Tuple

from .base import ProbeResult, SynthResult, TTSProvider

try:
    from integrations.env_loader import load_env_local
    load_env_local()  # populate os.environ from .env.local (existing env wins)
except Exception:
    pass

_DEFAULT_VOICES = ["Kore", "Aoede", "Puck"]
_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
_PCM_RATE, _PCM_WIDTH, _PCM_CH = 24000, 2, 1
_NOT_FOUND = ("404", "not_found", "not found", "model not found", "is not available",
              "is not supported", "not supported", "permission denied for model", "no such model")


def _genai():
    try:
        from google import genai
        from google.genai import types
        return genai, types, None
    except Exception as e:
        return None, None, repr(e)


def _candidate_env(cfg) -> List[str]:
    raw = cfg.get("api_key_env")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list) and raw:
        return [s for s in raw if isinstance(s, str) and s.strip()]
    return ["GEMINI_API_KEY", "GOOGLE_API_KEY"]


def _resolve_key(cfg) -> Tuple[Optional[str], Optional[str]]:
    for n in _candidate_env(cfg):
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip(), n
    return None, None


def _models(cfg) -> List[str]:
    out = []
    primary = (cfg.get("model") or _DEFAULT_MODEL).strip()
    if primary:
        out.append(primary)
    for m in cfg.get("fallback_models") or []:
        if isinstance(m, str) and m.strip() and m.strip() not in out:
            out.append(m.strip())
    return out


def _redact(secret, msg):
    return msg.replace(secret, "***REDACTED***") if (secret and secret in msg) else msg


def _pcm_to_wav(pcm: bytes, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(_PCM_CH); wf.setsampwidth(_PCM_WIDTH); wf.setframerate(_PCM_RATE)
        wf.writeframes(pcm)


def _extract_audio(resp) -> Optional[bytes]:
    try:
        for cand in getattr(resp, "candidates", None) or []:
            for part in getattr(getattr(cand, "content", None), "parts", None) or []:
                inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
                data = getattr(inline, "data", None) if inline else None
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
                if isinstance(data, str):
                    import base64
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        pass
    except Exception:
        return None
    return None


class GeminiTTSProvider(TTSProvider):
    name = "gemini_tts"
    requires_env: tuple = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    paid: bool = True
    last_model_used: Optional[str] = None

    def sdk_importable(self) -> bool:
        return importlib.util.find_spec("google.genai") is not None or importlib.util.find_spec("google") is not None

    def key_present(self) -> bool:
        k, _ = _resolve_key(self.config)
        return bool(k)

    def probe(self, *, network: bool = True) -> ProbeResult:
        r = ProbeResult(provider=self.name)
        genai, _types, err = _genai()
        r.importable = genai is not None
        r.extra["paid"] = True
        r.extra["model"] = (self.config.get("model") or _DEFAULT_MODEL)
        r.extra["api_key_present"] = self.key_present()
        if not r.importable:
            r.error = f"google-genai SDK not importable: {err}"
            return r
        if not self.key_present():
            r.error = "GEMINI_API_KEY / GOOGLE_API_KEY absent (fail-closed)"
            return r
        r.available = True
        r.notes.append("SDK + key present; reachability confirmed at synth time")
        if not network:
            r.notes.append("network probe skipped")
        return r

    def list_persian_voices(self) -> List[str]:
        return self.config.get("voices") or _DEFAULT_VOICES

    def resolve_voice(self, preferred: str = None) -> str:
        return preferred or self.config.get("selected_voice") or self.config.get("voice") or (self.list_persian_voices()[0])

    def synthesize(self, text, out_path, *, voice=None, rate=None, volume=None, pitch=None) -> SynthResult:
        chosen = voice or self.resolve_voice()
        out_path = Path(out_path)
        if out_path.suffix.lower() != ".wav":
            out_path = out_path.with_suffix(".wav")
        genai, types, err = _genai()
        if genai is None:
            return SynthResult(provider=self.name, success=False, voice=chosen,
                               error=f"fail-closed: SDK not importable ({err})")
        api_key, _env = _resolve_key(self.config)
        if not api_key:
            return SynthResult(provider=self.name, success=False, voice=chosen,
                               error="fail-closed: no GEMINI_API_KEY/GOOGLE_API_KEY")
        if not text or not text.strip():
            return SynthResult(provider=self.name, success=False, voice=chosen, error="empty text")
        client = genai.Client(api_key=api_key)
        gen_cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=chosen))),
        )
        models = _models(self.config)
        last_err = None
        for idx, model in enumerate(models):
            try:
                resp = client.models.generate_content(model=model, contents=text, config=gen_cfg)
            except Exception as e:
                msg = _redact(api_key, f"{type(e).__name__}: {e}")
                last_err = msg
                if any(m in msg.lower() for m in _NOT_FOUND) and idx + 1 < len(models):
                    continue
                return SynthResult(provider=self.name, success=False, voice=chosen, error=msg)
            audio = _extract_audio(resp)
            if not audio:
                return SynthResult(provider=self.name, success=False, voice=chosen,
                                   error=f"model {model} returned no audio")
            try:
                _pcm_to_wav(audio, out_path)
            except Exception as e:
                return SynthResult(provider=self.name, success=False, voice=chosen, error=f"wav write failed: {e!r}")
            if out_path.exists() and out_path.stat().st_size > 512:
                GeminiTTSProvider.last_model_used = model
                return SynthResult(provider=self.name, success=True, voice=chosen,
                                   out_path=out_path, audio_bytes=out_path.stat().st_size,
                                   encoding="wav", extra={"model": model})
        return SynthResult(provider=self.name, success=False, voice=chosen,
                           error=f"all models failed; last: {last_err}")
