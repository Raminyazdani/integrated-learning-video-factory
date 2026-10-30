# Source-only import

The code comes from the personal `integrated_learning_video_factory` project, local master HEAD `4f97c9137b62bb6ea6154738ac15ae52a2c437a3`, whose recorded author matches the repository owner. No upstream remote was configured. This release starts a new, code-only history; it does not import the original Git history.

Included: pipeline Python modules, pipeline scripts, JSON artifact schemas, and textual LaTeX/Jinja templates. Clean example configuration and an offline synthetic test suite were added. Original configuration, environment files, private prompts, instructions for local agents, documents, archives, generated artifacts, runtime diagnostics, and binary presentation templates were excluded. Two account-usage inspection helpers were omitted because they are outside the document pipeline.

The original curated lesson content, fixed visual reviews, segmentation hints, fixed source-name gate, and package metadata tables were emptied. The existing generic algorithms and run-specific authored input paths remain. This prevents the publication from embedding a prior document's authored output.

Environment detection now requires explicit `probe_network=True` before a DNS probe. The paid LLM adapter is unfinished in the original implementation and remains explicitly unfinished. This release does not supply a replacement implementation or fabricate missing authored inputs.

The synthetic extraction test exposed a Windows newline mismatch between the PDF extractor's recorded text hash and its written text bytes. Text output now uses explicit LF newlines so the manifest hash matches the actual file on Windows as well as other platforms.

The source has references to optional external renderers and local runtime tools. Those prerequisites and actual end-to-end media creation have not been validated by the offline checks. Dependencies retain their own licenses; no third-party input media or fonts are included.
