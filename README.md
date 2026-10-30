# Integrated Learning Video Factory

Personal Python source for a Persian educational-document workflow: extract PDF/PPTX pages, classify and segment them, accept run-specific authored reviews and lesson content, and build lessonbook, slide, narration, audio, and video artifacts.

This private source release preserves the implementation, schemas, and text templates. It contains no original teaching documents, transcripts, ebooks, recordings, generated lessons, account records, private prompts, credentials, or historical Git objects. Embedded sample lesson/review tables have been emptied; a document named `1.pdf` receives no special authored content.

## Install and inspect

Python 3.12 is the validated interpreter.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/run_pipeline.py --help
python -m unittest discover -s tests -v
```

The pinned core requirements cover document handling, schema validation, and rendering helpers. `requirements-optional.txt` contains separately audited provider/browser integrations. Install those only when that integration is needed; installing Playwright does not install a browser. FFmpeg, XeLaTeX, office conversion tools, Persian fonts, and any Playwright browser are separate local prerequisites and are not bundled.

## Run with your own permitted inputs

Place a document you have permission to process in `input/`, such as `input/fictional.pdf`. Start with a plan:

```powershell
python scripts/run_pipeline.py convert fictional --mode manual_agent --dry-run
```

The pipeline uses `runs/` for runtime artifacts. Understanding stages need run-specific authored reviews and content; the JSON schemas define the artifact formats. Missing authored content stops those stages. Private authoring prompts are not included. No completed-video or universal document-conversion claim is made for this source release.

`manual_agent` is the shipped default. `llm_dry_run` records plans without making provider calls. The paid LLM adapter still raises an explicit implementation error even when configured; it is preserved as an unfinished adapter, not presented as a working cloud integration. Paid mode also requires explicit approval, a key, and a budget. The mechanical fallback marks its output as low quality.

All TTS providers are disabled in the example configuration. Some optional TTS providers send narration to an external service when explicitly enabled and invoked. Supply credentials through your own environment or ignored local secret files. Ordinary environment detection and CLI inspection do not probe DNS by default.

## Validation scope

The offline tests compile all Python sources, resolve internal module paths, validate artifact schemas, exercise atomic writes and cache behavior, check manual/dry-run/paid guards, verify generic review behavior, inspect CLI help, and extract text and an image from a tiny synthetic PDF. The extraction test verifies that recorded text/image hashes match the actual output bytes, including Windows newline behavior. The segmentation test checks every three-signal sequence up to six fictional pages (1,092 cases), including coverage, order, continuity, and package-size limits. Tests fence network connections, DNS, and subprocess launches.

No real provider, browser, microphone, email account, original document, FFmpeg/LaTeX process, full media pipeline, or historical run was exercised. Original runtime-specific regression scripts remain source tools; they are not the offline test suite above.

## Rights

Copyright 2026 Ramin Yazdani. All rights reserved for original project code; see `LICENSE`. Dependencies, fonts, media, documents, and other third-party material retain their own rights and license terms. No permission to publish or redistribute an input or output follows from this repository.
