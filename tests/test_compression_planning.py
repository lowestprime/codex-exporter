"""Observable range planning and actual complete-archive integrity regressions."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import random
import zipfile

import pytest

from codex_exporter import archive_pipeline, compression_plan
from codex_exporter.archive_pipeline import ArchiveSettings
from codex_exporter.archive_stream import compress_range
from codex_exporter.bounded_archive import build_bounded_archives
from codex_exporter.compression_plan import CompressionBand, CompressionProfile, sample_profile
from codex_exporter.progress import ProgressReporter


def test_profile_integrates_local_variance_instead_of_reusing_prefix_ratio():
    profile = CompressionProfile(1000, (CompressionBand(0, 400, .8),
                                       CompressionBand(400, 1000, .1)), 128)
    assert profile.estimate() == 380
    assert profile.estimate(200, 400) == 180
    assert profile.range_bytes(0, 200) == 250
    assert profile.range_bytes(400, 50) == 500
    assert profile.range_bytes(900, 100) == 100
    assert profile.range_bytes(1000, 100) == 0
    assert profile.range_bytes(0, 200, correction=.5) == 1000
    for invalid in [0, -1, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            profile.range_bytes(0, invalid)


def test_stratified_probe_visits_head_middle_and_tail_with_bounded_work(tmp_path):
    source = tmp_path / 'heterogeneous.bin'
    source.write_bytes(b'a' * 300_000 + random.Random(181).randbytes(600_000) + b'b' * 300_000)
    settings = ArchiveSettings(method='Deflate', backend='python')
    calls = []
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream)
    def compress(path, name, source, offset, count, limit, **kwargs):
        calls.append((offset, count))
        return compress_range(path, name, source, offset, count, limit,
                              settings=settings, backend='python', **kwargs)
    profile = sample_profile(source, source.name, work=tmp_path, number=1,
                             max_bytes=500_000, compress=compress, reporter=reporter)
    reporter.finish()
    assert calls[0][0] == 0
    assert calls[-1][0] + calls[-1][1] == source.stat().st_size
    assert len(calls) == 4
    assert sum(count for _, count in calls) <= 4 * 1024 * 1024
    assert profile.bands[0].ratio < .01
    assert profile.bands[1].ratio > .9
    assert profile.estimate() > 500_000
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events[-1]['event'] == 'export_complete'
    assert events[-1]['stages_total'] == 1  # No one-stage-per-probe terminal noise.
    assert list(tmp_path.iterdir()) == [source]


def test_profile_planned_ranges_preserve_bytes_and_two_part_minimum(tmp_path, fake_range_compressor):
    rng = random.Random(191)
    raw = b'header\n' * 7000 + rng.randbytes(205_000) + b'end\n' * 40_000
    source = tmp_path / 'varied.bin'
    source.write_bytes(raw)
    profile = CompressionProfile(len(raw), (
        CompressionBand(0, 49_000, .01),
        CompressionBand(49_000, 254_000, 1.01),
        CompressionBand(254_000, len(raw), .01)), 24_000)
    counts = []
    def compress(path, name, source, offset, count, limit):
        counts.append(count)
        return fake_range_compressor(path, name, source, offset, count, limit)
    parts = build_bounded_archives({source.name: source}, work=tmp_path / 'parts', basename='varied.zip',
        max_bytes=160_000, method='LZMA', compress_range=compress,
        compression_profiles={source.name: profile})
    assert len(parts) == 2
    assert parts[0].packing['fixed_piece_minimum_proved']
    chunks = []
    for part in parts:
        assert part.size_bytes <= 160_000
        with zipfile.ZipFile(part.path) as archive:
            assert archive.testzip() is None
            for entry in part.provenance['members']:
                data = archive.read(entry['member'])
                assert hashlib.sha256(data).hexdigest() == entry['sha256']
                chunks.append((entry['offset'], data))
    assert b''.join(data for _, data in sorted(chunks)) == raw
    assert len(counts) == 2  # Neither range had to be recompressed.


def test_whole_archive_probe_overflow_is_retried_without_poisoning_success(
        tmp_path, monkeypatch, fake_range_compressor):
    raw = random.Random(193).randbytes(190_000)
    source = tmp_path / 'member.bin'
    source.write_bytes(raw)
    work = tmp_path / 'work'
    work.mkdir()
    settings = ArchiveSettings(max_bytes=131_072)
    # Deliberately misleading samples exercise the exact whole-probe fallback.
    monkeypatch.setattr(compression_plan, 'sample_profile', lambda *a, **k:
        CompressionProfile(len(raw), (CompressionBand(0, len(raw), .01),), 16_384))
    def run(command, *, on_tick=None, **kwargs):
        destination = Path(command[-2])
        destination.write_bytes(b'oversized' * 20_000)
        on_tick()
        pytest.fail('The speculative whole candidate should exceed its budget')
    monkeypatch.setattr('codex_exporter.progress.run_seven_zip', run)
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream)
    with reporter:
        parts, _, strategy = archive_pipeline._create_archive_parts(
            {source.name: source}, settings, work=work,
            destination=tmp_path / 'export.zip', backend='7zip',
            archiver=Path('unused'), reporter=reporter)
    assert strategy == 'streamed_bounded_members'
    assert len(parts) == 2
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events[-1]['event'] == 'export_complete'
    assert events[-1]['stages_retried'] == 1
    assert events[-1]['stages_failed'] == 0
    assert not (work / 'export.zip').exists()


def test_tiny_member_receipt_never_claims_a_measurement(tmp_path):
    source = tmp_path / 'tiny.txt'
    source.write_bytes(b'tiny input')
    profile = sample_profile(source, source.name, work=tmp_path, number=1,
        max_bytes=500_000, compress=lambda *a, **k: pytest.fail('Tiny member was sampled'))
    assert profile.estimate() == pytest.approx(10.2)
    assert profile.receipt()['sample_count'] == 0
    assert profile.receipt()['sample_bytes'] == 0
    assert profile.receipt()['minimum_sample_ratio'] is None
    assert profile.receipt()['maximum_sample_ratio'] is None


def test_locked_overflow_candidate_is_not_retried_by_packer(tmp_path):
    from codex_exporter.archive_stream import RangeTooLarge
    source = tmp_path / 'source.bin'
    source.write_bytes(b'x' * 200_000)
    calls = []
    failure = RangeTooLarge(100_000, 140_000, 131_072)
    failure.cleanup_error = PermissionError('Candidate remains locked')
    def failed(*args):
        calls.append(args)
        raise failure from failure.cleanup_error
    with pytest.raises(RangeTooLarge) as error:
        build_bounded_archives({source.name: source}, work=tmp_path / 'parts', basename='output.zip',
            max_bytes=131_072, method='LZMA', compress_range=failed)
    assert error.value is failure
    assert len(calls) == 1


def test_probe_cleanup_preserves_primary_diagnostic(tmp_path, monkeypatch):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'x' * 100_000)
    primary = RuntimeError('Synthetic probe compressor failed')
    cleanup = PermissionError('Synthetic probe candidate remains locked')
    def failed(*a, **k):
        raise primary
    unlink = Path.unlink
    def locked(path, *a, **k):
        if path.name.startswith('ratio-sample-'):
            raise cleanup
        return unlink(path, *a, **k)
    monkeypatch.setattr(Path, 'unlink', locked)
    with pytest.raises(RuntimeError) as error:
        sample_profile(source, source.name, work=tmp_path, number=1,
                       max_bytes=500_000, compress=failed)
    assert error.value is primary
    assert error.value.__cause__ is cleanup
