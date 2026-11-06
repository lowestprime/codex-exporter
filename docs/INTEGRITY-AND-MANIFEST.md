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
as reconstructed. Full chronological `--mode thread` remains the authoritative
history export.


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

All 0.3.9 CLI source hashes refer to the captured **decoded** JSONL in both render
paths. This intentionally makes buffered compressed exports agree with streaming
exports; `physical_prefix_sha256` preserves the separate compressed-byte identity.
Compressed frames must be complete, including checksums where present.

The archive stage can hard-link only a private, owned snapshot, never the
app-owned rollout. It verifies the staged digest and records `source_copy_strategy`
as `snapshot_hardlink` or `snapshot_copy`. Every ZIP member is independently read
and compared by hash and length before the usual loose-output cleanup. Staging
files are removed on ordinary success and exceptions; the native copy remains
with other loose outputs if archive creation or verification fails.
