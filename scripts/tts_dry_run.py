"""tts_dry_run - TTS provider-chain dry-run + optional Edge-fallback samples.

Probes every provider in the chain (offline, no paid synthesis), reports
availability + paid fail-closed status, and writes hq_tts_dry_run_report.md.
With --sample N, generates N sample clips from a run's `tts_text` using the first
AVAILABLE provider (Edge fallback when HQ is not approved/available) — free only.

NEVER calls a paid provider. Exit 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.tts_providers.registry import get_provider, available_provider_names, is_paid_provider  # noqa: E402


def _load_cfg():
    return json.loads((ROOT / "configs" / "tts_config.json").read_text(encoding="utf-8"))


def _provider_cfg(cfg, name):
    return (cfg.get("providers") or {}).get(name, {})


def _paid_allowed(cfg, name, approved):
    if not is_paid_provider(name):
        return True, "free"
    pcfg = _provider_cfg(cfg, name)
    paid = cfg.get("paid_tts", {})
    if pcfg.get("enabled") is not True:
        return False, "disabled"
    if paid.get("require_approval_before_paid", True) and not approved:
        return False, "no approval"
    if float(paid.get("max_cost_per_run", 0) or 0) <= 0:
        return False, "budget unset"
    return True, "gated-ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tts_dry_run")
    ap.add_argument("--chain", default=None, help="comma list to preview (default config provider_chain)")
    ap.add_argument("--approve-paid", action="store_true")
    ap.add_argument("--run", default=None, help="run dir (for --sample)")
    ap.add_argument("--sample", type=int, default=0, help="generate N sample clips from tts_text (free providers only)")
    args = ap.parse_args(argv)

    cfg = _load_cfg()
    chain = [c.strip() for c in args.chain.split(",")] if args.chain else list(cfg.get("provider_chain") or ["edge"])

    rows = []
    effective = None
    for name in chain:
        if name not in available_provider_names():
            rows.append({"provider": name, "status": "UNKNOWN (not registered)"}); continue
        prov = get_provider(name, _provider_cfg(cfg, name))
        pr = prov.probe(network=False)
        paid_ok, paid_reason = _paid_allowed(cfg, name, args.approve_paid)
        usable = pr.available and (paid_ok if is_paid_provider(name) else True)
        rows.append({"provider": name, "paid": is_paid_provider(name),
                     "importable": pr.importable, "available": pr.available,
                     "key_present": pr.extra.get("api_key_present", "n/a"),
                     "paid_gate": paid_reason, "usable": usable, "error": pr.error or ""})
        if usable and effective is None:
            effective = name

    # report
    out_dir = (Path(args.run) / "QA_REPORTS") if args.run else (ROOT / "QA_REPORTS")
    if args.run and not Path(args.run).is_absolute():
        out_dir = ROOT / args.run / "QA_REPORTS"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = out_dir / "hq_tts_dry_run_report.md"
    lines = ["# HQ TTS dry-run report", "",
             f"- chain: {chain}", f"- effective provider (first usable): **{effective or 'NONE'}**",
             "- no paid synthesis performed.", "",
             "| provider | paid | importable | available | key | paid_gate | usable | error |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.get('provider')} | {r.get('paid')} | {r.get('importable')} | {r.get('available')} | "
                     f"{r.get('key_present')} | {r.get('paid_gate')} | {r.get('usable')} | {r.get('error')} |")
    rep.write_text("\n".join(lines), encoding="utf-8")

    print(f"[tts_dry_run] chain={chain} effective={effective}")
    for r in rows:
        print(f"  {r.get('provider'):<12} usable={r.get('usable')} ({r.get('paid_gate')}) {r.get('error')}")
    print(f"[tts_dry_run] wrote {rep}")

    # optional free sample
    if args.sample and args.run:
        if effective is None or is_paid_provider(effective):
            print("[tts_dry_run] sample skipped: no FREE usable provider (paid is fail-closed).")
            return 0
        _make_samples(Path(args.run) if Path(args.run).is_absolute() else ROOT / args.run,
                      effective, cfg, args.sample)
    return 0


def _make_samples(run_dir, provider_name, cfg, n):
    nar = run_dir / "narration_scripts" / "full_narration.json"
    if not nar.is_file():
        print("[tts_dry_run] no full_narration.json; sample skipped."); return
    ents = [s for p in json.loads(nar.read_text(encoding="utf-8"))["packages"] for s in p["slides"]]
    out = run_dir / "QA_REPORTS" / "tts_provider_sample"
    out.mkdir(parents=True, exist_ok=True)
    prov = get_provider(provider_name, _provider_cfg(cfg, provider_name))
    made = []
    for i, e in enumerate(ents[:n], 1):
        text = e.get("tts_text") or e.get("narration_fa") or ""
        path = out / f"prov_{provider_name}_{i:02d}_{e.get('slide_id')}.mp3"
        res = prov.synthesize(text, path, voice=prov.resolve_voice())
        made.append((path.name, res.success, res.audio_bytes if res.success else res.error))
        print(f"  sample {path.name}: success={res.success}")
    print(f"[tts_dry_run] {sum(1 for _,s,_ in made if s)}/{len(made)} samples via {provider_name} -> {out}")


if __name__ == "__main__":
    sys.exit(main())
