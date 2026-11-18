# Integrated Learning Video Factory

Personal Python source for a Persian educational-document workflow: extract PDF/PPTX pages, classify and segment them, accept run-specific authored reviews and lesson content, and build lessonbook, slide, narration, audio, and video artifacts.

The repository includes the implementation, schemas, text templates and synthetic examples. Runtime documents and generated media stay in local run directories.

## Install and inspect

Use Python 3.12. Create the virtual environment next to the checkout so repository-wide source checks scan only project code. In PowerShell:

```powershell
python -m venv ../video-factory-venv
../video-factory-venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
```

In a POSIX shell:

```sh
python3.12 -m venv ../video-factory-venv
. ../video-factory-venv/bin/activate
python -m pip install -r requirements.txt
```

From the repository root:

```sh
python scripts/run_pipeline.py --help
python -m unittest discover -s tests -v
python examples/offline_extraction.py
```

The example draws a one-page lesson on counting tiles, runs document intake and PDF extraction, and prints the extracted text:

```text
Counting a rectangle
Three rows of four tiles cover twelve squares.
```

The command prints the run directory. Under `extracted/` it writes the page rendering to `pages/page_001.png`, the text to `page_text/page_001.txt`, and their hashes to `extraction_manifest.json`. The run also contains a QA report. By default each invocation uses its own temporary workspace. Pass `--output PATH` to keep the results in a new directory of your choice. This example runs locally with the core dependencies.

The pinned core requirements cover document handling, schema validation, and rendering helpers. `requirements-optional.txt` contains separately audited provider/browser integrations. Install those only when that integration is needed. FFmpeg, XeLaTeX, office conversion tools, Persian fonts, and any Playwright browser are separate local prerequisites.

## Run with your own permitted inputs

Place a document you have permission to process in `input/`, such as `input/fictional.pdf`. Start with a plan:

```powershell
python scripts/run_pipeline.py convert fictional --mode manual_agent --dry-run
```

The pipeline uses `runs/` for runtime artifacts. Understanding stages need run-specific authored reviews and content; the JSON schemas define the artifact formats. The package-content renderer requires a schema-valid document in `content_authored/<package_id>.json`. It checks the declared `provenance.authored_by` and verifies that its source pages match the package manifest, regardless of the input filename. Private authoring prompts are not included. No completed-video or universal document-conversion claim is made for this source release.

`manual_agent` is the shipped default. `llm_dry_run` records plans without making provider calls. The paid LLM adapter still raises an explicit implementation error even when configured; it is preserved as an unfinished adapter, not presented as a working cloud integration. Paid mode also requires explicit approval, a key, and a budget. The mechanical fallback marks its output as low quality.

All TTS providers are disabled in the example configuration. Some optional TTS providers send narration to an external service when explicitly enabled and invoked. Supply credentials through your own environment or ignored local secret files. Ordinary environment detection and CLI inspection do not probe DNS by default.

## Validation scope

The offline tests compile all Python sources, resolve internal module paths, validate artifact schemas, exercise atomic writes and cache behavior, check manual/dry-run/paid guards, verify generic review behavior, inspect CLI help, and extract text and an image from a tiny synthetic PDF. The extraction test verifies that recorded text/image hashes match the actual output bytes, including Windows newline behavior. The segmentation test checks every three-signal sequence up to six fictional pages (1,092 cases), including coverage, order, continuity, and package-size limits. Tests fence network connections, DNS, and subprocess launches.

No real provider, browser, microphone, email account, original document, FFmpeg/LaTeX process, full media pipeline, or historical run was exercised. Original runtime-specific regression scripts remain source tools; they are not the offline test suite above.

## Rights

Copyright 2026 Ramin Yazdani. All rights reserved for original project code; see `LICENSE`. Dependencies, fonts, media, documents, and other third-party material retain their own rights and license terms. No permission to publish or redistribute an input or output follows from this repository.
