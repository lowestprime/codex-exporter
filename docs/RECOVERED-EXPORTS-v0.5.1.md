# Recovered compression attempts and accurate export outcomes

## The v0.5.0 reporting defect

A large member can exceed its estimated compressed-byte budget. The exporter then
reduces the source range and retries. In v0.5.0, the rejected attempt remained a
failed progress stage even after all replacement ranges, complete archive-set
verification, publication and cleanup succeeded. A successful export could
therefore end with `Export failed | 25/26 stages`.

Version 0.5.1 keeps the attempt visible as `stage_retry`. The final JSON summary
contains `stages_completed`, `stages_retried`, `stages_failed`, `stages_settled`
and `stages_total`. A human summary reports settled stages and an explicit retry
count. Genuine compressor, CRC/SHA, source-consistency, publication, cleanup or
cancellation errors still produce failure. Unhandled budget overflows still fail
the overall operation.

The same handling covers both streamed ranges and a speculative ordinary ZIP
that must fall back to bounded parts. Size and ZIP-member checks after encoder
finalization remain inside the compression stage, so a finalization overflow
cannot be silently recorded as a completed stage.

## Compression planning across heterogeneous data

An early 256 KiB sample can be a poor estimate of a multi-gigabyte transcript.
Later portions may contain different proportions of text, repeated data and
encoded payloads. The new planner uses bounded samples distributed across the
whole member, including the head and tail, with at most sixteen 256 KiB probes
per member. Small members use a conservative estimate without spawning probes.
All probes for one member appear as a single visible progress stage.

Each sampled region contributes to a contiguous range estimate. A completed
range then calibrates subsequent estimates against its actual compressed size.
This avoids repeatedly assuming that every later region behaves like the header.
The JSON result includes `archive_compression_planning` with sample count, sample
bytes and estimated size; unsampled members report zero samples and null measured
ratios.

These are planning estimates. They are not guarantees of a compression ratio,
maximum speed or a globally minimum number of archives. Independent small
samples can miss long-distance repetition, and abrupt changes between samples
can still require a retry. Every final archive remains subject to its exact byte
ceiling and complete decoding, CRC, SHA-256 and contiguous reconstruction checks.
Compressed member streams are reused during assembly, and the existing bounded
exact packing search still operates on measured completed pieces.

LZMA, level 9, the 256 MiB maximum dictionary, word size 64 and nineteen requested
threads remain the defaults. Explicitly saved compression preferences are
preserved. The implementation does not substitute another codec or add default
review files. A fitting export still uses one ordinary ZIP with original names.

## Live listing and export use the same EOF policy

JSON Lines recommends a final newline but does not require one. Export snapshots
already retained a complete final record without a newline, including the
exporter's narrow legacy JSON repairs. Read-only turn listing previously used a
stricter check at that same boundary and could hide a complete repairable final
user prompt.

Both paths now share a completeness check. Supported repairable final records
remain visible and selectable; unfinished JSON and partial UTF-8 tails remain
deferred. This changes the interpretation of a captured boundary, never the
recorded bytes. Repairs remain visible in the audit metadata. Scalar JSON retains
the indexer's existing structural error rather than silently becoming a turn.

## Completion and process cleanup

The CLI records final summary detail without emitting success early. The reporter
finalizes only after required output handling and the resource stack have closed.
A late cleanup or output exception therefore cannot follow an already-issued
success summary.

If deleting a rejected compression candidate also fails, the original compression
error is preserved with the cleanup error chained to it. Cleanup failure is an
explicit failed stage and prevents another size retry. The exporter does not
silently ignore locks or delete unrelated files.

Reader and writer thread startup now occurs inside the compressor's cleanup
scope. If either thread cannot start, the child is terminated and reaped, only
started threads are joined, and its pipes are closed. Relative source and
candidate paths are resolved before a changed child working directory is used.

## Continuing to export a running thread

Use the same command:

```powershell
cdx-thread 'SESSION-ID'
```

Capture reads the initial open handle's fixed-size prefix, copies it to private
staging, independently hashes the same prefix again, and checks source identity
and shrinkage. Normal later appends are deferred to the next export. Rendering
and packaging use the captured bytes. This is a verified prefix capture, not an
atomic filesystem snapshot of arbitrary concurrent rewrites.

The existence of a backup client is not evidence of a sharing violation. A real
Windows sharing violation requires incompatible open-handle access/share modes.
If the log reports successful capture, complete archive verification and cleanup,
a recovered planning attempt must not be presented as proof that the backup
client damaged the export.

The original rollout remains unchanged. No stopping Codex, disabling backups,
changing source permissions or purging `.codex` data is required by this fix.
`--work-dir` remains available when a different volume is useful for capacity.

## Existing behavior retained

The regression suite covers the seven v0.5.0 requests: matching archive/member
stems, bounded packing and encoded-stream reuse, multi-column listing sorts,
complete effective-default help, source timestamps, terminal-height keyboard
paging, and identical versioned help for bare aliases. Live source snapshots,
selected native excerpts, source-copy options, collision protection and output
permissions remain covered.

## Recorded validation

The implementation in commit `25420a78311b9c475f15686a153fb4d019b86449`
passed [all seven CI jobs](https://github.com/lowestprime/codex-exporter/actions/runs/43788909075):
Python 3.10 and 3.13 on Windows, Linux and macOS, plus distribution packaging.
Each Windows job passed 635 tests with six platform/optional skips; each Linux
and macOS job passed 636 tests with five skips. The local integrated run passed
628 tests with thirteen platform/optional skips. Canonical and bundled modules
passed synchronization checks.

The [reproducible synthetic benchmark](BENCHMARK-v0.5.1.md) includes every run,
source identity, setting, timing range and a complete reproduction harness. Both
chosen heterogeneous-data cases retained two parts, eliminated rejected
candidates, preserved exact source bytes and corrected the final success status.
One case added 34 ZIP bytes and the other removed 58 bytes. Timings came from a
shared Linux runner and are not a general performance guarantee.

## Primary technical references

- [Python zipfile](https://docs.python.org/3.13/library/zipfile.html): supported
  methods, ZIP64 and CRC-reading behavior.
- [Python tempfile](https://docs.python.org/3.13/library/tempfile.html): temporary
  directory cleanup and Windows open-handle restrictions.
- [Microsoft CreateFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea):
  compatible sharing modes and `ERROR_SHARING_VIOLATION`.
- [JSON Lines](https://jsonlines.org/): UTF-8 records and optional final newline.

Synthetic regressions and operating-system CI establish the tested behaviors;
they do not claim a reproduction of a particular third-party backup client on a
user's Windows computer or universal decoding support in every receiving chat.
