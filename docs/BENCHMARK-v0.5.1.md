# v0.5.1 synthetic compression-planning benchmark

This benchmark compares v0.5.0 with the implementation in commit `25420a78311b9c475f15686a153fb4d019b86449` (PR #12). It uses deterministic synthetic files only. The complete settings, package inventories, source hashes, every compression attempt, all 12 measured runs and summary metrics are in [benchmark-v0.5.1.json](benchmark-v0.5.1.json).

## Result

The candidate eliminated rejected compression attempts in both selected cases and correctly reported success for all valid outputs. Version 0.5.0 produced valid, exactly reconstructable archives but reported failure after recovered planning overflows. Both versions produced two parts in every run.

| Scenario | Version | Median pipeline time | Range across three runs | Total ZIP bytes | Parts | Rejected attempts per run | Final progress |
|---|---|---:|---:|---:|---:|---:|---|
| misleading_prefix | v0.5.0 | 1.0675 s | 1.0464–1.1562 s | 4,255,074 | 2 | 3 | `export_failed` |
| misleading_prefix | Candidate | 0.5762 s | 0.5576–0.7655 s | 4,255,108 | 2 | 0 | `export_complete` |
| alternating_entropy | v0.5.0 | 2.1120 s | 0.9408–3.1468 s | 4,255,132 | 2 | 2 | `export_failed` |
| alternating_entropy | Candidate | 0.6597 s | 0.5808–1.5235 s | 4,255,074 | 2 | 0 | `export_complete` |

The candidate adds **34 bytes** in the first case and removes **58 bytes** in the second. It does not improve every size metric. The stable result across repetitions is fewer rejected candidates, accurate final reporting, identical reconstructed bytes, the same part count, and compliance with the configured cap.

Elapsed times are descriptive measurements from a shared runner with another compression workload active. Contention increased during the second scenario, and the reported ranges show substantial variation. These timings do not establish a general speedup or predict Windows performance.

### Every measured pipeline time

| Scenario | Version | Repetition 1 | Repetition 2 | Repetition 3 |
|---|---|---:|---:|---:|
| misleading_prefix | v050 | 1.156246 s | 1.046364 s | 1.067525 s |
| misleading_prefix | candidate | 0.576247 s | 0.765469 s | 0.557587 s |
| alternating_entropy | v050 | 0.940808 s | 2.112036 s | 3.146805 s |
| alternating_entropy | candidate | 0.659716 s | 0.580779 s | 1.523453 s |

## Fixed settings and workload model

| Setting | Value |
|---|---|
| Format / codec | ZIP / LZMA |
| 7-Zip version | 26.06 |
| Compression level | 9 |
| Maximum dictionary | 256 MiB (`256m`) |
| Word size | 64 |
| Requested threads | 19 |
| Final archive cap | 2,500,000 bytes |
| Source size | 8,388,608 bytes per scenario |
| Repetitions | Three per revision per scenario |
| Runtime | CPython 3.12.14, Linux x86_64 |
| Processor reported by runner | INTEL(R) XEON(R) PLATINUM 8573C |

Both revisions use the same pre-existing dictionary policy: streamed input ranges shorter than the configured maximum may use a smaller effective dictionary. No codec setting was reduced to obtain the timing results.

The seeded random generator is `random.Random(20261115)`. Draw 4 MiB, then 2 MiB, then 2 MiB in that order. Construct the cases as follows:

- `misleading_prefix`: 2 MiB of repeated `A`, the 4 MiB random draw, then 2 MiB of repeated `B`.
- `alternating_entropy`: 2 MiB of repeated `C`, the first 2 MiB random draw, 2 MiB of repeated `D`, then the second 2 MiB random draw. The final region becomes difficult to compress again.

The candidate samples four bounded regions for each of these files. v0.5.0 samples only the prefix, incorrectly attempts one whole archive, then rejects two range attempts in the first case or one in the second. The candidate performs two accepted range compressions with no rejected attempt in either case.

## Verification and scope

All 12 runs passed complete ZIP-member CRC decoding, declared segment SHA-256 checks, gap-free and overlap-free reconstruction, comparison with the original source size and SHA-256, unchanged source-byte checks, and final archive-cap checks. Payload members use ZIP method 14 (LZMA); part metadata uses Store.

| Source | SHA-256 |
|---|---|
| `misleading_prefix` | `ead24480fd55c7d90254f8ea7e53f5bcd83ab55a541c8f7e7f99c6f4c1a12d28` |
| `alternating_entropy` | `8b7d23bd7d18c080ea18afb3904c54b3c786d98cb5b366a7dd45e00834860664` |

Timing covers `_create_archive_parts`, including its normal internal verification and progress finalization. A second independent reconstruction pass is recorded separately and excluded from pipeline time. A successful function return with verified archives is distinguished from the terminal reporter’s final event; that distinction exposes v0.5.0’s false failure.

These two scenarios test heterogeneous content with misleading local samples. They do not measure every data distribution. Additional samples may add overhead on uniform data. The planner remains an estimate; actual output-size enforcement and complete verification provide the correctness guarantee. Neither arbitrary compression nor global minimum archive count is proved optimal by this benchmark.

## Exact source identity

- Baseline commit: `abb1fe0585131055f81d89dba77b0e5f2d16164d`.
- Candidate commit: `25420a78311b9c475f15686a153fb4d019b86449`.
- Baseline Python-package inventory SHA-256: `3ea0bbd54932019c61f8f702feeeb1074bfaacf020d6ebcb950d07ba1f5236f2`.
- Candidate Python-package inventory SHA-256: `59e7dcde331d753be2749d41cca2efd8f15ee6891b413792444354cfdb31f263`.
- Tested 7-Zip executable SHA-256: `f94066e93f9a807547a3bff1021b3ec79d5ea4f57dfead018e9698182ad283b5`.

An inventory is the sorted list of `[repository-relative Python filename, file SHA-256]` rows under `codex_exporter`, serialized with `json.dumps(rows, separators=(",", ":"))` and hashed as UTF-8. The complete inventories are included in the JSON data. The executable digest identifies the tested Linux binary; binaries for other platforms have different hashes.

## Reproduce

Use separate worktrees so neither checkout changes while it is measured. From a clone containing both revisions:

```bash
git worktree add --detach ../codex-exporter-v050 abb1fe0585131055f81d89dba77b0e5f2d16164d
git worktree add --detach ../codex-exporter-v051 25420a78311b9c475f15686a153fb4d019b86449
```

Install the appropriate official 7-Zip 26.06 CLI for the host platform. Save the following complete harness as `benchmark_planning.py`, then run it with Python 3.10 or later. The destination must not already exist; the harness retains per-run archives, progress streams, metrics, fixture hashes and package inventories for review.

```bash
python benchmark_planning.py --baseline ../codex-exporter-v050 --candidate ../codex-exporter-v051 --archiver /path/to/7zz --output planning-benchmark-output
```

The harness deliberately imports each revision in a separate Python process and uses the same alternating order as the reported run. Its extra integrity pass is outside the timed pipeline. Reproduction must compare hashes and status behavior first; elapsed time and adaptive boundaries can vary across hosts.

```python
"""Deterministic, synthetic, two-revision archive planning benchmark."""
from pathlib import Path
import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
import zipfile

SETTINGS = dict(method='LZMA', level=9, dictionary='256m', word_size=64,
                threads=19, backend='7zip', max_bytes=2_500_000)
EXPECTED = {
    'misleading_prefix': 'ead24480fd55c7d90254f8ea7e53f5bcd83ab55a541c8f7e7f99c6f4c1a12d28',
    'alternating_entropy': '8b7d23bd7d18c080ea18afb3904c54b3c786d98cb5b366a7dd45e00834860664',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(checkout):
    return [[str(path.relative_to(checkout)).replace('\\', '/'), digest(path.read_bytes())]
            for path in sorted((checkout / 'codex_exporter').rglob('*.py'))]


def worker(args):
    sys.path.insert(0, str(args.checkout.resolve()))
    from codex_exporter import archive_pipeline, archive_stream
    from codex_exporter.archive_pipeline import ArchiveSettings
    from codex_exporter.progress import ProgressReporter

    source = args.output / 'sources' / (args.scenario + '.bin')
    source_size = source.stat().st_size
    source_hash = digest(source.read_bytes())
    assert source_hash == EXPECTED[args.scenario]
    work = args.output / 'runs' / f'{args.scenario}-{args.worker}-{args.repetition}'
    work.mkdir(parents=True, exist_ok=False)
    result = dict(version=args.worker, scenario=args.scenario, repetition=args.repetition,
                  source_bytes=source_size, source_sha256=source_hash, requested_settings=SETTINGS,
                  range_calls=0, probe_calls=0, whole_calls=0,
                  rejected_range_attempts=0, rejected_whole_attempts=0, calls=[])
    original_range = archive_stream.compress_range
    original_whole = archive_pipeline._compress_archive

    def ranged(destination, name, src, offset, count, limit, **kwargs):
        probe = kwargs.get('stage_label', '').startswith('Sample')
        result['probe_calls' if probe else 'range_calls'] += 1
        call = dict(kind='sample' if probe else 'range', offset=offset,
                    requested_bytes=count, budget=limit)
        started = time.perf_counter()
        try:
            compressed = original_range(destination, name, src, offset, count, limit, **kwargs)
            call.update(status='completed', consumed_bytes=compressed.size_bytes,
                        archive_bytes=destination.stat().st_size)
            return compressed
        except archive_stream.RangeTooLarge as error:
            result['rejected_range_attempts'] += 1
            call.update(status='rejected_budget', consumed_bytes=error.input_bytes,
                        output_bytes=error.output_bytes)
            raise
        finally:
            call['seconds'] = time.perf_counter() - started
            result['calls'].append(call)

    def whole(*values, **kwargs):
        result['whole_calls'] += 1
        started = time.perf_counter()
        call = dict(kind='whole')
        try:
            value = original_whole(*values, **kwargs)
            call['status'] = 'completed'
            return value
        except archive_stream.RangeTooLarge:
            result['rejected_whole_attempts'] += 1
            call['status'] = 'rejected_budget'
            raise
        finally:
            call['seconds'] = time.perf_counter() - started
            result['calls'].append(call)

    archive_stream.compress_range = ranged
    archive_pipeline._compress_archive = whole
    progress_path = work / 'progress.jsonl'
    started = time.perf_counter()
    with progress_path.open('w', encoding='utf-8') as log:
        with ProgressReporter(style='json', stream=log) as reporter:
            parts, _, strategy = archive_pipeline._create_archive_parts(
                {source.name: source}, ArchiveSettings(**SETTINGS), work=work,
                destination=work.parent / 'synthetic_export.zip', backend='7zip',
                archiver=args.archiver, reporter=reporter,
                expected_checksums={source.name: (source_hash, source_size)})
    result.update(pipeline_seconds=time.perf_counter() - started, part_count=len(parts),
                  total_archive_bytes=sum(part.size_bytes for part in parts),
                  max_part_bytes=max(part.size_bytes for part in parts), strategy=strategy,
                  under_cap=all(part.size_bytes <= SETTINGS['max_bytes'] for part in parts))
    started = time.perf_counter()
    pieces = []
    compressed_payload = 0
    for part in parts:
        with zipfile.ZipFile(part.path) as archive:
            compressed_payload += sum(info.compress_size for info in archive.infolist())
            if 'ARCHIVE_PART.json' in archive.namelist():
                index = json.loads(archive.read('ARCHIVE_PART.json'))
                for item in index['members']:
                    raw = archive.read(item['member'])  # Complete decoding checks CRC.
                    assert len(raw) == item['bytes'] and digest(raw) == item['sha256']
                    pieces.append((item['offset'], raw))
            else:
                assert archive.namelist() == [source.name]
                pieces.append((0, archive.read(source.name)))
    offset = 0
    reconstructed = hashlib.sha256()
    for start, raw in sorted(pieces):
        assert start == offset
        reconstructed.update(raw)
        offset += len(raw)
    assert offset == source_size and reconstructed.hexdigest() == source_hash
    assert digest(source.read_bytes()) == source_hash and result['under_cap']
    result.update(crc_verified=True, sha256_verified=True, no_gaps_or_overlaps=True,
                  source_unchanged=True, reconstructed_sha256=reconstructed.hexdigest(),
                  compressed_payload_bytes=compressed_payload,
                  reconstruction_seconds=time.perf_counter() - started)
    events = [json.loads(line) for line in progress_path.read_text().splitlines()]
    result['final_progress'] = next(event for event in reversed(events)
                                    if event['event'] in ('export_complete', 'export_failed'))
    result['rejected_attempts'] = result['rejected_range_attempts'] + result['rejected_whole_attempts']
    result['stage_failed_count'] = sum(event['event'] == 'stage_failed' for event in events)
    result['stage_retry_count'] = sum(event['event'] == 'stage_retry' for event in events)
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--archiver', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('planning-benchmark-output'))
    parser.add_argument('--worker', choices=('v050', 'candidate'))
    parser.add_argument('--checkout', type=Path)
    parser.add_argument('--scenario', choices=tuple(EXPECTED))
    parser.add_argument('--repetition', type=int)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.archiver = args.archiver.resolve()
    if args.worker:
        worker(args)
        return
    if not args.baseline or not args.candidate:
        parser.error('--baseline and --candidate are required')
    args.output.mkdir(exist_ok=False)  # Preserve any prior benchmark.
    sources = args.output / 'sources'
    sources.mkdir()
    mib = 1024 * 1024
    rng = random.Random(20261115)
    noise1, noise2, noise3 = (rng.randbytes(n * mib) for n in (4, 2, 2))
    payloads = {
        'misleading_prefix': b'A' * (2 * mib) + noise1 + b'B' * (2 * mib),
        'alternating_entropy': b'C' * (2 * mib) + noise2 + b'D' * (2 * mib) + noise3,
    }
    for scenario, raw in payloads.items():
        assert digest(raw) == EXPECTED[scenario]
        (sources / (scenario + '.bin')).write_bytes(raw)
    checkouts = {'v050': args.baseline.resolve(), 'candidate': args.candidate.resolve()}
    results = []
    for scenario in EXPECTED:
        for repetition in range(1, 4):
            order = ('v050', 'candidate') if repetition % 2 else ('candidate', 'v050')
            for version in order:
                subprocess.run([
                    sys.executable, str(Path(__file__).resolve()), '--worker', version,
                    '--checkout', str(checkouts[version]), '--scenario', scenario,
                    '--repetition', str(repetition), '--archiver', str(args.archiver),
                    '--output', str(args.output)], check=True)
                result = args.output / 'runs' / f'{scenario}-{version}-{repetition}' / 'result.json'
                results.append(json.loads(result.read_text()))
    inventories = {version: inventory(path) for version, path in checkouts.items()}
    record = dict(settings=SETTINGS, runs=results, package_inventories=inventories,
                  package_inventory_sha256={version: digest(json.dumps(items, separators=(',', ':')).encode())
                                            for version, items in inventories.items()})
    (args.output / 'results.json').write_text(json.dumps(record, indent=2) + '\n')
    print(args.output / 'results.json')


if __name__ == '__main__':
    main()
```
