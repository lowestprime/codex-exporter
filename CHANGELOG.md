# Changelog

## 0.3.2 (proposed; audited Codex desktop event coverage)

- Recognize capitalized `event_msg/item_completed` UserMessage, AgentMessage, FileChange, Extension, ImageView, and McpToolCall items; preserve the complete recorded text, file-change content, tool results, and source-line provenance in streaming output rather than silently relegating them to an unknown-event sidecar.
- Distinguish known telemetry (`token_usage_record`), empty reasoning-status events, and context-compaction boundaries from truly unknown schemas. The raw unknown-event sidecar now only contains genuinely unrecognized record types.
- Support opt-in explicit `summary_text` from Reasoning events, excluding `raw_content` even when summary inclusion is requested.
- Add regression tests for actual 2026 source shapes and an audit showing 24,007 previously unknown events in the provided user archive, including 1,145 previously omitted FileChange records.
- The original source JSONL is still required for total fidelity, and official source-size/manifest checks do not prove that an export includes all original source bytes unless the input JSONL is independently available.

## 0.3.1 (proposed; Windows filename/listing hotfix)

- Prevent Windows `WinError 123` in streaming project/single-session exports when session titles contain pasted-text attachment paths or other Windows-reserved filename characters. Sanitize both default and user-template filename substitutions, retain compact unique session IDs, and prohibit explicit subfolder templates.
- Make `--list-sessions`/`cdx-sessions` output compact, valid single-line Markdown table entries regardless of title length. Use `--full-session-titles` for complete single-line display; original SQLite source metadata is unmodified.
- Cache streaming session-catalog lookup once per batch instead of rescanning for every session.
- Regression-test exact Windows-style attachment-path titles, custom filenames, collisions, project batches, and very long multiline catalog titles.

## 0.3.0 (proposed)

- Reconcile SQLite threads against active and archived rollouts; normalize Windows extended paths.
- Export all available local rollouts for a named project in one command.
- Add efficient streaming full-thread export with progress, provenance, and schema audit.
- Convert user-supplied official ChatGPT account export JSON/ZIP, including branches.
- Bundle new components in the Windows standalone and Codex skill distributions.

## 0.2.0

- Persist and natively select export directories and Save As filenames.
- Add mode-aware filenames, templates, collision policies, and batch-safe short IDs.
- Add missing PowerShell helpers including `cdx-sessions`, `cdx-last`, `cdx-live`, `cdx-batch`, `cdx-browse`, `cdx-set-dir`, and `cdx-tokenizer`.
- Add last-N-turn, multi-session batch, project-aware browser, event reports, live-context reconstruction, and reasoning-summary opt-in.
- Repair narrowly recoverable invalid JSON backslashes and distinguish repaired from unrecovered lines.
- Replace misleading approximate-token metadata with method/encoding-specific fields.
- Add export manifests, package metadata, console entry point, expanded tests, compatibility matrix, and release workflow.
- Force CLI standard output and error to UTF-8 so browser and JSON output remain deterministic through Windows legacy-code-page pipes.
