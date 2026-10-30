"""build_v2_video — full v2 deep-tutor audio + video (real Gemini Kore only).

Dedicated v2 pipeline so NO v1 artifact is touched:
  audio  -> runs/<id>/v2_audio/<slide_id>.wav  (+ v2_audio_manifest.json)
  video  -> runs/<id>/v2_video_segments/seg_NNN.mp4
  final  -> output/004_v2_deep_tutor_final_educational_video.mp4  (NEW name)

Hard rules: real Gemini TTS (voice Kore); NO Edge fallback — if Gemini fails,
STOP and report. Never overwrites the v1 video or protected videos. Never prints
the API key.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.env_loader import load_env_local
from integrations.tts_providers.registry import get_provider
from integrations.tts_loudness import normalize_to_lufs
from integrations.ffmpeg_concat import concat_clips

OUT_NAME = "004_v2_deep_tutor_final_educational_video.mp4"


def _wav_dur(p: Path):
    try:
        with wave.open(str(p)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def _load(run_dir):
    m = json.loads((run_dir / "v2_slide_manifest.json").read_text(encoding="utf-8"))
    g = json.loads((run_dir / "narration_scripts" / "v2_gemini_tts_text.json").read_text(encoding="utf-8"))
    tts = {s["slide_id"]: s["tts_text_fa"] for s in g["slides"]}
    return m["slides"], tts


def do_audio(run_dir: Path, force: bool = False) -> int:
    load_env_local(ROOT)
    cfg = json.loads((ROOT / "configs" / "tts_config.json").read_text(encoding="utf-8"))
    gcfg = cfg["providers"]["gemini_tts"]
    if cfg.get("sample_only") or float(cfg["paid_tts"]["max_cost_per_run"]) <= 0:
        print("[v2-audio] STOP: sample_only or budget==0 (gate)."); return 2
    slides, tts = _load(run_dir)
    prov = get_provider("gemini_tts", gcfg)
    pr = prov.probe(network=False)
    if not pr.available:
        print(f"[v2-audio] STOP: Gemini not available ({pr.error}). NO Edge fallback."); return 1
    voice = prov.resolve_voice()
    out = run_dir / "v2_audio"; out.mkdir(parents=True, exist_ok=True)
    man = []
    for i, s in enumerate(slides, 1):
        sid = s["slide_id"]; text = tts.get(sid, "")
        wav = out / f"{sid}.wav"
        if wav.exists() and not force and _wav_dur(wav) > 1.0:
            man.append({"slide_id": sid, "file": wav.name, "duration": round(_wav_dur(wav), 3), "provider": "gemini_tts", "reused": True})
            print(f"  [{i:3d}/{len(slides)}] {sid} reused"); continue
        res = prov.synthesize(text, wav, voice=voice)
        if not getattr(res, "success", False):
            print(f"[v2-audio] STOP at {sid}: Gemini failed: {res.error}. NO Edge fallback."); return 1
        # normalize -16 LUFS in place
        tmp = wav.with_suffix(".norm.wav")
        r = normalize_to_lufs(str(wav), str(tmp), target_lufs=-16.0, tp=-1.5, lra=11.0, execute=True)
        if r.get("ran") and tmp.exists() and tmp.stat().st_size > 1000:
            shutil.move(str(tmp), str(wav))
        dur = _wav_dur(wav)
        if dur <= 1.0:
            print(f"[v2-audio] STOP at {sid}: duration {dur:.2f}s <= 1s."); return 1
        man.append({"slide_id": sid, "file": wav.name, "duration": round(dur, 3),
                    "provider": "gemini_tts", "voice": voice, "model": (res.extra or {}).get("model"),
                    "loudness_normalized": True, "reused": False})
        print(f"  [{i:3d}/{len(slides)}] {sid} ok {dur:.1f}s")
    total = round(sum(x["duration"] for x in man), 2)
    (run_dir / "v2_audio_manifest.json").write_text(json.dumps(
        {"run_id": run_dir.name, "provider": "gemini_tts", "voice": voice,
         "target_lufs": -16.0, "clip_count": len(man), "total_duration": total, "clips": man},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[v2-audio] DONE {len(man)} clips, total {total}s -> v2_audio/ + v2_audio_manifest.json")
    return 0


def do_video(run_dir: Path) -> int:
    ff = shutil.which("ffmpeg")
    if not ff:
        print("[v2-video] STOP: ffmpeg not found."); return 1
    slides, _ = _load(run_dir)
    adir = run_dir / "v2_audio"
    seg_dir = run_dir / "v2_video_segments"; seg_dir.mkdir(parents=True, exist_ok=True)
    vf = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
          "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,fps=30")
    segs = []
    for i, s in enumerate(slides, 1):
        img = run_dir / s["image"]; wav = adir / f"{s['slide_id']}.wav"
        if not img.is_file() or not wav.is_file():
            print(f"[v2-video] STOP: missing {img if not img.is_file() else wav}"); return 1
        seg = seg_dir / f"seg_{i:03d}.mp4"
        cmd = [ff, "-y", "-loop", "1", "-i", str(img), "-i", str(wav),
               "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
               "-vf", vf, "-c:a", "aac", "-b:a", "192k", "-shortest", str(seg)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not (seg.exists() and seg.stat().st_size > 0):
            print(f"[v2-video] STOP: segment {i} failed:\n{p.stderr[-800:]}"); return 1
        segs.append(seg)
        if i % 10 == 0 or i == len(slides):
            print(f"  segments {i}/{len(slides)}")
    out_dir = ROOT / "output"; out_dir.mkdir(exist_ok=True)
    out_path = out_dir / OUT_NAME
    protected = {"004_final_educational_video.mp4", "004_bilingual_final_educational_video.mp4",
                 "001_final_educational_video.mp4", "003_final_educational_video.mp4"}
    if out_path.name in protected:
        print("[v2-video] STOP: refusing to overwrite a protected/v1 video."); return 1
    r = concat_clips(segs, out_path, ffmpeg_binary=ff, timeout_seconds=900)
    if not getattr(r, "success", False):
        print(f"[v2-video] STOP: concat failed: {getattr(r,'error',None)}"); return 1
    print(f"[v2-video] DONE -> {out_path} size={out_path.stat().st_size} dur={getattr(r,'duration_seconds',None)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_v2_video")
    ap.add_argument("--run", required=True)
    ap.add_argument("--audio", action="store_true")
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    run_dir = (Path(a.run) if Path(a.run).is_absolute() else (ROOT / a.run)).resolve()
    if not run_dir.is_dir():
        print(f"[ERROR] run not found: {run_dir}"); return 2
    if a.audio:
        rc = do_audio(run_dir, force=a.force)
        if rc or not a.video:
            return rc
    if a.video:
        return do_video(run_dir)
    if not (a.audio or a.video):
        print("[build_v2_video] specify --audio and/or --video.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
