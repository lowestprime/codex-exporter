# Export integrity and manifest semantics

Each written Markdown export receives a sibling `.manifest.json` by default.
The manifest provides machine-readable provenance and separates distinct kinds
of loss or transformation that should not be conflated.

## JSON parsing

- `parse_error_count`: source lines that remained unparseable and were omitted.
- `repaired_json_line_count`: otherwise-valid JSON lines recovered by narrowly
  escaping invalid backslashes inside JSON strings.
- `repaired_json_lines`: line numbers, original decoder errors, repair class,
  and repair count.

The repair routine never joins lines, invents braces, or modifies valid JSON
escape sequences.

## Event accounting

- recognized schemas produced export records;
- ignored schemas are known transport/context records with a documented reason;
- unknown schemas are retained in the manifest for compatibility auditing;
- `--strict-events` converts unknown schemas into a failing exit status.

## Truncation accounting

Source-runtime truncation markers mean Codex omitted content before the exporter
read the rollout. `--source-truncation annotate` makes that irrecoverability
explicit. Exporter-created trimming uses separate sentinels and counters.

The manifest distinguishes markers found in raw source lines, markers reached by
record extraction, and markers present in the selected rendered record set.

## Token counting

The manifest reports method, encoding, optional library/version, exactness for
the selected encoding, and special-token policy. Exactness does not include
hidden API framing or server-side accounting.

## Reconstructed history

`--live-context` records compactions and rollbacks applied and labels the result
as reconstructed. Full chronological `--mode thread` exports recorded history
without that reconstruction; the captured native JSONL remains the fidelity
authority.


## v0.3 streaming manifest

`stream_thread` is a separate format: it reports source SHA-256 computed during the
read pass, source size at open and at completion, source lines, rendered records, parse
errors, raw upstream truncation markers when recognized, and unknown event counts.
The inline Markdown is not a byte-for-byte JSONL serialization. Use the recorded
`source_jsonl` as the fidelity authority. Unlike the original renderer, streaming
mode retains adjacent identical classified records and does not calculate tokens.

## 0.3.9 live snapshot contract

A file export represents the complete-record prefix captured for that invocation,
not all events that may arrive before ZIP compression finishes. The byte cutoff
comes from `os.fstat()` on the original open handle. Copying and verification both
stop at that fixed physical length. A second SHA-256 pass through the same handle
checks the captured prefix. Observed truncation, replacement, or rewriting fails
before rendering; later changes after successful capture do not invalidate it.

These checks support append-only rollouts. They do not establish a transactional
or atomic snapshot against arbitrary in-place writes occurring between checks.
Each batch member has an independent boundary. Title/index metadata is descriptive
and is not part of the transcript SHA-256.

Both manifest formats and CLI JSON results include `source_snapshot`:

| Field | Meaning |
|---|---|
| `policy` | `verified_prefix` |
| `original_source` | Original resolved rollout path; never a temporary staging path |
| `capture_started_at`, `capture_completed_at` | UTC capture timestamps |
| `source_encoding` | Original `plain` or `zstandard` encoding |
| `source_size_bytes_at_open`, `source_size_bytes_after_capture` | Physical source lengths observed through the open handle |
| `physical_prefix_sha256` | Hash of all physical bytes at the initial cutoff, including any later-deferred plain tail |
| `decoded_prefix_bytes` | Decoded bytes before terminal-record handling |
| `captured_size_bytes`, `captured_sha256` | Exact decoded JSONL bytes used for rendering and the native copy |
| `deferred_tail_bytes`, `deferred_tail_sha256`, `deferred_tail_reason` | Audit of an invalid or incomplete unterminated tail excluded from this invocation |
| `source_changed_during_capture` | Size or modification time changed during physical capture; ordinary growth is allowed |
| `appended_bytes_during_capture` | Observed physical size increase before capture verification completed |
| `prefix_verified` | The captured physical prefix matched a second bounded SHA-256 read |

`source_changed_during_capture=false` does not mean Codex stopped or that no events
arrived later during rendering/compression. No post-export stat of the live source
is required for success. A subsequent export can include later completed records.

A complete final JSON record without a newline is retained. Narrow repairs already
supported by the renderer are also retained and audited. An unrecoverable non-LF
tail is deferred with a stderr note; that tail may be unfinished or invalid, and
the metadata does not claim which. Newline-terminated malformed records remain in
the JSONL snapshot and count as parse errors. No missing content is reconstructed.

From 0.3.9 onward, CLI source hashes refer to the captured **decoded** JSONL in both render
paths. This intentionally makes buffered compressed exports agree with streaming
exports; `physical_prefix_sha256` preserves the separate compressed-byte identity.
Compressed frames must be complete, including checksums where present.

The archive stage can hard-link only a private, owned snapshot, never the
app-owned rollout. It verifies the staged digest and records `source_copy_strategy`
as `snapshot_hardlink` or `snapshot_copy`. Every ZIP member is independently read
and compared by hash and length before the usual loose-output cleanup. Staging
files are removed on ordinary success and exceptions; the native copy remains
with other loose outputs if archive creation or verification fails.

## 0.3.10 review companion contract

The review companion is derived from the **rendered Markdown for the selected
export**, after the caller's rendering and redaction choices. It adds no content
suppression, deduplication, summarization, or redaction. Long lines and embedded
payload text remain present. Byte-exact reconstruction of the Markdown does not
imply that Markdown contains every native event or external attachment.

The `.review` directory is retained alongside a Deflate `.review.zip`. It contains
`START_HERE.txt`, `RECENT.txt`, numbered TXT parts, `review-index.json`, and
`original-export.manifest.json` when an export manifest is available. The review
ZIP is independently decoded and checked against the generated files' SHA-256
hashes. A same-stem existing review directory or ZIP triggers a new numbered
basename; existing outputs are not replaced.

### Review index

| Field | Meaning |
|---|---|
| `markdown.filename`, `markdown.bytes`, `markdown.sha256` | Identity of the complete original rendered Markdown |
| `text_policy`, `omitted_rendered_bytes` | Lossless rendered-Markdown policy; zero Markdown bytes omitted |
| `source_sha256`, `source_snapshot` | Native-source provenance copied from the export manifest, if available |
| `native_source_verification` | Explicitly states that the review builder did not reverify native source bytes |
| `input_verification` | Whether the input was a directly read text file or CRC-checked selected members of an existing ZIP |
| `chunk_bytes_limit` | Maximum complete numbered TXT part size, including its header |
| `parts[].filename`, `header_bytes`, `body_bytes`, `file_bytes` | Part inventory and exact separation of navigation header from transcript bytes |
| `parts[].source_byte_start`, `source_byte_end` | Half-open byte range `[start, end)` within the original UTF-8 Markdown |
| `parts[].source_line_start`, `source_line_end` | One-based inclusive source line range; ranges can overlap when a long line spans parts |
| `parts[].sha256`, `body_sha256` | SHA-256 of the complete TXT file and its transcript body, respectively |
| `recent` | Explicit byte range, size, and hash of `RECENT.txt`, which is an excerpt rather than complete history |
| `original_manifest_sha256` | SHA-256 of the unchanged manifest bytes included with the review, if present |

Remove exactly `header_bytes` from each part and concatenate the bodies in index
order. The result must have exactly `markdown.bytes` bytes and the recorded
`markdown.sha256`. Part boundaries preserve UTF-8 characters; CRLF, BOM, blank
lines, and a final line without a newline are preserved. Byte ranges are offsets
in the Markdown, not in the native JSONL. Source-event references already present
in the Markdown retain their original meaning.

The default size cap is 1,000,000 bytes per complete part, configurable with
`--review-chunk-bytes` from 4,096 to 1,500,000. `RECENT.txt` contains at most 200,000
transcript bytes and stays within the selected cap with its header included. The
index sets its own token count to `null`: neither byte sizing nor an old
manifest's tokenizer report establishes the receiving model's remaining context.

### Old exports and verification scope

`--review-from EXPORT.zip` requires one Markdown member and at most one matching
manifest. It rejects duplicate, ambiguous, unsafe, encrypted, or symlink member
inventories. Only the selected Markdown and manifest are decoded and CRC-checked;
the native JSONL is not opened. Thus conversion can repair delivery compatibility
without repeatedly decompressing a large native snapshot. The original ZIP is
retained, and its source hash is carried forward as provenance.

For a newly generated export, full-archive verification completes before the
review is built and before loose-file cleanup. The review's manifest copy matches
the final manifest included in that full ZIP. If review creation fails, the full
archive and loose exports are retained. With `--no-archive`, review files are
still generated unless `--no-review` is also supplied. `--no-file` produces no
review companion.

Ordinary path output and `--open-after` prefer the review ZIP. JSON reports
`review_archive`, `review_directory`, `review_index`, `review_recent`,
`review_part_count`, and `review_verified` alongside the full archival result.
`archive` remains the full ZIP path, while `file`, `manifest`, and `source_copy`
become `null` when archival cleanup removes those loose files. The retained
review directory is outside that cleanup set.

## Archive backend and publication verification

New full-archive defaults are Deflate level 6 and `backend=auto`. Auto selects
Python for Deflate without a configured archiver, and selects a compatible
external CLI when a path is configured or LZMA is requested. The `python` backend
supports Deflate only; `7zip` explicitly selects an external CLI. Saved settings
are not migrated silently. Review ZIPs always use built-in Deflate level 6.

The full archive's `archive_plan` and CLI result record the effective backend,
method, level, and worker count; LZMA-only settings are `null` for Deflate. Each
member is fully decoded and compared to its loose source by SHA-256 and size.
Detected changes to loose-member file signatures before publication or cleanup
abort cleanup. Publication uses exclusive creation, with a verified copy fallback
when hard links are unavailable. These checks do not create an atomic transaction
against arbitrary concurrent replacement of output files.
