---
name: codex-thread-export
description: Export local Codex sessions to Markdown with provenance, manifests, persistent destinations, batch selection, last-N turns, and Unicode-safe clipboard support.
---

Use `codex-export` when installed. Otherwise run `scripts/Export-CodexThread.py` with the same Python interpreter that contains optional `tiktoken`.

Core workflows:

- List active and archived sessions: `codex-export --list-sessions`
- Optional browser: `codex-export --browse --mode thread`
- Full chronological archive: `codex-export --session-id <UUID> --mode thread`
- Last N turns: add `--last-n-turns N`
- Batch: repeat `--session-id`/`--jsonl` or use `--sessions-file`
- Selected response: `--mode response --response N`
- Selected turn: `--mode turn --response N --no-ui-style`
- Reconstructed active context: add `--live-context` and always describe the result as reconstructed/approximate
- Explicit stored reasoning summaries: add `--reasoning-summaries`; never imply raw internal reasoning is exported
- Select/remember destination: `--choose-out-dir`; exact path: `--save-as`
- Public sharing: add `--redact` and inspect the generated manifest
- Schema regression: add `--report-events` or `--strict-events`

Never describe a source `…tokens truncated…` region as recovered. The exporter can only annotate the loss already present in the rollout record. Token counts are exact only for the explicitly reported encoding and exported text.
