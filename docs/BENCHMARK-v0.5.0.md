# Codex Exporter v0.5.0 synthetic packaging benchmark

## Result

The updated packaging path reduced the median time of the overflowing synthetic workload by 39.6% while retaining four independently readable ZIPs and every source/Markdown byte. The final ZIP set grew by 522 bytes (0.00203%), including changed provenance. The CPU-heavy ordinary one-ZIP control remained close to baseline at about 2.7 seconds. These measurements support an overflow-specific improvement, not a claim that every export is faster or smaller.

## Controlled conditions

- Baseline: repository commit `afe93d32efce93ae42081b2f47447d47a5fbcc60` (v0.4.0), independently extracted with `git archive`.
- Updated: the finalized v0.5.0 working tree with 256 KiB ratio probes. Module SHA-256 values are recorded below and were identical across all nine final comparison runs.
- Archiver: official 7-Zip 26.06 x64, built 2026-11-06; Linux environment with nine visible/affine CPUs. Binary SHA-256: `f94066e93f9a807547a3bff1021b3ec79d5ea4f57dfead018e9698182ad283b5`.
- Identical requested compression: ZIP/LZMA, level 9, dictionary 256m, word size 64, threads 19. No Deflate or payload-codec downgrade was used.
- Both workloads contain 1,024 synthetic records: a 16,906,154-byte native JSONL and a 16,802,769-byte Markdown file, totaling 33,708,923 bytes (32.1473 MiB), plus a small generated manifest.
- Repetitive data uses repeated harmless generated text. Mixed data uses deterministic random bytes, seed 54921, encoded as base64. No attachment, user prompt, project, correspondence, or other private source content enters the fixtures.
- The overflow/repetitive cap is 8,388,608 bytes (8 MiB). A separate 31,457,280-byte cap (30 MiB) makes the mixed workload fit one archive while its raw size still exercises the probe-based decision.
- Each reported baseline and final case ran three times, sequentially. Fixture creation, initial Markdown copying, and extra post-run verification are outside packaging time.
- Packaging timings include the implementation's own full verification, publication, and cleanup.

## Timings and output

| Case | Baseline median (range), s | Updated median (range), s | Change | ZIP count | Final bytes, baseline → updated |
|---|---:|---:|---:|---:|---:|
| Repetitive, ordinary ZIP | 0.3658 (0.2827–0.3850) | 0.3063 (0.2319–0.3437) | -16.3% | 1 → 1 | 11,274 → 11,336 |
| Mixed base64, multipart | 6.2905 (6.2324–6.7972) | 3.7986 (3.7036–3.8903) | -39.6% | 4 → 4 | 25,749,534 → 25,750,056 |
| Mixed base64, ordinary ZIP | 2.7566 (2.7334–2.7578) | 2.6849 (2.6543–2.7116) | -2.6% | 1 → 1 | 25,747,638 → 25,747,699 |

Negative time change means faster. The repetitive case is subsecond, with overlapping run ranges; its small median difference should not be treated as a reliable performance change. The CPU-heavy ordinary case retained its original whole-archive multi-file compression path and a similar runtime despite the two small real-codec ratio probes. Three repeats do not establish universal performance.

## Disk, memory, and invocation measurements

| Case | Observed allocated work peak, baseline | Observed allocated work peak, updated | Compression launches | Maximum child RSS range, baseline → updated |
|---|---:|---:|---:|---:|
| Repetitive, ordinary ZIP | 33,730,560–33,730,560 B | 33,730,560–33,730,560 B | 1 → 3 | 459.88–460.01 → 459.94–460.04 MiB |
| Mixed base64, multipart | 110,977,024–110,977,024 B | 62,091,264–66,809,856 B | 5 → 7 | 474.47–475.49 → 138.64–138.69 MiB |
| Mixed base64, ordinary ZIP | 59,469,824–59,469,824 B | 59,469,824–59,469,824 B | 1 → 3 | 475.45–475.51 → 473.57–475.44 MiB |

The overflow case's sampled allocated-work high-water decreased from 110,977,024 bytes to 62,091,264–66,809,856 bytes, a 39.8%–44.1% reduction. The updated seven compression launches are two small ratio probes and five final payload members. The baseline five launches include the initial complete ZIP and its oversized-member segmentation passes. Launch count alone does not measure work: eliminating repeated full/overflowing compression produces the time reduction.

Disk sampling runs every 20 ms, sums `st_blocks × 512`, and counts each `(device, inode)` once. This includes snapshot, rendered output, compression candidates, assembled ZIPs, and publication files inside the run directory. It excludes immutable fixture originals outside that directory. Hard links are not counted as duplicate allocations. These are observed high-water marks, not exact continuous peaks or disk-space reservations.

Child RSS uses Linux `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` in KiB and reports the largest child-process high-water, not aggregate simultaneous Python-plus-child memory. Baseline POSIX source publication uses hard links; this benchmark therefore does not quantify the additional Windows-only savings from removing the old copied native snapshot.

## Fidelity, cap, and compression checks

Every final comparison run passed independent decoding of every ZIP member with Python `zipfile`, which enforces CRC checks, followed by independent SHA-256 and byte-count verification. Multipart members were ordered by source offsets; the verifier rejected any gap or overlap and required each original file's exact reconstructed SHA-256. JSONL and Markdown matched their immutable original fixtures byte for byte. Manifests were valid JSON and retained the requested LZMA/9/256m/64/19 settings and the exact per-case byte cap.

Every ZIP was within its configured cap. Ordinary results contained precisely the original three associated members. Multipart results contained the original payload coverage plus reconstruction metadata. Summed encoded payload bytes require four parts under the 8 MiB cap in the measured multipart case; four were produced. This lower-bound statement applies to these already encoded payloads, not all conceivable codecs, source segmentation choices, or arbitrary future inputs.

Effective LZMA dictionary properties were read from ZIP member payload headers. The updated multipart outputs used 4,096, 6,291,456, and 11,534,336 bytes; the ordinary outputs used 17,825,792 bytes. The requested 256m setting remains the maximum. Capping it to a known shorter input reproduces the external archiver's existing input-size reduction and avoids an unnecessary 256 MiB dictionary for tiny or short stdin members.

## Issues caught before the final measurement

The first prototype was slower on the overflow case (9.519 seconds, nine launches). It repeatedly retried the first large members because bytes fed into the archiver pipe were an inaccurate proxy for bytes already encoded. It also put a 256 MiB dictionary into every stdin member. The final implementation added real-codec ratio probes, retained the whole-archive path for a predicted easy fit, reused observed ratios for later ranges, bounded speculative whole-archive output, and capped effective dictionaries to known input length. The initial prototype measurements were excluded from the final comparison table.

A separate near-cap test exposed avoidable splitting: an ordinary LZMA ZIP of 15,333 bytes fit a 16,384-byte cap, while unconditional multipart metadata reservation produced two parts. The implementation now first checks whether complete members fit an ordinary ZIP and only resegments a member when multipart overhead actually requires it. A long-name collision extension was also corrected to retain the filename byte bound.

## Limits on interpretation

This is a targeted synthetic packaging benchmark, not a Windows 11 system benchmark, a multi-gigabyte source rendering benchmark, or a universal proof of minimum archive count. Three repeats do not establish statistical confidence intervals. Runs were sequential without cache eviction; background host activity and cache warmth can affect timings. The exact final artifact size includes a few bytes of changed provenance, and the package retains stronger checks instead of promising zero overhead. A format successfully decoded here is not proof that an arbitrary ChatGPT conversation backend has materialized an upload or supports that codec.

## Measurement method and fixture identity

The fixture generator used Python's `random.Random(54921).randbytes(12 * 1024)` for each mixed record, then ASCII base64 encoding. For repetitive records, it repeated the harmless sentence `Repeated synthetic command, deterministic result, and a preserved tool output. ` and sliced the resulting string to 16,384 characters. Each workload contains 1,024 records. The JSONL records use compact JSON separators and an explicit newline; the Markdown places each identical payload under a numbered synthetic turn heading.

The benchmark prepared the Markdown and manifest, captured the JSONL using `snapshot_source`, and timed `package_export` with the requested archive settings. A separate verifier then decoded the outputs and reconstructed their source offsets. This measures packaging and its own verification/publication/cleanup; rendering, tokenization, terminal browsing, and attachment materialization are outside the measurement.

| Fixture | Bytes | SHA-256 |
|---|---:|---|
| Repetitive JSONL | 16,906,154 | `bf1396cac9ea10a48db536e1107c8b4824a46b72b68263ae2eff3864b6a15c41` |
| Repetitive Markdown | 16,802,769 | `5fc7a985bd43853a5360f2852f92068014192602e48687eb3b23f81d69f6fa0b` |
| Mixed JSONL | 16,906,154 | `7367911a28522c0c4c571a5f26b52f82f27ca63d027398227890a9c255288bdd` |
| Mixed Markdown | 16,802,769 | `e9ee80a2180cd9a2cbc65c621b424a85653f3567c6af2c796b9815d3671db150` |

### Final implementation module SHA-256

| Module | SHA-256 |
|---|---|
| `archive_pipeline.py` | `6df8f0de40438d3462cb1b524792b06ad2fdc0f3330cfba9df8b9952550c5853` |
| `bounded_archive.py` | `4e0bcd1891aa8e0bf3c3115793534e5414103f073f9e813fa392e6519924b97f` |
| `progress.py` | `b1377225849f8a828f6e72e92dc8386a8f448ee6eba8526dc17a20a56e148267` |
| `snapshot.py` | `342ec574b56a4eccd9f53a2bce8127372db24fb66fb01d112a0b752c25557cd5` |
| `file_access.py` | `1d0534c41bfc4f36fca943b0ec4f90fa60b52f0f063dc691ec996eee024f3569` |
| `archive_stream.py` | `ff42b3d273595c9b908f80b25151e2e33829a23fa33e7473703c58432240f45d` |

## Final comparison scope

The main tables use three fresh repeats of the final **256 KiB** probe setting. Earlier prototype and 1 MiB-probe measurements were kept separate and were not pooled into these statistics. The package modules listed above were unchanged across all nine final comparison runs. The measured gains should be attributed to this implementation and these workloads, with the limits stated above.
