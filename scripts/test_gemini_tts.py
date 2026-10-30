"""test_gemini_tts - verify google-genai Gemini TTS + generate a BOUNDED sample.

Never prints the API key (presence + redacted prefix only). With --sample N --run,
synthesizes N clips (default cap 5) from the run's gemini_tts_text.json into
QA_REPORTS/gemini_tts_real_sample/. Falls back to Edge only if Gemini is unusable.
Does NOT generate full audio / ffmpeg / video.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.env_loader import load_env_local, get_secret, redact  # noqa: E402
from integrations.tts_providers.registry import get_provider  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="test_gemini_tts")
    ap.add_argument("--run", default=None)
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args(argv)

    load_env_local(ROOT)
    cfg = json.loads((ROOT / "configs" / "tts_config.json").read_text(encoding="utf-8"))
    gcfg = (cfg.get("providers") or {}).get("gemini_tts", {})

    # key presence WITHOUT printing the value
    key = None
    for env_name in (gcfg.get("api_key_env") or ["GEMINI_API_KEY", "GOOGLE_API_KEY"]):
        v = get_secret(env_name, project_root=ROOT)
        if v:
            key = v
            print(f"[test_gemini_tts] key found via {env_name}: {redact(v)}")
            break
    if not key:
        print("[test_gemini_tts] no GEMINI_API_KEY/GOOGLE_API_KEY found (env or .env.local)")

    prov = get_provider("gemini_tts", gcfg)
    pr = prov.probe(network=False)
    print(f"[test_gemini_tts] gemini probe: importable={pr.importable} available={pr.available} "
          f"model={pr.extra.get('model')} key_present={pr.extra.get('api_key_present')} err={pr.error or '-'}")

    if not a.sample:
        return 0
    if not a.run:
        print("[test_gemini_tts] --sample needs --run"); return 2
    run_dir = Path(a.run) if Path(a.run).is_absolute() else (ROOT / a.run)
    g = json.loads((run_dir / "narration_scripts" / "gemini_tts_text.json").read_text(encoding="utf-8"))
    ents = {s["slide_id"]: s["tts_text"] for s in g["slides"]}
    pick = [s for s in ["p001_s001", "p002_s002", "p002_s003", "p003_s003"] if s in ents][:max(1, min(a.sample, 5))]
    out = run_dir / "QA_REPORTS" / "gemini_tts_real_sample"
    out.mkdir(parents=True, exist_ok=True)

    used = "gemini_tts"
    if not pr.available:
        print("[test_gemini_tts] Gemini unusable -> Edge fallback for sample")
        used = "edge"
        prov = get_provider("edge", (cfg.get("providers") or {}).get("edge", {}))

    meta = []
    for i, sid in enumerate(pick, 1):
        ext = "wav" if used == "gemini_tts" else "mp3"
        path = out / f"gsample_{i:02d}_{sid}_via_{used}.{ext}"
        res = prov.synthesize(ents[sid], path, voice=prov.resolve_voice())
        ok = getattr(res, "success", False)
        meta.append({"slide_id": sid, "file": (Path(res.out_path).name if (ok and res.out_path) else path.name),
                     "success": ok, "provider": used, "model": (res.extra or {}).get("model") if ok else None,
                     "error": None if ok else res.error})
        print(f"  {sid}: success={ok} provider={used} " + ("model=" + str((res.extra or {}).get('model')) if ok else "err=" + str(res.error)[:80]))
    json.dump({"effective_provider": used, "voice": prov.resolve_voice(), "samples": meta},
              open(out / "samples_index.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[test_gemini_tts] {sum(1 for m in meta if m['success'])}/{len(meta)} samples via {used} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
