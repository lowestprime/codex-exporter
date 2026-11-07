## 0.3.12 — Separate archive availability, codec capability, and integrity (2026-11-07)

- Add opt-in read-only ZIP diagnostics to the CLI and a standalone standard-library reader. Report actual runtime capabilities, archive size/SHA-256, inventory, encoded LZMA dictionaries, and full streamed member size/CRC/SHA-256 checks.
- Classify absent paths, access failures, missing/unsupported codecs, explicit dictionary policy, actual memory failures, malformed data, and source changes separately. Metadata-only results never claim full content verification.
- Validate LZMA envelope bounds and report unsafe/duplicate/link names without extracting or executing members. Keep decoder-memory scope explicit: read chunk size is not a total memory ceiling.
- Preserve the normal compression profile, single three-file ZIP, live snapshots, Windows publication, review options, progress, saved configuration, and compression path. No default diagnostics or companion outputs.
- Add regression coverage and receiving-chat diagnostic instructions. Correct stale compatibility documentation; do not claim remote ChatGPT compatibility from local codec success.

## 0.3.11 — One LZMA ZIP, inherited Windows access, measured progress (2026-11-07)

- Restore LZMA level 9, a 256 MiB dictionary, word size 64, 19 requested threads, and the automatic compatible external backend. Retain explicit saved configuration until deliberately changed; document restoring earlier Deflate overrides.
- Default to one ZIP containing the original Markdown/manifest/native-JSONL triplet. Disable review companions by default and place opt-in review TXT/index/guide content inside the same ordinary archive. Preserve explicit standalone `--review-from` conversion; honor `--no-archive` with verified review text only and no ZIP.
- Create fresh Windows destination files with inherited parent DACLs; verify copied bytes and readback instead of carrying private staging permissions through hard links or renames. Keep private staging and live rollout permissions unchanged and retain the POSIX hard-link optimization.
- Add repeatable JSON access diagnostics and narrowly scoped inherited-ACL repair for explicit recognized exports. Refuse source paths, broad directories, links/junctions, multiple hard links, wildcards, and unexpected review members; never take ownership or grant global access.
- Add terminal bars/icons/colors, plain and JSON progress styles, measured per-stage rates/elapsed time/ETA, and separately reported checksum/copy/readback. Stream actual 7-Zip compression progress with bounded diagnostics and interruption handling; keep stdout machine-readable.
- Preserve verified fixed-prefix live snapshots, original source provenance, full ZIP member hash/length checks, source-copy validation, collision protection, and cleanup only after success.
- Extend regressions for real Windows ACL inheritance/repair, three-member defaults, embedded review, measured progress, streaming process output, cancellation, output/source path aliases, and publication failures. Platform outcomes are reported by the release's CI runs.

## 0.3.10 — Portable review companions and Deflate archives (2026-11-06)

- Create a verified `.review.zip` and retained `.review` text directory for ordinary local Codex file exports by default. Printed/opened output prefers the review ZIP; JSON retains both archive paths. `--no-review` opts out.
- Split rendered Markdown into UTF-8 TXT parts without changing any source bytes. Index original byte/line ranges, whole-part/body SHA-256 hashes, and source provenance; retain a bounded, explicitly labeled recent excerpt and upload guide.
- Bound each complete TXT part, including its header, to 1,000,000 bytes by default; expose `--review-chunk-bytes` for 4,096–1,500,000 bytes. Long lines can span parts without Unicode corruption or content trimming.
- Add `--review-from` for existing ZIP/MD/TXT exports. Convert legacy LZMA ZIPs by reading only the selected Markdown and matching manifest, retaining the input and explicitly distinguishing CRC checks from native-source provenance.
- Default new full archives to Deflate level 6. Add `--archive-backend auto/python/7zip` and a persisted backend option. Auto uses built-in Deflate unless an external archiver is configured; Python can be forced without erasing the saved path. Preserve saved LZMA preferences until explicitly changed.
- Verify review ZIPs independently with method, CRC, and SHA-256 checks. Keep loose TXT parts after archival cleanup, reject unsafe/ambiguous input inventories, and choose new names on collisions.
- Support verified archive publication when hard links are unavailable and recheck loose-member signatures before cleanup. Preserve the live-prefix snapshot behavior introduced in 0.3.9.
- Extend synthetic regression coverage for exact review reconstruction, size and Unicode boundaries, archive backends, legacy conversion, collision handling, and native-source isolation.

## 0.3.9 — Verified snapshots of running threads (2026-11-06)

- Make bounded, verified snapshots the default for all local Codex export modes. Ordinary appends after the capture boundary no longer abort rendering or archival cleanup.
- Detect truncation, replacement, and prefix rewriting during capture using a fixed-length copy and second SHA-256 read from the same open handle.
- Keep complete EOF records, including narrowly repairable JSON; record and defer an invalid or unfinished unterminated tail. Preserve malformed completed records in the native snapshot and parse audit.
- Use one decoded snapshot for rendered content, manifests, and native JSONL archives; retain original-path provenance and expose exact capture metadata.
- Decode compressed rollouts once, support concatenated Zstandard frames, and reject incomplete frames/checksums.
- Reuse owned snapshots through exclusive hard links with a verified copy fallback, avoiding a redundant large source copy. Replace buffered whole-file hashing allocations with the captured digest.
- Open the final archived result after packaging in buffered mode; reject incompatible explicit fast options before capture.
- Synchronize and check every bundled Python module, including the new snapshot module, across package and standalone distributions.
- Add 36 deterministic live-export tests covering both renderers, all archive layouts, partial-record recovery, captured hashes, and source changes before/after capture.

## 0.3.8 — ZIP mixed Store/LZMA validation and PowerShell wrapper fix (2026-11-02)
- Correct per-member ZIP verification: allow Store (0) when 7-Zip decides LZMA is wasteful for a short or incompressible member, while rejecting unauthorized algorithms, unexpected member paths, duplicates or corrupt content.
- Preserve full SHA-256/size verification and fail-closed deletion semantics.
- Reject a substantive (>=64 KiB) entirely uncompressed archive instead of incorrectly reporting LZMA success.
- Return effective per-member methods in `archive_member_methods` and flag `archive_used_store_fallback` in JSON output.
- Repair dictionary-key membership in all PowerShell `cdx-*` archive-option forwarders; Windows smoke tests cover ten invocations without exporting real rollouts.
- Synchronize the packaged Codex skill archive pipeline with the installed package implementation.

## 0.3.7 — Safe archival exports (2026-11-01)

- Default validated three-member ZIP/LZMA/Ultra archive per Codex session export, including an identical-stem decoded JSONL copy and manifest. Original Codex source untouched.
- Full SHA-256 member verification, no archive overwrite, per-job overrides and persistent archive defaults.
- Auto-discovery for compatible installed 7-Zip, 7-Zip ZS and PeaZip backend CLIs; explicit --archiver-path for NanaZip/other installs.
- Cleanup of loose export triplet only after archive verified; unknown-event sidecars remain outside by design.
- Archive result metadata identifies the ZIP and archived members; JSON outputs no longer claim removed loose files remain present.

## v0.3.6 — 2026-10-25

- Classify Codex Desktop `response_item/additional_tools` as known tool-catalog metadata instead of incorrectly reporting an unknown schema. No catalog definitions are emitted into user-facing transcript text.
- Expose `title_source` in streaming Markdown frontmatter and its manifest (explicit overrides and durable sidebar rename have priority).
- Add honest end-of-document streaming source coverage statistics and decoded source SHA-256, with no in-memory whole-transcript buffering.
- Preserve all previous v0.3.5 filename, title, and PowerShell routing improvements; metadata reflects only validated source evidence.

## 0.3.5
- Latest append-only session-index rename overrides stale SQLite `threads.title` throughout discovery and export.
- Stream-mode Markdown gains YAML frontmatter and compact export map.
- Stream filename generation honors persisted templates and includes unique UUID suffix, omitting unavailable precomputed counts.
- Optional conservative PowerShell helper-routing patch tracks the latest installed Python package.

# v0.3.4 — Read compressed Codex rollouts (2026-10-22)

- Discover `.jsonl.zst` cold-storage sessions as existing rollouts (plain `.jsonl` takes precedence when both exist).
- Stream decompress directly into archival Markdown and retain source encoding + decoded byte totals in manifest.
- Support both compressed and plain transcripts in ordinary/full-memory parsing and session header scanning (optional `zstandard` dependency).
- Never modify, decompress in place, or overwrite app-owned compressed source files.

## 0.3.3 (2026-10-22)
- Read numbered `conversations-NNN.json` from ZIP or extracted account-export directory; validate gaps and duplicates.
- Add read-only `--chatgpt-audit` and per-shard provenance. Require optional ijson for shards >64 MiB.
- Preserve local Codex export behavior and existing commands.

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
