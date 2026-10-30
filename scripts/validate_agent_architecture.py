#!/usr/bin/env python3
"""validate_agent_architecture.py — static checker for the strict source-exhaustive
Claude-company agent architecture (PDF-first lessonbook).

Read-only. Renders nothing, authors no content, calls no paid/TTS/video. It verifies
that the `.claude/agents/` role layer, the `/convert-lecture-pdf` SKILL, and the core
docs encode the strict, future-PDF-generic company workflow whose **active default
product is a standalone source-exhaustive Persian lessonbook PDF**.

Checks
  1. every required ACTIVE agent file exists
  2. every ACTIVE agent has all 16 required sections (exact H2 headings)
  3. every ACTIVE agent states the global invariants + a provenance token
  4. no run-specific tokens leak into the generic ACTIVE agents
  4b. every ACTIVE agent carries `## Perfect Source-Exhaustive Lessonbook Binding`
      with its key invariant phrases
  5. SKILL.md references the mandatory company gates; promotion needs explicit approval;
     it references the v2 artifacts; and it does NOT put any legacy video/audio agent on
     the default PDF path
  6. the core docs exist; docs referenced by SKILL.md resolve (hard) and by CLAUDE.md (warn)
  7. the load-bearing roles exist
  8. AGENT_INDEX.md separates Active / Legacy-Optional-Video / Superseded / Support (A/B/C/D)
     and lists every active + legacy agent
  9. LEGACY agents are clearly marked `## Legacy / Optional Status` (WARN, not FAIL, if a
     legacy agent is unmarked or lacks the full 16 sections — they are off-by-default)

Exit code: 0 = PASS, 1 = FAIL.
Usage: python scripts/validate_agent_architecture.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".claude" / "agents"
SKILL = ROOT / ".claude" / "skills" / "convert-lecture-pdf" / "SKILL.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
DOCS_DIR = ROOT / "docs"
AGENT_INDEX = AGENTS_DIR / "AGENT_INDEX.md"

# --- 1. required ACTIVE agent set (the strict source-exhaustive lessonbook company) ---
REQUIRED_AGENTS = [
    "queen-orchestrator",
    "state-memory-manager",
    "safety-budget-gate",
    "input-extraction-lead",
    "source-page-inventory-lead",
    "page-understanding-worker",
    "visual-understanding-worker",
    "ocr-text-verification-worker",
    "criticality-assigner",
    "package-planning-lead",
    "visual-asset-planning-lead",
    "package-teaching-lead",
    "text-bullet-interpreter-worker",
    "formula-math-tutor-worker",
    "graph-figure-explainer-worker",
    "table-explainer-worker",
    "proof-derivation-worker",
    "example-exercise-worker",
    "bilingual-terms-worker",
    "narrative-tutor-writer",
    "coverage-auditor-worker",
    "coherence-editor",
    "full-lessonbook-assembler",
    "final-source-exhaustive-qa-lead",
    "renderer-lead",
    "rtl-typography-qa-worker",
    "user-review-gate",
    "promotion-protected-output-registrar",
]

# --- 2. required sections (exact H2 headings, in any order) -------------------
REQUIRED_SECTIONS = [
    "Mission",
    "Scope",
    "Inputs",
    "Outputs",
    "Required schemas/artifacts",
    "Allowed actions",
    "Forbidden actions",
    "Dependency gates",
    "Handoff format",
    "Self-audit checklist",
    "Failure modes",
    "Escalation rules",
    "Stop conditions",
    "Future-PDF genericity rules",
    "Role boundaries",
    "Provenance requirements",
]

# --- 3. global invariants every ACTIVE agent must state (lowercased) ----------
GLOBAL_INVARIANT_PHRASES = {
    "paid/TTS/video OFF by default": "off by default",
    "approval-gated promotion + deletion": "explicit user approval",
    "preserve canonical outputs": "canonical",
    "no run-specific assumptions": "run-specific",
    "Python = no educational reasoning": "no educational reasoning",
}
PROVENANCE_TOKENS = ["claude_code_manual_agent", "python_executor"]

# --- 4. run-specific tokens that must NOT appear in the generic agents --------
RUN_SPECIFIC_PATTERNS = [
    r"\btest\.pdf\b",
    r"\b4\.pdf\b",
    r"\bfile-[13]\b",
    r"\bpackage_002\b",
    r"\btest_original\b",
    r"isabel\s+valera",
    r"performance\s+metrics\s+for\s+classification",
]

# --- 4b. Perfect Source-Exhaustive Lessonbook Binding (required in ACTIVE agents) ---
BINDING_HEADING = "Perfect Source-Exhaustive Lessonbook Binding"
BINDING_PHRASES = [
    "standalone Persian lessonbook PDF",
    "No shallow-summary default",
    "Page count is never a constraint",
    "source_element_coverage_matrix_v2",
    "Crops, annotated crops",
    "No promotion without explicit user approval",
]

# --- 5. mandatory gate references in SKILL.md (lowercased substrings) ---------
SKILL_REQUIRED_SUBSTRINGS = [
    "company",
    "source page inventory",
    "inventory",
    "criticality",
    "package planning",
    "visual-asset",
    "coverage audit",
    "coherence",
    "assembl",
    "final source-exhaustive qa",
    "render",
    "typography",
    "user review",
    "promotion",
    "off by default",
]
# v2 artifacts the active workflow must reference (lowercased)
SKILL_V2_ARTIFACTS = [
    "source_page_inventory",
    "source_element_coverage_matrix_v2",
    "source_exhaustive_depth_review",
]

# --- 6. core docs that must exist ---------------------------------------------
REQUIRED_DOCS = [
    "COMPANY_AUTOMATION_STRICT_COVERAGE_ARCHITECTURE.md",
    "AGENT_ROLE_BOUNDARIES.md",
    "AGENT_HANDOFF_PROTOCOL.md",
    "AUTOMATIC_SOURCE_COVERAGE_AUDIT_LOOP.md",
    "SOURCE_ELEMENT_CRITICALITY_POLICY.md",
    "FUTURE_PDF_LESSONBOOK_WORKFLOW_CONTRACT.md",
]

# --- 7. load-bearing roles called out explicitly -----------------------------
SPECIFIC_ROLES = {
    "source page inventory role": "source-page-inventory-lead",
    "criticality role": "criticality-assigner",
    "coverage auditor role": "coverage-auditor-worker",
    "final source-exhaustive QA role": "final-source-exhaustive-qa-lead",
    "state/memory role": "state-memory-manager",
    "safety/budget role": "safety-budget-gate",
    "user-review gate role": "user-review-gate",
    "promotion registrar role": "promotion-protected-output-registrar",
}

# --- 8/9. LEGACY agents (kept, off-by-default) --------------------------------
# superseded legacy agents -> their active replacement (for AGENT_INDEX §C)
LEGACY_SUPERSEDED = {
    "file-intake-extraction-queen": "input-extraction-lead",
    "page-review-worker": "page-understanding-worker",
    "page-review-qa": "source-page-inventory-lead + coverage-auditor-worker",
    "page-understanding-queen": "source-page-inventory-lead + page-understanding-worker",
    "package-architecture-queen": "package-planning-lead",
    "package-grouping-worker": "package-planning-lead",
    "package-metadata-worker": "package-planning-lead",
    "package-content-company-queen": "package-teaching-lead",
    "package-content-worker": "package-teaching-lead + narrative-tutor-writer",
    "figure-diagram-explainer-worker": "graph-figure-explainer-worker",
    "glossary-example-exam-worker": "bilingual-terms-worker + example-exercise-worker",
    "final-qa-board-queen": "final-source-exhaustive-qa-lead",
    "final-qa-worker": "final-source-exhaustive-qa-lead + validators",
    "project-librarian": "state-memory-manager",
    "regression-safety-queen": "final-source-exhaustive-qa-lead + safety-budget-gate",
}
# optional video/audio/slide/narration legacy agents (AGENT_INDEX §B) — must NEVER
# appear as a step on the default PDF lessonbook path in SKILL.md
VIDEO_AUDIO_LEGACY = [
    "slide-production-queen",
    "slide-spec-worker",
    "narration-company-queen",
    "narration-worker",
    "tts-text-worker",
    "gemini-tts-audio-queen",
    "gemini-audio-worker",
    "video-assembly-queen",
    "video-segment-worker",
]
# support / optional-runtime legacy agents (AGENT_INDEX §D)
SUPPORT_LEGACY = ["task-graph-manager"]

LEGACY_AGENTS = list(LEGACY_SUPERSEDED.keys()) + VIDEO_AUDIO_LEGACY + SUPPORT_LEGACY
LEGACY_MARKER = "## Legacy / Optional Status"

# AGENT_INDEX must carry the A/B/C/D separation headings
INDEX_REQUIRED_SUBSTRINGS = [
    "## A. Active PDF-First Lessonbook Agents",
    "## B. Legacy / Optional",
    "## C. Superseded Agents",
    "## D. Support",
]

DOC_REF_RE = re.compile(r"docs/([A-Za-z0-9_./-]+\.md)")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.checks = 0

    def ok(self, msg: str) -> None:
        self.checks += 1
        print(f"  [PASS] {msg}")

    def fail(self, msg: str) -> None:
        self.checks += 1
        self.failures.append(msg)
        print(f"  [FAIL] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  [WARN] {msg}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def has_section(text: str, section: str) -> bool:
    return re.search(r"^##\s+" + re.escape(section) + r"\s*$", text, re.M) is not None


def check_agents(rep: Report) -> None:
    print("\n[1-4] ACTIVE agents: existence, 16 sections, invariants, provenance, no run-specific leakage")
    for slug in REQUIRED_AGENTS:
        path = AGENTS_DIR / f"{slug}.md"
        if not path.exists():
            rep.fail(f"missing agent file: .claude/agents/{slug}.md")
            continue
        text = read(path)
        low = text.lower()

        missing = [s for s in REQUIRED_SECTIONS if not has_section(text, s)]
        if missing:
            rep.fail(f"{slug}.md missing sections: {', '.join(missing)}")
        else:
            rep.ok(f"{slug}.md has all 16 sections")

        missing_inv = [label for label, sub in GLOBAL_INVARIANT_PHRASES.items() if sub not in low]
        if missing_inv:
            rep.fail(f"{slug}.md missing invariant statement(s): {', '.join(missing_inv)}")
        else:
            rep.ok(f"{slug}.md states all global invariants")

        if not any(tok in low for tok in PROVENANCE_TOKENS):
            rep.fail(f"{slug}.md missing a provenance token ({' / '.join(PROVENANCE_TOKENS)})")
        else:
            rep.ok(f"{slug}.md declares provenance")

        hits = []
        for pat in RUN_SPECIFIC_PATTERNS:
            for m in re.finditer(pat, text, re.I):
                hits.append(m.group(0))
        if hits:
            rep.fail(f"{slug}.md contains run-specific token(s): {sorted(set(hits))}")
        else:
            rep.ok(f"{slug}.md is free of run-specific tokens")


def check_binding(rep: Report) -> None:
    print("\n[4b] ACTIVE agents carry the Perfect Source-Exhaustive Lessonbook Binding")
    for slug in REQUIRED_AGENTS:
        path = AGENTS_DIR / f"{slug}.md"
        if not path.exists():
            continue  # already failed in [1-4]
        text = read(path)
        if not has_section(text, BINDING_HEADING):
            rep.fail(f"{slug}.md missing section: ## {BINDING_HEADING}")
            continue
        missing = [p for p in BINDING_PHRASES if p not in text]
        if missing:
            rep.fail(f"{slug}.md binding missing phrase(s): {missing}")
        else:
            rep.ok(f"{slug}.md has the Perfect Source-Exhaustive Lessonbook Binding")


def check_specific_roles(rep: Report) -> None:
    print("\n[7] Load-bearing roles present")
    for label, slug in SPECIFIC_ROLES.items():
        if (AGENTS_DIR / f"{slug}.md").exists():
            rep.ok(f"{label}: {slug}.md present")
        else:
            rep.fail(f"{label}: expected {slug}.md is missing")


def check_skill(rep: Report) -> None:
    print("\n[5] SKILL.md references the mandatory gates, v2 artifacts; no legacy video/audio on default path")
    if not SKILL.exists():
        rep.fail("missing .claude/skills/convert-lecture-pdf/SKILL.md")
        return
    raw = read(SKILL)
    low = raw.lower()
    missing = [s for s in SKILL_REQUIRED_SUBSTRINGS if s not in low]
    if missing:
        rep.fail(f"SKILL.md missing gate references: {', '.join(missing)}")
    else:
        rep.ok(f"SKILL.md references all {len(SKILL_REQUIRED_SUBSTRINGS)} mandatory gates/rules")

    if "explicit" in low and "approval" in low:
        rep.ok("SKILL.md states promotion needs explicit approval")
    else:
        rep.fail("SKILL.md does not state that promotion needs explicit approval")

    missing_v2 = [a for a in SKILL_V2_ARTIFACTS if a not in low]
    if missing_v2:
        rep.fail(f"SKILL.md does not reference v2 artifact(s): {', '.join(missing_v2)}")
    else:
        rep.ok("SKILL.md references the v2 inventory/coverage/depth artifacts")

    leaked = [slug for slug in VIDEO_AUDIO_LEGACY if slug in low]
    if leaked:
        rep.fail(f"SKILL.md puts legacy video/audio agent(s) on the default PDF path: {leaked}")
    else:
        rep.ok("SKILL.md keeps legacy video/audio agents off the default PDF path")


def check_docs(rep: Report) -> None:
    print("\n[6] Core docs exist; referenced docs resolve")
    for name in REQUIRED_DOCS:
        if (DOCS_DIR / name).exists():
            rep.ok(f"docs/{name} exists")
        else:
            rep.fail(f"missing core doc: docs/{name}")

    if SKILL.exists():
        for ref in sorted(set(DOC_REF_RE.findall(read(SKILL)))):
            if (DOCS_DIR / ref).exists():
                rep.ok(f"SKILL.md -> docs/{ref} resolves")
            else:
                rep.fail(f"SKILL.md references a missing doc: docs/{ref}")

    if CLAUDE_MD.exists():
        missing = sorted({r for r in DOC_REF_RE.findall(read(CLAUDE_MD)) if not (DOCS_DIR / r).exists()})
        for ref in missing:
            rep.warn(f"CLAUDE.md references a missing doc (non-blocking): docs/{ref}")
        if not missing:
            rep.ok("CLAUDE.md doc references all resolve")


def check_index(rep: Report) -> None:
    print("\n[8] AGENT_INDEX.md separates Active / Legacy / Superseded / Support and lists every agent")
    if not AGENT_INDEX.exists():
        rep.fail("missing .claude/agents/AGENT_INDEX.md")
        return
    text = read(AGENT_INDEX)
    missing_secs = [s for s in INDEX_REQUIRED_SUBSTRINGS if s not in text]
    if missing_secs:
        rep.fail(f"AGENT_INDEX.md missing separation section(s): {missing_secs}")
    else:
        rep.ok("AGENT_INDEX.md has the A/B/C/D separation (active/legacy/superseded/support)")

    missing_active = [s for s in REQUIRED_AGENTS if s not in text]
    if missing_active:
        rep.fail(f"AGENT_INDEX.md does not list active agent(s): {missing_active}")
    else:
        rep.ok(f"AGENT_INDEX.md lists all {len(REQUIRED_AGENTS)} active agents")

    missing_legacy = [s for s in LEGACY_AGENTS if s not in text]
    if missing_legacy:
        rep.fail(f"AGENT_INDEX.md does not list legacy agent(s): {missing_legacy}")
    else:
        rep.ok(f"AGENT_INDEX.md lists all {len(LEGACY_AGENTS)} legacy agents")


def check_legacy_markers(rep: Report) -> None:
    print("\n[9] LEGACY agents clearly marked off-by-default (WARN, not FAIL)")
    for slug in LEGACY_AGENTS:
        path = AGENTS_DIR / f"{slug}.md"
        if not path.exists():
            rep.warn(f"legacy agent file absent (kept-for-history expectation): {slug}.md")
            continue
        text = read(path)
        if LEGACY_MARKER not in text:
            rep.warn(f"legacy agent not yet marked '{LEGACY_MARKER}': {slug}.md")
        else:
            rep.ok(f"{slug}.md is marked legacy/off-by-default")
        # off-by-default invariant phrasing (informational warn only)
        if "off-by-default" not in text.lower() and "off by default" not in text.lower():
            rep.warn(f"legacy agent {slug}.md does not say off-by-default explicitly")


def main() -> int:
    print("validate_agent_architecture.py — strict source-exhaustive lessonbook company architecture check")
    print(f"root: {ROOT}")
    rep = Report()
    check_agents(rep)
    check_binding(rep)
    check_specific_roles(rep)
    check_skill(rep)
    check_docs(rep)
    check_index(rep)
    check_legacy_markers(rep)

    print("\n" + "=" * 72)
    verdict = "PASS" if not rep.failures else "FAIL"
    print(f"VERDICT: {verdict}  ({rep.checks} checks, {len(rep.failures)} failures, {len(rep.warnings)} warnings)")
    if rep.failures:
        print("\nFailures:")
        for f in rep.failures:
            print(f"  - {f}")
    if rep.warnings:
        print(f"\nWarnings ({len(rep.warnings)}):")
        for w in rep.warnings:
            print(f"  - {w}")
    return 0 if not rep.failures else 1


if __name__ == "__main__":
    sys.exit(main())
