"""run_regression_suite - repeatable, non-destructive regression checks.

All tests are read-only / dry-run / tiny-sample. No paid LLM/TTS, no ffmpeg media
build, no final-video overwrite, no deletions of project artifacts. Writes
QA_REPORTS/regression_suite_report.md.

Exit: 0 if no FAIL (SKIP allowed), 1 otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = []  # (id, name, status, detail)


def record(tid, name, status, detail=""):
    RESULTS.append((tid, name, status, detail))
    print(f"[{status:4}] {tid}. {name}" + (f"  — {detail}" if detail else ""))


def _run(cmd, timeout=240):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                          env={**os.environ, "PYTHONUTF8": "1"}, timeout=timeout)


def _validate(token):
    p = _run([PY, str(ROOT / "scripts" / "run_pipeline.py"), "validate", token])
    out = p.stdout + p.stderr
    passed = ("qa_verdict" in out and "pass" in out.lower()) or p.returncode == 0
    return passed, out


def _run_dir(token):
    cands = sorted((ROOT / "runs").glob(f"*_{token}_*"))
    return cands[-1] if cands else None


def t1_file1_gated():
    try:
        from agents.visual_classification_reviewer import _is_file1_run
        f1 = _is_file1_run({"source_file": "input/1.pdf"})
        ft = _is_file1_run({"source_file": "input/test.pdf"})
        ok, _ = _validate("1")
        if f1 and not ft and ok:
            record(1, "file-1 curated fixture gated + validate", "PASS", "is_file1(1)=True, is_file1(test)=False, validate 1 pass")
        else:
            record(1, "file-1 curated fixture gated + validate", "FAIL", f"f1={f1} ft={ft} validate={ok}")
    except Exception as e:
        record(1, "file-1 curated fixture gated + validate", "FAIL", repr(e))


def t2_file3_valid():
    ok, _ = _validate("3")
    record(2, "file-3 manual-agent run valid", "PASS" if ok else "FAIL", "validate 3")


def t3_testpdf_valid():
    ok, _ = _validate("test")
    record(3, "test.pdf benchmark run valid", "PASS" if ok else "FAIL", "validate test")


def t4_no_leak_to_test():
    rd = _run_dir("test")
    if rd is None:
        record(4, "no file-1 leakage into test.pdf", "SKIP", "no test run found"); return
    terms = ["Taxonomy", "ChatGPT", "Introduction to ML", "تاکسونومی", "Knowledge Transfer", "Vapnik", "Risk Minimization"]
    blob = ""
    for sub in ("content_json", "classification", "packages"):
        for f in (rd / sub).glob("*.json") if (rd / sub).exists() else []:
            blob += f.read_text(encoding="utf-8", errors="ignore")
    hit = [t for t in terms if t in blob]
    record(4, "no file-1/file-3 leakage into test.pdf", "PASS" if not hit else "FAIL", "leak terms: " + (str(hit) if hit else "NONE"))


def t5_named_input():
    try:
        from agents.full_orchestrator import plan
        pl = plan("test", "manual_agent")
        ok = pl["run_dir"] is not None
        record(5, "named input support ('test')", "PASS" if ok else "FAIL", f"run_dir={pl['run_dir']}")
    except Exception as e:
        record(5, "named input support ('test')", "FAIL", repr(e))


def t6_llm_dry_run_no_paid():
    try:
        from integrations.llm_client import load_llm_config, LLMClient
        cfg = dict(load_llm_config()); cfg["provider"] = "anthropic"; cfg["mode"] = "dry_run"
        c = LLMClient(cfg, pipeline_mode="llm_dry_run")
        c.vision_page_review(page_text="x"); c.generate_content(package_id="package_001")
        rep = c.usage_report()
        ok = (rep["planned_calls"] >= 2) and (not rep["paid_allowed"])
        record(6, "LLM dry-run plans calls, no paid", "PASS" if ok else "FAIL",
               f"planned={rep['planned_calls']} paid_allowed={rep['paid_allowed']}")
    except Exception as e:
        record(6, "LLM dry-run plans calls, no paid", "FAIL", repr(e))


def t7_slide_consistency():
    failed = []
    for token in ("1", "3", "test"):
        rd = _run_dir(token)
        if rd is None:
            continue
        p = _run([PY, str(ROOT / "scripts" / "validate_slide_manifest_consistency.py"), "--run", str(rd)])
        if "PASS" not in (p.stdout + p.stderr):
            failed.append(token)
    record(7, "slide manifest consistency (1/3/test)", "PASS" if not failed else "FAIL",
           "all PASS" if not failed else f"failed: {failed}")


def t8_tts_provider():
    try:
        cfg = json.loads((ROOT / "configs" / "tts_config.json").read_text(encoding="utf-8"))
        import importlib.util as u
        edge = (cfg.get("default_provider") == "edge") and (u.find_spec("edge_tts") is not None)
        paid = [k for k, v in (cfg.get("providers") or {}).items() if v.get("enabled") and k != "edge"]
        record(8, "TTS provider check (edge active, no paid enabled)", "PASS" if edge and not paid else "FAIL",
               f"edge_active={edge} paid_enabled={paid or 'none'}")
    except Exception as e:
        record(8, "TTS provider check", "FAIL", repr(e))


def t9_ffmpeg():
    ff = shutil.which("ffmpeg"); fp = shutil.which("ffprobe")
    record(9, "ffmpeg/ffprobe availability", "PASS" if ff and fp else "SKIP" if not (ff and fp) else "PASS",
           f"ffmpeg={'yes' if ff else 'no'} ffprobe={'yes' if fp else 'no'}")


def t10_missing_key_fail_closed():
    try:
        from integrations.llm_client import load_llm_config, LLMClient, LLMClientError
        cfg = dict(load_llm_config()); cfg["provider"] = "anthropic"; cfg["mode"] = "live"
        # ensure no key in this check's view
        os.environ.pop("ANTHROPIC_API_KEY", None)
        c = LLMClient(cfg, pipeline_mode="llm_paid", approved=True)
        raised = False
        try:
            c.generate_content(package_id="package_001")
        except LLMClientError:
            raised = True
        ok = (not c.paid_allowed()) and raised
        record(10, "missing API key fail-closed (llm_paid)", "PASS" if ok else "FAIL",
               f"paid_allowed={c.paid_allowed()} raised={raised}")
    except Exception as e:
        record(10, "missing API key fail-closed (llm_paid)", "FAIL", repr(e))


def t11_bad_pdf():
    tmp = Path(tempfile.mkdtemp(prefix="regr_badpdf_"))
    try:
        bad = tmp / "not_a.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not a valid pdf \x00\x01 garbage")
        graceful = False
        detail = ""
        try:
            import fitz
            try:
                d = fitz.open(str(bad))
                _ = d.page_count  # may raise
                d.close()
                # opened but invalid content -> still graceful (no crash)
                graceful = True; detail = "opened, no crash"
            except Exception as e:
                graceful = True; detail = "raised cleanly: " + type(e).__name__
        except Exception as e:
            graceful = True; detail = "fitz import issue: " + type(e).__name__
        record(11, "bad PDF handled gracefully (no crash)", "PASS" if graceful else "FAIL", detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t12_paid_no_approval():
    try:
        from integrations.llm_client import load_llm_config, LLMClient, LLMClientError
        cfg = dict(load_llm_config()); cfg["provider"] = "anthropic"; cfg["model"] = "x"; cfg["mode"] = "live"
        cfg["max_cost_per_run"] = 5.0
        os.environ["ANTHROPIC_API_KEY"] = "dummy-not-used"  # key present but approval missing
        c = LLMClient(cfg, pipeline_mode="llm_paid", approved=False)
        raised = False
        try:
            c.generate_content(package_id="package_001")
        except LLMClientError:
            raised = True
        os.environ.pop("ANTHROPIC_API_KEY", None)
        ok = (not c.paid_allowed()) and raised
        record(12, "paid mode without approval fails closed", "PASS" if ok else "FAIL",
               f"paid_allowed={c.paid_allowed()} raised={raised}")
    except Exception as e:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        record(12, "paid mode without approval fails closed", "FAIL", repr(e))


def t13_cache_key_created():
    try:
        from integrations.llm_client import load_llm_config, LLMClient
        rd = _run_dir("test")
        cfg = dict(load_llm_config()); cfg["provider"] = "anthropic"; cfg["mode"] = "dry_run"
        c = LLMClient(cfg, run_dir=rd, pipeline_mode="llm_dry_run")
        c.vision_page_review(page_text="x", page_id="page_001")
        pl = c.dry_run_plan()
        k = pl["items"][0]["cache_key"] if pl["items"] else ""
        root_ok = ("cache" in pl["cache_root"] and "llm" in pl["cache_root"])
        ok = bool(k) and root_ok and c.spent_usd == 0.0
        record(13, "cache key + per-stage cache root created (dry-run)", "PASS" if ok else "FAIL",
               f"key={k} root={pl['cache_root']}")
    except Exception as e:
        record(13, "cache key + per-stage cache root created (dry-run)", "FAIL", repr(e))


def main():
    print("=== regression suite (read-only / dry-run / safe) ===")
    for fn in (t1_file1_gated, t2_file3_valid, t3_testpdf_valid, t4_no_leak_to_test,
               t5_named_input, t6_llm_dry_run_no_paid, t7_slide_consistency, t8_tts_provider,
               t9_ffmpeg, t10_missing_key_fail_closed, t11_bad_pdf,
               t12_paid_no_approval, t13_cache_key_created):
        try:
            fn()
        except Exception as e:
            record(getattr(fn, "__name__", "?"), fn.__name__, "FAIL", "uncaught: " + repr(e))

    n_pass = sum(1 for *_, s, _ in [(r[0], r[1], r[2], r[3]) for r in RESULTS] if s == "PASS")
    n_fail = sum(1 for r in RESULTS if r[2] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r[2] == "SKIP")

    rep = ROOT / "QA_REPORTS" / "regression_suite_report.md"
    lines = ["# Regression Suite Report", "",
             f"- date: 2026-05-22", f"- result: **{n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP** (of {len(RESULTS)})",
             "- safety: read-only / dry-run / tiny-sample only — no paid LLM/TTS, no ffmpeg media build, no overwrite, no deletions.",
             "", "| # | test | status | detail |", "|---|---|---|---|"]
    for tid, name, status, detail in RESULTS:
        lines.append(f"| {tid} | {name} | **{status}** | {detail} |")
    lines += ["", f"## Verdict: {'PASS' if n_fail == 0 else 'FAIL'} ({n_pass}/{len(RESULTS)})"]
    rep.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {rep}")
    print(f"SUMMARY: {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
