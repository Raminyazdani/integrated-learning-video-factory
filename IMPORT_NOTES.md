# Source-only import

The code comes from the personal `integrated_learning_video_factory` project, local master HEAD `4f97c9137b62bb6ea6154738ac15ae52a2c437a3`, whose recorded author matches the repository owner. No upstream remote was configured. This release starts a new, code-only history; it does not import the original Git history.

Included: pipeline Python modules, pipeline scripts, JSON artifact schemas, and textual LaTeX/Jinja templates. Clean example configuration and an offline synthetic test suite were added. Original configuration, environment files, private prompts, instructions for local agents, documents, archives, generated artifacts, runtime diagnostics, and binary presentation templates were excluded. Two account-usage inspection helpers were omitted because they are outside the document pipeline.

Prior lesson content, visual reviews, segmentation hints and curated metadata are excluded. Package metadata now follows the same run-specific path for every input filename. The content renderer validates the run-specific JSON against the authored-content schema, accepts the declared author tags `CLAUDE_CODE_MANUAL_AGENT` or `CLAUDE_API`, and checks the source pages against the manifest. A missing content file, disallowed tag, or unavailable schema validation stops rendering. The contract status command reports the same checks for each artifact. The tag is supplied by the artifact author and is a declaration of provenance.

Environment detection now requires explicit `probe_network=True` before a DNS probe. The paid LLM adapter is unfinished in the original implementation and remains explicitly unfinished. This release does not supply a replacement implementation or fabricate missing authored inputs.

The synthetic extraction test exposed a Windows newline mismatch between the PDF extractor's recorded text hash and its written text bytes. Text output now uses explicit LF newlines so the manifest hash matches the actual file on Windows as well as other platforms.

The source has references to optional external renderers and local runtime tools. Those prerequisites and actual end-to-end media creation have not been validated by the offline checks. Dependencies retain their own licenses; no third-party input media or fonts are included.
