# Storage, listings, and timestamps in 0.5.0

## Archive-only jobs use a private transaction

A normal archived export captures and verifies one fixed source prefix, renders
inside an owned temporary directory, and borrows that immutable snapshot as the
native JSONL archive member. It publishes verified ZIPs only. A failure removes
the current job's owned staging files; a retry does not leave another loose
Markdown/manifest/source triplet in the output folder.

The original live rollout is never a hard-link publication source, moved, deleted,
or edited. Changes after the captured prefix remain for the next export. The
snapshot is still necessary for a stable export; this is not a zero-space export.
Explicit loose-file retention (`--no-archive` or `--no-archive-cleanup`) requires
space for those requested outputs. Private batch staging may link its owned
snapshot internally; publicly visible Windows files are created with the output
folder's inherited permissions.

Use a work volume with enough space when the output volume is constrained:

```powershell
# Substitute an existing volume with adequate space.
cdx-thread 'SESSION-ID' --work-dir 'D:\CodexExportWork'
# Persist that selection; an invocation flag overrides it.
codex-export --set-archive-default 'work_dir=D:\CodexExportWork'
# Restore output-volume staging.
codex-export --set-archive-default 'work_dir='
```

The default work parent is the output directory. Exact known allocations are
checked against the actual volume, with 8 MiB of headroom; these checks do not
reserve disk space against other writers. Disk-full errors identify the work and
output paths and suggest the relevant setting. The final output drive still
needs space for the verified archive set. Existing archives, prior loose exports,
Codex databases, caches, sessions and other app data are never purged to make room.

## Bounded compression and publication

Small exports retain a single ordinary archiver invocation. Larger inputs are
sampled through the actual codec to choose a useful starting range, then
compressed as exact ranges through the configured encoder without raw segment
copies. The bounded 256 KiB probes add a small fixed cost; highly compressible
inputs can still use one ordinary archiver invocation. Completed compressed payloads are reused when assembling final ZIPs;
assembly does not recompress them. A fitting export retains original member names
and one ZIP. Every final part, including its navigation metadata, must satisfy the
configured cap, at most 512,000,000 bytes.

Packing uses measured compressed bytes and actual metadata overhead. A bounded
search improves packing for modest piece inventories. Its receipt describes the
scope of any lower-bound/optimality result: this is not a guarantee of a globally
best compression ratio or a minimum count across every possible segmentation.
Encoder buffering or an unexpectedly poor compression ratio can require a bounded
retry. Compression speed depends on data, storage, codec, and available resources;
requested threads do not imply that every stage uses that many CPU workers.

The configuration remains LZMA, level 9, dictionary 256m, word size 64, and 19
requested threads. The configured dictionary is a maximum; stdin encoding is
given the known range length so short members do not request the entire 256 MiB.
7-Zip may round its encoded dictionary property upward. JSON receipts disclose
the actual per-member and maximum dictionary sizes. No automatic Deflate
downgrade is introduced. Each published
set undergoes full decoding, CRC checks, SHA-256 comparison, and byte-range
coverage checks. Consumed private candidates are retired progressively. Windows
publication retains fresh destination-folder permissions and verifies the copied
bytes before consuming its private source.

### Names and content

The resolved Markdown stem also names its ordinary ZIP and multipart ZIPs:

| Item | Pattern |
|---|---|
| Markdown | `<export-stem>.md` |
| Native JSONL | `<export-stem>.jsonl` |
| Provenance | `<export-stem>.manifest.json` |
| Fitting archive | `<export-stem>.zip` |
| Multipart archive | `<export-stem>.part0001-of-0003.zip` |
| Segmented Markdown | `<export-stem>.segment000001.md` |
| Segmented native record bytes | `<export-stem>.segment000001.jsonl` or `.jsonl.bytes` |

A `.bytes` suffix explicitly identifies a fragment that must be reconstructed by
its recorded byte offset before parsing the original file. Long filenames are
bounded and retain a digest where truncation would otherwise hide identity. Batch
members keep each session's own resolved stem. Batch archive names use the saved
filename template with mode `batch`. Names are reserved across the current job,
including before any private render is publicly visible, so repeated titles or
custom templates cannot collide silently. Batch `{counts}` is omitted because
there is no single aggregate Markdown member. Explicit `{lines}` or `{tokens}`
in a combined-batch template is rejected before export; use `--separate-archives`
for per-file count-based names.

No new public export folder or review ZIP is added to default output. Optional
review conversion remains explicit. Previous archives are never overwritten;
`--collision skip` returns a successful skipped result without rewriting a
manifest or opening a new archive.

## Sort and page listings

```powershell
cdx-sessions --sort 'project:asc,modified:desc'
cdx-sessions --sort 'size:desc,title:asc'
cdx-turns 'SESSION-ID' --sort 'turn:desc'
cdx-turns 'SESSION-ID' --sort 'timestamp:asc' --page-size 24
codex-export --session-id 'SESSION-ID' --list --sort 'chars:desc'
codex-export --session-id 'SESSION-ID' --list-responses --sort 'tokens:desc'
cdx-browse --browse-sort 'project:asc,size:desc'
```

Use comma-separated fields with optional `:asc` or `:desc`. `--sort-order` supplies
a direction for fields without one; `auto` follows each field's documented order.
`recent`, `oldest`, and `native` are convenience aliases. Sorting is presentation
only: a sorted row keeps its original selector number and native IDs. Session,
turn, message, response, event, audit and browser/project listings expose their
own relevant columns. `--help` lists accepted keys.

In a terminal, long listings use the usable terminal height and wrap long rows.
`--page-size N` sets a maximum, not a request to overflow a shorter terminal. The
pager adapts to resizing and restores terminal state on exit:

- Enter, Space or Page Down: next page.
- Page Up: previous page.
- Up/Down: one line.
- Home/End: first/last page.
- Q: quit the listing.

`--pager never` prints all rows. `--pager always` also pages short terminal lists.
Pipes, redirected files and JSON output remain complete and noninteractive in all
modes. Browser selection keeps its own search, project, preview and export controls.
At an empty browser prompt, arrow/page keys navigate immediately and Enter advances.

```powershell
codex-export --set-default pager=auto --set-default page_size=0
codex-export --set-default sort_order=auto
cdx-turns 'SESSION-ID' --json > turns.json
```

A plain JSONL `--list-turns` scan no longer creates a disk snapshot. It indexes a
fixed prefix and independently verifies its SHA-256, tolerates later appends, and
rejects source replacement, truncation, or edits to that prefix. An unparseable
unterminated final line is deferred and disclosed by byte count. Compressed
rollouts still need a decoded snapshot. Actual exports retain immutable snapshots.

### Turn-numbering correction

Complete known internal goal, browser/page-state and interruption envelopes are
context records, not new human prompt anchors. Their native bytes and rendered
context notes are retained. Mixed context-plus-user messages, unclosed wrappers,
actual question replies, heartbeats and literal `/goal` requests remain input.
Text-only deduplication also respects distinct recorded turn IDs.

This correction can change numeric selectors from older versions. Re-run
`cdx-turns` before reusing old numbers. Prefer an exact message ID, distinctive
prompt text with explicit occurrence when ambiguous, or a timestamp. Turn IDs can
repeat across native rows; ambiguity continues to require an explicit choice.
JSON listings and excerpt manifests identify `human_user_turns_v2` and disclose
excluded context counts. Sorting itself never renumbers anchors.

## Human-readable recorded timestamps

Available source timestamps appear beneath turn/record headings by default:

```text
November 14, 2026 at 05:50:00 PM PST
```

The formatter uses the offset applicable at the event instant. It handles
standard/daylight transitions, Unix epoch zero, native message metadata, and
ChatGPT per-message times. It never substitutes the export time for missing source
time. Naive timestamps are labeled `timezone unspecified`; date-only values say
`time not recorded`. ISO/native provenance stays unchanged.

```powershell
cdx-thread 'SESSION-ID' --no-timestamps
codex-export --set-default timestamps=false
cdx-thread 'SESSION-ID' --timestamps --timestamp-timezone UTC
codex-export --set-default timestamps=true --set-default timestamp_timezone=local
```

Accepted timezone settings are `local`, `UTC`, `source`, fixed offsets such as
`-08:00`, and IANA names such as `America/Los_Angeles`. For a negative offset, use
`--timestamp-timezone=-08:00`. Default local formatting uses the OS; explicit IANA
zones need a system timezone database or the optional `codex-exporter[timezones]`
extra (Python `tzdata`, particularly relevant to Windows). Invalid zones fail
before export files are written. Disabling human labels does not erase raw
provenance dates or the classic metadata/index tables.

## Help and configuration recovery

`codex-export`, `codex-exporter`, and their `--help` forms show the same versioned
help without initializing tokenization or resolving sources. `cdx-help` forwards
help through the configured shell interpreter. Every public flag has a purpose,
accepted syntax/values, and effective defaults where relevant; saved preferences
are overridden by explicit invocation flags. Browser-only modifiers, such as
`codex-export --browse-sort recent`, enter the browser.

Help, version, inspection, reset and corrective configuration commands remain
available when a saved preference is invalid. Markdown on stdout and JSON receipts
are different output representations; `--stdout --json` is rejected. An archival
`--manifest-path` must be alongside the public Markdown destination; loose exports
can use an explicitly separate directory.

## Verification boundaries

A valid ZIP below the cap establishes a local archive property. It cannot establish
that a different conversation has materialized the attachment, has the decoder,
parsed every member, or read every token. Diagnose receiving-runtime failures
separately. OpenAI's upload guidance also distinguishes file-size and document-token
limits; compression does not reduce the amount of text after decompression.

Primary implementation references:

- [Python temporary directory cleanup](https://docs.python.org/3.13/library/tempfile.html)
- [Python disk usage and filesystem operations](https://docs.python.org/3.13/library/shutil.html)
- [Python ZIP methods and streaming reads](https://docs.python.org/3.13/library/zipfile.html)
- [Python timezone conversion](https://docs.python.org/3.13/library/datetime.html#datetime.datetime.astimezone)
- [IANA timezone data and Windows support](https://docs.python.org/3.13/library/zoneinfo.html#data-sources)
- [OpenAI file uploads guidance](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)
