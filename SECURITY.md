# Local configuration and runtime material

Keep API keys and account configuration in environment variables or ignored local secret files. Never commit input documents, authored private prompts, transcripts, run state, provider logs, or output media. Configure an external provider only when you intend to send its specific input to that service.

The default configuration uses manual authoring, disables every TTS provider, and has a zero paid budget. Paid LLM request bodies remain unimplemented and fail explicitly. The offline test suite blocks DNS, sockets, and child processes; it does not validate external-provider behavior.

Before publishing a change, inspect the exact staged file list and scan the proposed tree and new history for secrets. Pattern-based scanning does not establish that material is yours to distribute. Report a suspected credential exposure privately to the repository owner and rotate any affected credential.
