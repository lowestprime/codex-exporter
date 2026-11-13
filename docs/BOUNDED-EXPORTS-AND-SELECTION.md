# Bounded archives, exact selection, and the session browser

Codex Exporter 0.4.0 keeps the LZMA archive profile and verified live snapshots,
adds a hard final ZIP size ceiling, and makes selected native JSONL follow the
selected conversation scope. No application-owned rollout is modified.

## The size contract

Every successfully published export ZIP is at most **512,000,000 bytes**,
including local headers, central directory, filenames, and internal metadata.
This conservative decimal ceiling is below 512 MiB. Use `--archive-max-bytes N`
or `--set-archive-default max_bytes=N` to choose a smaller limit, from 16,384
through 512,000,000 bytes. A larger limit is rejected before export.

The exporter first compresses the full requested member set using the configured
method. A fitting single-session ZIP retains the familiar three members:
Markdown, provenance manifest, and optional native JSONL. It has no extra review
ZIP or output folder. Optional unknown-event and review content stays inside the
same archive set.

If the actual completed ZIP exceeds the ceiling:

1. Reuse compressed streams for members that fit. They are not decompressed and
   recompressed merely to move them into another ZIP.
2. Split oversized individual members using measured compression sizes, favoring
   newline boundaries. Compress only the necessary pieces.
3. Pack the observed compressed pieces using first-fit decreasing with metadata
   overhead reserved. This is a practical packing heuristic, not a proof of the
   globally smallest possible number of archives.
4. Decode every final member, check CRCs, sizes and SHA-256 hashes, and verify
   complete, contiguous original-byte coverage before publishing any part.
5. Check actual on-disk ZIP sizes again during publication. Remove generated loose
   files only after the complete verified set is published successfully.

Parts are independently readable ZIPs named `.part0001-of-000N.zip`, not
`.zip.001` volumes or slices of a compressed stream. Upload all parts needed for
the requested scope. Every overflow part contains `ARCHIVE_PART.json` with the
set identifier, expected part count, original filenames/hashes/sizes, and each
piece's original byte offsets, SHA-256 and line-boundary status.

Ordinary line-aligned JSONL segments remain JSONL. A single source record can
itself exceed the cap; lossless, bounded output then requires a byte fragment,
marked with a `.bytes` suffix and explicit metadata. Such a fragment is **not** a
standalone JSON record. Reconstruct its original by sorting that original's
pieces by offset, checking there are no gaps/overlaps, concatenating exact bytes,
and verifying the original size and SHA-256 before parsing it. No record is
silently dropped, summarized, or rewritten to force a fit.

No lossless compressor can guarantee that arbitrary content fits in one archive
of fixed size. Trying many codecs/dictionaries also costs time. The normal
profile stays LZMA, level 9, dictionary 256m, word size 64, requested threads 19.
There is no silent Deflate conversion or unverified switch to XZ/7z/Zstandard.

## Batch and project exports

Multiple Codex sources now share one bounded archive set by default. Each session
keeps its distinct MD/manifest/JSONL names; `BATCH_INDEX.json` maps sessions to
those members. Optional review navigation is namespaced per session. This avoids
one small archive per session when a combined export fits under the cap.

Use `--separate-archives` to retain one independently bounded archive set per
session. With `--json`, a bundled result is one object containing `archives`,
`archive_parts`, and `exports`; separate results retain the per-session list.
Normal stdout prints every final ZIP path exactly once. Progress and browser UI
use stderr. Explicit `--no-archive` and `--no-archive-cleanup` remain available.

## Find the exact inclusive starting turn

```powershell
cdx-turns 'SESSION-ID'
cdx-since 'SESSION-ID' 'text:distinct phrase from the prompt'
cdx-since 'SESSION-ID' 'turn:42'
cdx-since 'SESSION-ID' 'id:recorded-turn-or-client-id'
cdx-since 'SESSION-ID' 'time:2026-11-13T09:30:00-08:00'
cdx-since 'SESSION-ID' 'text:repeated phrase' -Occurrence 2
```

Direct equivalents and aliases:

```text
codex-export --session-id SESSION-ID --list-turns --json
codex-export --session-id SESSION-ID --since "distinct phrase"
codex-export --session-id SESSION-ID --since-turn 42
codex-export --session-id SESSION-ID --since-id RECORDED-ID
codex-export --session-id SESSION-ID --since-text "repeated phrase" --since-occurrence 2
codex-export --session-id SESSION-ID --since-time 2026-11-13T09:30:00-08:00
codex-export --session-id SESSION-ID --last 5 --mode thread
codex-export --session-id SESSION-ID --turn 42
```

Selection is inclusive: the selected actual user prompt, subsequent assistant
messages, tool calls, outputs, steering messages and all intervening native
records through the captured snapshot end are included. The exporter does not
wait for an active assistant response to finish. Later writes belong to the next
export. If an earlier tool call began before the selected prompt but completed
after it, the source interval preserves the later completion; it does not pull
the earlier call or previous turns back into scope.

Text matches are case-insensitive literal substrings with normalized whitespace,
searched across the complete prompt rather than its preview. Multiple matches
produce an error with candidate turns; choose `--since-occurrence N`, an ID, or
`turn:N`. A bare integer is literal text; use `turn:N` for a turn number. ID
selection prefers exact IDs and permits an unambiguous prefix. IDs available in
native records include turn, message/item and client IDs.

Clock timestamps require `Z` or an explicit UTC offset. A date alone means local
midnight in the export process's timezone. Time selection chooses the first user
turn at or after that timestamp, not a point halfway through an earlier turn.
The requested selector and resolved threshold are recorded in the manifest.
ChatGPT conversation timestamps are not automatically Codex turn IDs: inspect the
actual rollout with `--list-turns` before relying on a number or ID.

`--since` defaults to chronological thread mode and rejects conflicting selectors
or reconstructed live context. `--last` aliases `--last-n-turns`; `--turn N`
aliases `--mode turn --response N`. `--fast` can render a selected `--since` or
last-N thread after excerpting it, so old history is not parsed into an in-memory
record list or tokenized merely to render the selected interval.

## Native JSONL fidelity

`--turn`, `--last`, `--since`, response/message/range and other subset modes no
longer attach the whole historical JSONL. Contiguous turn selections retain the
exact raw interval. Other record selections retain all source lines contributing
to the selected rendered records, including both sides of a merged tool-call /
output card. Line endings, JSON formatting, Unicode and source bytes are copied
without reserialization.

Minimal session metadata and the applicable turn configuration may precede the
selected records. The manifest marks these as context and records parent-snapshot
hashes plus original/excerpt line and byte ranges. `source_sha256` identifies the
included excerpt; `source_hash_scope`/`sha256_scope` identifies that meaning. The
original captured-source hash remains in `selection.original_snapshot` and
`selection.input_snapshot_sha256`, including nested scope when selection occurs
in more than one stage.

Post-anchor compaction records may embed a history summary or replacement
history. Removing that content would change a selected native event. These
records remain intact and are explicitly counted/disclosed. Earlier standalone
turn records are not reintroduced. Approximate `--live-context` reconstruction
cannot be mapped to an exact subset of native lines and is rejected when combined
with subset source-copy exports; choose chronological export or explicitly omit
the native copy.

Duplicate native representations of one user prompt are identified by available
stable IDs and equivalent cross-schema surfaces. Genuine repeated steering
prompts remain distinct. Injected model-context envelopes do not invent extra
user turns; explicit recorded user-message events remain authoritative. Media-only
user turns have stable markers for selection. Native media payloads remain in
JSONL; a marker does not assert image understanding or audio transcription.

## Browser interaction

Run `cdx-browse` or `codex-export --browse`. The chooser completes before tokenizer
or source-resolution progress starts, so a progress heartbeat cannot cover the
input prompt. UI and prompts use stderr, leaving stdout for paths or JSON. Quit,
EOF and Ctrl+C cancel cleanly. Browsing does not repeatedly scan large rollout
contents during ordinary navigation.

| Control | Action |
|---|---|
| `1`, `1,3-5` | Export visible row(s) immediately |
| `+1,3-5`, `-2` | Add/remove visible rows in a basket retained across pages |
| `+page`, `+all` | Add visible page or all matching sessions |
| `x`, `basket`, `clear` | Export, inspect or clear the basket |
| `n`, `p`, `page N` | Navigate pages |
| `/words` | Literal AND search across session metadata |
| `s`, `id PREFIX` | Find an ID or rollout filename |
| `v N` | Inspect a session preview |
| `m`, `b` | Project view / back |
| `filter available`, `filter missing` | Filter rollout availability |
| `filter live`, `filter archived`, `filter all` | Filter archive state; live means unarchived, not proven running |
| `sort recent`, `oldest`, `size`, `title`, `project` | Sort results |
| `size N`, `r`, `q` | Set page size, refresh catalog, quit |

Initial controls: `--browse-query`, `--browse-page-size`, `--browse-status`, and
`--browse-sort`. Width-aware rendering, Unicode/ASCII fallback, colors where
supported, and visible missing-rollout markers keep the display usable. Missing
selected sources block export instead of silently disappearing. Projects are
grouped by normalized full workspace path, so equal folder names remain distinct.

## ChatGPT compatibility boundary

As checked 2026-11-13, OpenAI documents a 512 MB file limit and a separate
2-million-token limit for text/document files. It says Data Analysis may open ZIPs
and that readability depends on contents and the available tools. It does not
publish an unconditional ZIP-LZMA, 7z, XZ, or Zstandard decoder guarantee for every
conversation. See [OpenAI's file-upload guidance](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)
and [file management limits](https://help.openai.com/en/articles/20001052-using-library-to-manage-files-in-chatgpt).

The exporter guarantees its own output-size/integrity contract when it reports
success. It cannot guarantee that another conversation materializes an uploaded
file, has a particular codec, reads every member, or places all content into the
model's active context. Use `--diagnose-archive` in the receiving runtime to
separate missing-file, unsupported-codec, dictionary-guard and corruption cases.
Optional bounded review text is available when direct text attachment is needed.
