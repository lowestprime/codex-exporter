# Export integrity and manifest semantics

Each written Markdown export receives a `.manifest.json` by default. The ordinary
0.3.11 output is one ZIP containing Markdown, manifest, and captured native JSONL;
loose files are removed only after verification and publication. The manifest
provides machine-readable provenance and separates distinct kinds of loss or
transformation that should not be conflated.

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

The archive stage verifies the private, owned snapshot before native-copy staging;
it never links the app-owned rollout. From 0.3.11, Windows creates a fresh
destination file so it inherits the output folder ACL. POSIX may hard-link the
private snapshot. The result records `source_copy_strategy` as `snapshot_hardlink`
or `snapshot_copy`; copying includes digest comparison and independent readback.
Every ZIP member is independently decoded and compared by hash and length before
loose-output cleanup. Staging is removed on ordinary success and exceptions;
recoverable loose export files remain when archive creation or verification fails.

## Review text contract and 0.3.11 output layouts

Review text is **disabled by default**. An ordinary successful 0.3.11 export
leaves one three-member ZIP. `--review` opts into additional review text inside
that same archive. Its flat additional members are `START_HERE.txt`, `RECENT.txt`,
`review-index.json`, and `part-0001.txt`, etc. The original export manifest is
referenced directly; there is no duplicate `original-export.manifest.json` member
in the embedded layout. No separate review ZIP or directory is published.

Review parts derive from the **rendered Markdown for the selected export**,
after rendering and redaction choices. The review builder adds no suppression,
deduplication, summarization, or redaction. Long lines and embedded payload text
remain present. Byte-exact Markdown reconstruction does not imply that Markdown
contains every native event or external attachment.

An explicit `--review-from` conversion retains the standalone layout introduced
in 0.3.10: a Deflate `.review.zip` and `.review` directory containing the guide,
recent excerpt, numbered TXT parts, index, and `original-export.manifest.json`
when supplied. The original source remains intact. With `--no-archive`, either
`--review` or `--review-from` creates only the verified review text directory and
no ZIP. `--no-archive` alone produces no review output. Use `--no-review` to
suppress review regardless of a persisted opt-in. `--no-file` produces no review
output.

### Review index

The 0.3.11 writer uses schema version 2. It preserves the byte-range/hash contract
and distinguishes embedded from standalone provenance.

| Field | Meaning |
|---|---|
| `markdown.filename`, `markdown.bytes`, `markdown.sha256` | Identity of the complete original rendered Markdown |
| `archive_context` | `embedded_in_primary_export` or `standalone_review` |
| `text_policy`, `omitted_rendered_bytes` | Lossless rendered-Markdown policy; zero Markdown bytes omitted |
| `source_sha256`, `source_snapshot` | Native-source provenance copied from the export manifest, if available |
| `native_source_verification` | States that the review builder itself did not reverify native source bytes |
| `input_verification` | Directly read text or CRC-checked selected members from an existing ZIP |
| `chunk_bytes_limit` | Maximum complete numbered TXT part size, including its header |
| `parts[].filename`, `header_bytes`, `body_bytes`, `file_bytes` | Part inventory and separation of navigation header from transcript bytes |
| `parts[].source_byte_start`, `source_byte_end` | Half-open byte range `[start, end)` within the original UTF-8 Markdown |
| `parts[].source_line_start`, `source_line_end` | One-based inclusive source line range; ranges can overlap across long-line splits |
| `parts[].sha256`, `body_sha256` | SHA-256 of the complete TXT file and its transcript body |
| `recent` | Byte range, size, and hash of `RECENT.txt`, which is an excerpt |
| `manifest.filename`, `manifest.sha256`, `manifest.location` | Exact referenced manifest identity; location is `same_archive` or `review_bundle` |
| `original_manifest_sha256` | Retained manifest-hash field for compatibility |
| `native_source_member` | Native JSONL member of an embedded primary archive, when included |
| `not_included_in_review_parts` | Distinguishes the TXT parts from other members of the enclosing archive |

Remove exactly `header_bytes` from each part and concatenate the bodies in index
order. The result must have exactly `markdown.bytes` bytes and the recorded
`markdown.sha256`. Part boundaries preserve UTF-8 characters; CRLF, BOM, blank
lines, and a final line without a newline are preserved. Byte ranges refer to the
Markdown, not native JSONL. Existing source-event references retain their meaning.

The default size cap is 1,000,000 bytes per complete part, configurable with
`--review-chunk-bytes` from 4,096 to 1,500,000. `RECENT.txt` contains at most 200,000
transcript bytes and stays within the selected cap including its header. The
index token count is `null`: byte sizing and a historical tokenizer report do
not establish a receiving model's available context or prove complete ingestion.

### New archive verification and result fields

Embedded review files are prepared in private staging and verified against their
generated hashes. The enclosing export manifest then receives the complete member
inventory. The review index's manifest hash/provenance is refreshed to match
those final manifest bytes, without rereading/rebuilding all transcript parts.
All primary and review members use the selected archive compression policy and
are independently decoded and hash/length checked before publication and cleanup.

For the embedded layout, stdout and `--open-after` select the primary archive.
CLI JSON includes:

| Field | Meaning |
|---|---|
| `archive`, `review_archive` | The same primary ZIP path when embedded review is enabled |
| `review_layout` | `embedded` |
| `review_directory` | `null`; no public review directory was created |
| `review_index_member`, `review_recent_member`, `review_start_member` | Member names inside the ZIP, not loose filesystem paths |
| `review_part_count` | Number of chronological TXT parts |
| `review_verified` | Generated review hashes were checked; enclosing ZIP verification remains separate |
| `review_markdown_sha256`, `review_markdown_bytes` | Original rendered Markdown identity |
| `review_member_sha256` | Review member hash map |

`archive_verified` describes full ZIP verification. `file`, `manifest`, and
`source_copy` become `null` when their loose files are removed by successful
cleanup. Explicit standalone conversion retains filesystem paths for its review
directory/index/recent file and its own separate review archive unless
`--no-archive` is selected. Existing archives
or review outputs are not silently overwritten.

### Existing exports and verification scope

`--review-from EXPORT.zip` requires one Markdown member and at most one matching
manifest. It rejects duplicate, ambiguous, unsafe, encrypted, or symlink member
inventories. Only the selected Markdown and manifest are decoded and CRC-checked;
the native JSONL is not opened. This can change the delivery format without
repeatedly decompressing a large native snapshot. The original ZIP is retained,
and its source hash is carried forward as provenance. Conversion does not
reconstruct data omitted by Codex or the earlier renderer, nor does it repair
filesystem access rights; `--diagnose-access`/`--repair-access` address that
separate issue.

## Output-path separation

Before writing, generated paths must be distinct from protected source paths and
from each other. Resolved paths detect relative/symlink/junction aliases; existing
filesystem identities also detect differently named hard links. Explicit manifest
paths cannot overwrite live source JSONL or the rendered Markdown. Packaging
rechecks its core path separation before constructing the member inventory.
These are read-only preflight checks, not a transaction against hostile concurrent
path replacement.

## Archive backend and publication verification

New full-archive defaults are LZMA level 9, dictionary `256m`, word size 64,
19 requested threads, and `backend=auto`. Auto selects a compatible external
CLI when LZMA is requested or an archiver is configured. It selects Python for
explicit Deflate without a configured archiver. The `python` backend supports
Deflate only; `7zip` explicitly selects an external CLI. Saved choices are not
silently migrated. Explicit standalone review ZIPs use built-in Deflate level 6;
embedded review uses the enclosing ZIP's selected compression policy.

The `archive_plan` and CLI result identify the effective backend, requested
method/level/thread settings, and LZMA controls where applicable. A requested
thread limit does not establish actual worker utilization. Each member is
fully decoded and compared to its loose source by SHA-256 and size. Detected
loose-member signature changes before publication or cleanup abort cleanup.
LZMA ZIP method 14 is validated along with permitted per-member Store fallback;
substantive all-Store archives and other unrequested methods are rejected.

Windows publication always creates a new destination file. The ordinary
creation inherits the chosen parent directory's ACL, without copying the private
staging ACL or changing source/parent ownership or rights. Source checksum,
bounded copying, flush, and independent destination readback are separate
reported stages. POSIX permits an exclusive hard link and verified-copy fallback.
A failed copy removes only the destination object it created and retains source
staging until the enclosing workflow's cleanup. Existing destinations and
detected replacements are preserved. These checks are not an atomic transaction
against arbitrary concurrent replacement of output paths.

## Access diagnosis and scoped repair

Windows access diagnostics report a real read probe, file owner SID, DACL
protection and inherited/explicit ACE counts, the security descriptor, and parent
security. They do not infer unreadable file contents from Explorer size or a
blank editor buffer. A matching temporary-directory OWNER RIGHTS issue is
[documented upstream](https://github.com/python/cpython/issues/157876), but the
actual ACL of an affected file requires direct observation.

Explicit repair accepts only recognized `codex_` artifacts and validated flat
`.review` directories. It resets inherited DACLs with exact-path `icacls`, without
ownership changes, new global grants, app-source edits, or broad directory resets.
The parent must itself allow the intended user, and the caller needs permission
to change each selected DACL. Before/after results distinguish attempted repair
from verified readability and inherited ACL state. Refusal and partial failure
remain visible; the helper does not broaden its scope automatically.
