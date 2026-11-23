# Progress and incremental selection in v0.5.2

Version 0.5.2 repairs streamed compression telemetry and several independent
selection defects. It preserves the live-source snapshot policy, bounded ZIP
format, configured codec, dictionary ceiling, requested thread limit, saved
archive size limit, and output/workspace preferences.

## What compression progress measures

The bounded range compressor sends a known number of native bytes to 7-Zip's
standard input, avoiding another raw segment copy. Although the exporter knows
that range length, the encoder does not receive a seekable input size. In this
path the native 7-Zip console reports counters such as `3M`, `7M`, and `11M`.
The upstream implementation computes `Completed >> 20`: each unit is 1,048,576
processed bytes. The old parser recognized percentages only and discarded those
useful counters.

The repaired parser accepts native MiB counters, including carriage-return,
backspace, and fragmented console frames. It compares the measured processed
bytes with the exact selected input length. It does not use archive growth as a
substitute for source work and does not infer bytes from a percentage callback.

| Work or state | Display and meaning |
|---|---|
| Snapshot copy, verification, selected-range copy, archive checks, publication | Measured bytes against the known work size |
| Streamed 7-Zip range | Native encoder bytes, 1 MiB resolution, percentage, recent throughput, estimated remaining work time |
| 7-Zip percentage callbacks | Native percentage and percentage-based ETA; no invented byte throughput |
| Encoder emits no usable callbacks | Explicit input-feed progress and input ETA; this describes accepted pipe bytes |
| Buffered parse | Observed physical record count against a total counted during the existing snapshot pass |
| Buffered render and Markdown write | Processed Unicode characters against the known character count |
| Normalization | Completed configured operations and current operation name |
| Exact token-count convergence | Count-pass status and processed characters; remaining pass count is unknown |
| Metadata or other work without a denominator | Activity, elapsed time, explicit unavailable ETA |
| Counted work finished but process/close/check pending | Finalizing or checking, with no premature successful 100% stage claim |

Rates use bounded samples from approximately the last 30 seconds. A changing
compression region can therefore change the ETA. Startup has an estimating
state. If no new work is observed for ten seconds, throughput/ETA are withheld
until progress resumes; a stale estimate is not repeatedly advertised as current.
The window and bounded diagnostic retention limit reporting overhead.

An ETA is a forecast from recent work, not a guarantee. Quantized counters,
buffering, disk behavior, input content, and CPU scheduling can change the
remaining duration. Finalization and full independent archive verification are
separate work. The exporter does not create an overall ETA or job percentage
from a stage count: further segments, verification stages, and retries can be
discovered while it runs.

### Terminal, plain log, and JSON output

```powershell
cdx-thread 'SESSION-ID' -ProgressStyle auto
cdx-thread 'SESSION-ID' -ProgressStyle plain
codex-export --session-id 'SESSION-ID' --mode thread --json --progress-style json 2> progress.jsonl
```

Progress is always on stderr; final paths and JSON results remain on stdout.
The same measured facts drive all renderers. Terminal output uses a compact bar
when a denominator exists; redirected text remains sparse. Color/Unicode and
terminal control behavior retain their existing capability checks.

The JSON event format retains `completed`, `total`, `unit`, `percent`,
`rate_per_second`, and `eta_seconds`. It adds:

| Field | Meaning |
|---|---|
| `progress_basis` | Work source: measured work, native encoder bytes, native percent, labeled input-feed fallback, or verified member bytes |
| `progress_resolution` | Counter granularity; native `M` counters use 1,048,576 bytes |
| `phase` | Working/compressing, finalizing, checking, or terminal state |
| `eta_status` | Warming up, estimated, unknown total, waiting for progress, finalizing, complete, failed, or retry |
| `eta_scope` | Current measured stage work or explicitly limited input feed |
| `work_percent` | Fraction of counted work; may reach 100 while stage completion is still pending |
| `observations` | Bounded auxiliary counters such as input bytes accepted, known input total, input completion, and observed archive bytes |

A running stage may have `work_percent=100` and `percent=null` during finalization.
Only successful stage completion receives the completed 100% state. A recovered
size-budget attempt remains a retry under the v0.5.1 outcome policy. Encoder,
source-generator, validation, publication, and cancellation failures remain
visible failures, including a source-generator error discovered after subprocess
stdout has closed.

## Narrow source-gap policy

A line containing only NUL bytes followed by LF or CRLF cannot be parsed as JSON.
Version 0.5.1 full-thread streaming could preserve its native bytes and record a
parse error, while exact turn indexing stopped at the same line. Version 0.5.2
recognizes only that narrow, newline-terminated shape as an opaque source gap.

This policy has five guarantees:

1. A warning states that the gap's origin and any missing historical content are
   unknown. No content is inferred or reconstructed.
2. The index records the original line, byte start/end, total bytes, NUL count,
   SHA-256, and the snapshot hash to which those coordinates refer.
3. If a selected native interval includes the gap, its bytes are copied exactly.
   An excerpt does not silently remove an included gap.
4. Known gaps remain disclosed in selection provenance even when outside the
   chosen range; inclusion and coordinate scope are distinguished. Nested
   excerpts preserve that audit trail.
5. Mixed NUL/text, invalid UTF-8, non-object records, and malformed JSON that
   cannot be handled by the existing narrow Windows-path escape repair remain
   errors. This exception does not permit arbitrary damaged records.

An unterminated NUL tail remains subject to the unfinished-tail policy because a
live writer may still be appending it. Turn numbers count observed complete human
user records; they are not a claim that unknown missing historical records have
been recovered. The live rollout is not edited, truncated, or repaired in place.

A full-stream export may still disclose the gap as a parse error in its integrity
audit while retaining every native byte. Archive CRC/SHA success establishes byte
integrity, not JSON syntax validity. Neither result identifies which application,
backup process, or storage layer introduced an anomalous source line.

## Consistent selection identities

Chronological response listings now use the same native human-turn index as
response selection. A late completed-user-message mirror with a matching native
ID cannot create an extra numbered response. Recorded turn numbers remain the
selection keys even if a listing is sorted for presentation. Combining `--last N`
with response/turn selection resolves the requested relative response within
that indexed last-N human scope. A pending turn with no response reports a clear
selection error and does not publish an empty export.

Buffered parsing and action formatting now preserve native message/item/call
identities. A separate occurrence with a different explicit ID stays separate,
even if the visible text is identical. Compatible mirror records merge only under
the supported identity rules, and every native line behind a merged record is
retained in `source_seqs` for an exact native excerpt.

This fixes three consequential cases:

- Hiding actions no longer collapses distinct identical assistant messages and
  changes the message indexes established by `--list`.
- A repeated command in a later turn is retained rather than discarded by a
  global text cache.
- Parallel tool results are matched by call ID instead of FIFO order. Ambiguous
  unmatched records remain visible rather than being attached to a guessed call.

Explicit `--from-message 0` or `--to-message 0` is rejected. Omitted endpoints
retain their documented first/last defaults. Indices are one-based.

`response_item/configuration_update` is recognized as configuration metadata.
Its native bytes remain present; it is not treated as a user turn or a tool
result. Supported native command/function-output, file-change, extension, MCP-tool,
image-view, and legacy action discriminators remain recognized when a message-only
view hides actions, so `--strict-events` does not reject them merely because actions
are disabled. Recognition checks explicit schema discriminators without traversing
large hidden output bodies. An arbitrary new name containing a tool-like word is
not automatically accepted; unrecognized schemas still fail strict handling. Streaming defaults continue to preserve recorded
adjacent duplicates.

## Recorded command output and completion state

Modern command-completion records can store their console text in
`aggregated_output` rather than `output` or `stdout`. The extractor now reads
nonempty `aggregated_output`/`aggregatedOutput` strings and the recorded root
`exit_code`/`exitCode` and `status`. Explicit root metadata takes precedence over
nested transport hints. A failed or in-progress status is not labeled successful.
Console strings remain literal, including output that happens to look like JSON.

When the aggregate is present, it is the primary representation. An independently
recorded stdout or stderr stream is added, with a label, only when its complete
raw text is not already contained in that aggregate. This preserves separately
recorded text when the aggregate was capped; any overlapping text remains visible
rather than being reconstructed by a guessed diff. When no combined representation
exists, nonempty stdout and stderr are both retained with labels if both exist.
Other `output`/`result`/`content` representations retain their alternative-field
precedence rather than being concatenated indiscriminately. Empty, null, and
unsupported aggregate containers fall through to supported alternatives.

The existing `none`, `summary`, `tail`, and `full` output policies apply afterward.
This can intentionally add output and completion information to Markdown from
older captures. The original native bytes, source identities, and selection
coordinates are unchanged.

## Typed shell helpers

Upgrade shell helper files as well as the Python package. The four typed helpers
are available in both PowerShell and Bash:

```powershell
cdx-turns 'SESSION-ID'
cdx-since-text 'SESSION-ID' 'distinct phrase from the starting human prompt'
cdx-since-turn 'SESSION-ID' 42
cdx-since-id 'SESSION-ID' 'recorded-turn-or-message-id'
cdx-since-time 'SESSION-ID' '2026-11-23T09:30:00-08:00'
cdx-last 'SESSION-ID' 4
cdx-turn 'SESSION-ID' 42
```

The generic helper remains available:

```powershell
cdx-since 'SESSION-ID' 'text:distinct phrase'
cdx-since 'SESSION-ID' 'turn:42'
cdx-since 'SESSION-ID' 'id:recorded-turn-or-message-id'
cdx-since 'SESSION-ID' 'time:2026-11-23T09:30:00-08:00'
```

A bare selector `text` is a literal search for that word, not a request to prompt
for a selector type. The text helper also accepts `TEXT SESSION-UUID` when the UUID
makes the reversed order unambiguous. Prefer the documented `SESSION-ID VALUE`
order. PowerShell retains occurrence selection for ambiguous text/ID matches.
`cdx-last` still requires the number of turns and may prompt when it is omitted.

Since selection includes the selected user prompt and all later native records
through the captured boundary. It is a fresh snapshot/selection on each call,
not a persistent export cursor that changes future commands automatically.
Re-run a listing when the source has grown before relying on numeric anchors.

## Linear metadata collection in buffered modes

The full-source validation exposed another cost in the classic parser: each
observed timestamp searched the entire accumulated timestamp list before being
appended. With mostly unique timestamps, doubling the records approximately
quadrupled that metadata work. This affected source parsing before message,
range, and action filtering.

Metadata buckets now build a membership set once while retaining an ordered list
of the original values. Membership has average constant cost, and first-seen
order, normalization, duplicate handling, and JSON values remain unchanged.
The transient set is an object attribute rather than a metadata key; buckets are
converted back to ordinary lists when parsing finishes. No cache or set is added
to manifests or result JSON. The streaming renderer does not use this collector.

This removes the quadratic metadata scan. It does not claim that every buffered
mode has bounded memory or that arbitrary input processing becomes constant time.

## Resource and validation boundaries

The byte index and streaming thread/turn-range path remain bounded by index size
and per-record work. Some classic message/range/action rendering paths still
buffer selected record objects and may scan the full captured source before
filtering. Version 0.5.2 does not claim constant memory for all modes. Selected
turn intervals are narrowed before buffered rendering where supported; prefer
an explicit turn/since/last scope for very large histories when that is the
intended content.

The test suite uses synthetic source fixtures, subprocesses, independent ZIP
readback, and pinned real 7-Zip binaries. Tests cover source gaps, exact excerpts,
ambiguous/corrupt records, all selector modes, mirror identity, out-of-order tool
results, actual native-byte progress, ETA state transitions, streaming failures,
and rendering fidelity. Release CI is the authority for supported platform and
interpreter results; user-machine background processes are not recreated by a
Linux test run.

### Primary implementation references

- [7-Zip 26.06 native progress printer](https://github.com/ip7z/7zip/blob/26.06/CPP/7zip/UI/Console/PercentPrinter.cpp)
- [7-Zip-Zstandard progress printer](https://github.com/mcmilk/7-Zip-zstd/blob/master/CPP/7zip/UI/Console/PercentPrinter.cpp)
- [Python subprocess pipe and lifecycle semantics](https://docs.python.org/3.13/library/subprocess.html)
- [Python text I/O newline semantics](https://docs.python.org/3.13/library/io.html)
- [Codex native protocol item definitions](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/items.rs)
- [Codex command-completion emitter](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/events.rs)
- [Codex stream aggregation](https://github.com/openai/codex/blob/main/codex-rs/core/src/exec.rs)
- [Codex response item definitions](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)
- [PowerShell advanced parameter binding](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_functions_advanced_parameters?view=powershell-7)
