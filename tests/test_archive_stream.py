"""Real ranged compression and byte-complete bounded assembly regressions."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import zipfile

import pytest

from codex_exporter import archive_pipeline, archive_stream, bounded_archive, progress, storage
from codex_exporter.archive_pipeline import ArchiveSettings
from codex_exporter.archive_stream import CompressedRange, RangeTooLarge, compress_range
from codex_exporter.bounded_archive import build_bounded_archives, publish_parts, segment_member_name


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _rebuild(paths):
    chunks = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            if 'ARCHIVE_PART.json' not in archive.namelist():
                return {name: archive.read(name) for name in archive.namelist()}
            index = json.loads(archive.read('ARCHIVE_PART.json'))
            for entry in index['members']:
                raw = archive.read(entry['member'])
                assert _digest(raw) == entry['sha256']
                chunks.setdefault(entry['original'], []).append((entry['offset'], raw))
    rebuilt = {}
    for name, values in chunks.items():
        offset = 0
        data = bytearray()
        for start, raw in sorted(values):
            assert start == offset
            data.extend(raw)
            offset += len(raw)
        rebuilt[name] = bytes(data)
    return rebuilt


def test_python_range_streams_exact_slice_without_raw_temp_file(tmp_path):
    raw = random.Random(101).randbytes(150_000)
    source = tmp_path / 'original.bin'
    source.write_bytes(raw)
    output = tmp_path / 'slice.zip'
    result = compress_range(output, 'source.segment000001.bin.bytes', source, 1_234, 60_000,
                            80_000, settings=ArchiveSettings(method='Deflate', backend='python'),
                            backend='python')
    expected = raw[1_234:61_234]
    assert (result.size_bytes, result.sha256) == (len(expected), _digest(expected))
    with zipfile.ZipFile(output) as archive:
        assert archive.read('source.segment000001.bin.bytes') == expected
    assert {path.name for path in tmp_path.iterdir()} == {'original.bin', 'slice.zip'}


def test_over_budget_candidate_is_removed_and_original_unchanged(tmp_path):
    raw = random.Random(103).randbytes(150_000)
    source = tmp_path / 'original.bin'
    source.write_bytes(raw)
    output = tmp_path / 'failed.zip'
    with pytest.raises(RangeTooLarge):
        compress_range(output, 'original.bin', source, 0, len(raw), 20_000,
                       settings=ArchiveSettings(method='Deflate', backend='python'), backend='python')
    assert not output.exists()
    assert source.read_bytes() == raw


def test_large_compressible_input_keeps_one_original_member(tmp_path, fake_range_compressor):
    source = tmp_path / 'codex_exact_thread_SAT_11142026_055000_PM-PST_12345678.jsonl'
    raw = b'{"content":"same complete record"}\n' * 30_000
    source.write_bytes(raw)
    work = tmp_path / 'work'
    parts = build_bounded_archives({source.name: source}, work=work, basename='export.zip',
        max_bytes=32_768, method='LZMA', compress_range=fake_range_compressor)
    assert len(parts) == 1 and parts[0].provenance is None
    assert parts[0].members == [source.name]
    assert _rebuild([part.path for part in parts]) == {source.name: raw}
    assert sorted(path.name for path in work.iterdir()) == ['export.zip']


def test_streamed_segmentation_never_uses_copy_range_and_names_original_stem(
        tmp_path, monkeypatch, fake_range_compressor):
    source = tmp_path / 'codex_exact_thread_SAT_11142026_055000_PM-PST_12345678.jsonl'
    rng = random.Random(107)
    raw = b''.join((json.dumps({'i': i, 'text': rng.randbytes(100).hex()}) + '\n').encode()
                   for i in range(700))
    source.write_bytes(raw)
    monkeypatch.setattr(bounded_archive, '_copy_range',
                        lambda *args: pytest.fail('Raw segment staging regressed'))
    work = tmp_path / 'private'
    parts = build_bounded_archives({source.name: source}, work=work, basename=source.with_suffix('.zip').name,
        max_bytes=32_768, method='LZMA', compress_range=fake_range_compressor)
    assert len(parts) > 1
    assert all(part.size_bytes <= 32_768 for part in parts)
    assert all(part.path.name.startswith(source.stem + '.part') for part in parts)
    for part in parts:
        for entry in part.provenance['members']:
            assert entry['member'].startswith(source.stem + '.segment')
            assert entry['member'].endswith('.jsonl')
            assert entry['starts_at_line_boundary'] and entry['ends_at_line_boundary']
    assert _rebuild([part.path for part in parts]) == {source.name: raw}
    assert set(work.iterdir()) == {part.path for part in parts}


def test_streamed_giant_record_and_entropy_change_reconstruct_exactly(tmp_path, fake_range_compressor):
    raw = ('{"text":"漢🙂' + 'same' * 100_000
           + random.Random(109).randbytes(180_000).hex() + '"}\n').encode()
    source = tmp_path / 'codex_long.jsonl'
    source.write_bytes(raw)
    parts = build_bounded_archives({source.name: source}, work=tmp_path / 'private', basename='codex_long.zip',
        max_bytes=32_768, method='LZMA', compress_range=fake_range_compressor)
    assert len(parts) > 1 and max(part.size_bytes for part in parts) <= 32_768
    assert any(entry['kind'] == 'byte_fragment' and entry['member'].endswith('.bytes')
               for part in parts for entry in part.provenance['members'])
    assert _rebuild([part.path for part in parts])[source.name] == raw


def test_streamed_original_hash_mismatch_prevents_publication(tmp_path, fake_range_compressor):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'something changed' * 100_000)
    with pytest.raises(RuntimeError, match='mismatch'):
        build_bounded_archives({source.name: source}, work=tmp_path / 'private', basename='output.zip',
            max_bytes=32_768, method='LZMA', compress_range=fake_range_compressor,
            checksums={source.name: (_digest(b'x' * source.stat().st_size), source.stat().st_size)})
    assert source.exists() and not (tmp_path / 'output.zip').exists()


def test_consuming_publication_bounds_staging_and_rolls_back_own_parts(tmp_path, fake_range_compressor):
    source = tmp_path / 'source.bin'
    source.write_bytes(random.Random(113).randbytes(90_000))
    parts = build_bounded_archives({source.name: source}, work=tmp_path / 'private', basename='export.zip',
        max_bytes=32_768, method='LZMA', compress_range=fake_range_compressor)
    staged_paths = [part.path for part in parts]
    from codex_exporter.file_access import publish_file
    calls = []

    def publisher(staged, target, **kwargs):
        calls.append(target)
        if len(calls) == 2:
            assert not staged_paths[0].exists()
            raise OSError(errno.ENOSPC, 'Synthetic full publication volume')
        return publish_file(staged, target)

    with pytest.raises(OSError):
        publish_parts(parts, tmp_path / 'export.zip', max_bytes=32_768,
                      publisher=publisher, consume=True)
    assert all(not path.exists() for path in calls)
    assert source.exists()


def test_archive_only_borrows_snapshot_without_loose_jsonl(tmp_path, monkeypatch):
    from codex_exporter.snapshot import snapshot_source
    raw = b'{"type":"event_msg","payload":{"type":"user_message","message":"hello"}}\n'
    source = tmp_path / 'live.jsonl'
    source.write_bytes(raw)
    private = tmp_path / 'private'
    private.mkdir()
    out = tmp_path / 'output'
    out.mkdir()
    md = private / 'codex_export.md'
    md.write_text('# hello\n')
    manifest = md.with_suffix('.manifest.json')
    manifest.write_text('{}\n')
    monkeypatch.setattr(archive_pipeline, '_stage_snapshot_copy',
                        lambda *args, **kwargs: pytest.fail('Archive-only mode created a redundant source copy'))
    with snapshot_source(source, directory=private) as snapshot:
        result = archive_pipeline.package_export(
            {'file': str(md), 'manifest': str(manifest), 'source_sha256': snapshot.sha256}, snapshot.path,
            ArchiveSettings(method='Deflate', backend='python', work_dir=str(private)),
            snapshot=snapshot, destination=out / 'codex_export.zip', private_output=True)
        assert result['source_copy_strategy'] == 'borrowed_verified_snapshot'
        assert snapshot.path.read_bytes() == raw
        assert not md.with_suffix('.jsonl').exists()
    assert source.read_bytes() == raw
    with zipfile.ZipFile(result['archive']) as archive:
        assert archive.read('codex_export.jsonl') == raw
        assert len(archive.namelist()) == 3
    assert list(out.iterdir()) == [Path(result['archive'])]


def test_runner_streams_stdin_while_draining_large_diagnostics():
    raw = random.Random(127).randbytes(400_000)
    program = ('import sys,hashlib\n'
               'sys.stderr.write("diagnostic" * 200000);sys.stderr.flush()\n'
               'data=sys.stdin.buffer.read()\n'
               'print("RESULT:" + hashlib.sha256(data).hexdigest())\n')
    result = progress.run_seven_zip([sys.executable, '-c', program],
        input_chunks=(raw[i:i + 8192] for i in range(0, len(raw), 8192)),
        timeout=10, output_limit=1024)
    assert result.returncode == 0
    assert result.stdout.endswith('RESULT:' + _digest(raw) + '\n')
    assert len(result.stdout) <= 1024


def test_runner_reaps_child_when_range_monitor_cancels(monkeypatch):
    children = []
    popen = subprocess.Popen

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(progress.subprocess, 'Popen', capture)
    def fail():
        raise RangeTooLarge(100, 110, 100)
    with pytest.raises(RangeTooLarge):
        progress.run_seven_zip([sys.executable, '-c', 'import time;time.sleep(30)'],
            input_chunks=[b'a' * 100_000], on_tick=fail, timeout=10)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed


@pytest.mark.parametrize('stem', ['x' * 230, '漢' * 79])
def test_long_segment_names_preserve_identity_and_suffix(stem):
    name = segment_member_name(stem + '.jsonl', 123, '.jsonl.bytes')
    assert len(name.encode('utf-8')) <= 255
    assert name.endswith('.segment000123.jsonl.bytes')
    assert name != segment_member_name(stem + 'y.jsonl', 123, '.jsonl.bytes')


def test_space_error_reports_chosen_volume_and_known_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: shutil._ntuple_diskusage(1000, 900, 100))
    with pytest.raises(storage.StorageSpaceError) as error:
        storage.require_space(tmp_path, 101, stage='capture snapshot', reserve_bytes=0)
    assert error.value.errno == errno.ENOSPC
    assert str(tmp_path) in str(error.value)
    assert '101' in str(error.value) and '100' in str(error.value)
    assert '--work-dir' in str(error.value)


def test_disk_full_error_annotation_preserves_nonstorage_failures(tmp_path):
    normal = ValueError('Bad input')
    assert storage.storage_failure(normal, stage='test', work_dir=tmp_path, output_dir=tmp_path) is normal
    error = storage.storage_failure(OSError(errno.ENOSPC, 'full'), stage='compress',
                                    work_dir=tmp_path / 'work', output_dir=tmp_path / 'output')
    assert isinstance(error, storage.StorageSpaceError)
    assert str(tmp_path / 'work') in str(error) and str(tmp_path / 'output') in str(error)


def test_real_seven_zip_streamed_lzma_preserves_requested_settings(tmp_path, monkeypatch):
    executable = os.environ.get('CODEX_EXPORT_TEST_7Z') or shutil.which('7zz') or shutil.which('7z')
    if not executable:
        pytest.skip('Optional integration test requires a 7-Zip CLI')
    raw = (b'{"text":"Complete streamed LZMA record"}\n' * 40_000
           + random.Random(131).randbytes(400_000))
    source = tmp_path / 'native.jsonl'
    source.write_bytes(raw)
    seen = []
    real = archive_stream.run_seven_zip
    def recorded(command, **kwargs):
        seen.append(command)
        return real(command, **kwargs)
    monkeypatch.setattr(archive_stream, 'run_seven_zip', recorded)
    settings = ArchiveSettings()
    result = compress_range(tmp_path / 'streamed.zip', 'renamed.jsonl', source, 17, len(raw) - 17,
        512_000_000, settings=settings, backend='7zip', archiver=Path(executable))
    assert (result.size_bytes, result.sha256) == (len(raw) - 17, _digest(raw[17:]))
    assert {'-mx=9', '-mm=LZMA', '-mfb=64', '-mmt=19', '-sirenamed.jsonl'} <= set(seen[0])
    assert f'-md={len(raw) - 17}b' in seen[0]
    assert settings.dictionary == '256m'  # persisted request remains the maximum
    with zipfile.ZipFile(tmp_path / 'streamed.zip') as archive:
        assert archive.getinfo('renamed.jsonl').compress_type == zipfile.ZIP_LZMA
        assert archive.read('renamed.jsonl') == raw[17:]


def test_near_cap_complete_file_uses_one_ordinary_zip(tmp_path, fake_range_compressor):
    raw = random.Random(137).randbytes(15_000)
    source = tmp_path / 'source.bin'
    source.write_bytes(raw)
    parts = build_bounded_archives({source.name: source}, work=tmp_path / 'private', basename='export.zip',
        max_bytes=16_384, method='LZMA', compress_range=fake_range_compressor)
    assert len(parts) == 1 and parts[0].provenance is None
    assert parts[0].size_bytes <= 16_384
    assert _rebuild([parts[0].path]) == {source.name: raw}


def test_near_cap_complete_member_refines_only_when_other_files_need_parts(tmp_path, fake_range_compressor):
    rng = random.Random(139)
    payloads = {'large.bin': rng.randbytes(15_000), 'other.bin': rng.randbytes(7_000)}
    members = {}
    for name, raw in payloads.items():
        path = tmp_path / name
        path.write_bytes(raw)
        members[name] = path
    parts = build_bounded_archives(members, work=tmp_path / 'private', basename='export.zip',
        max_bytes=16_384, method='LZMA', compress_range=fake_range_compressor)
    assert len(parts) >= 2 and max(part.size_bytes for part in parts) <= 16_384
    assert _rebuild([part.path for part in parts]) == payloads


def test_publication_unlink_failure_rolls_back_new_public_target(tmp_path, monkeypatch):
    staged = tmp_path / 'staged.zip'
    staged.write_bytes(b'a verified synthetic fixture')
    from codex_exporter.bounded_archive import BoundedPart
    from codex_exporter.file_access import publish_file
    unlink = Path.unlink
    def fail_staged(path, *args, **kwargs):
        if path == staged:
            raise PermissionError('Synthetic staged cleanup failure')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_staged)
    target = tmp_path / 'output.zip'
    with pytest.raises(PermissionError):
        publish_parts([BoundedPart(staged, {})], target, max_bytes=16_384,
                      publisher=publish_file, consume=True)
    assert staged.exists() and not target.exists()


def test_bounded_exact_packing_improves_ffd_counterexample(tmp_path):
    # Capacity ten: FFD makes [6,3], [5,2,2], [2]; exact packing makes
    # [6,2,2] and [5,3,2]. Account for real per-piece metadata too.
    base = {'schema': 'synthetic'}
    cap = 32_768
    index = 'ARCHIVE_PART.json'
    payload_capacity = cap - bounded_archive._planned_size(base, [], index) + 1
    unit = payload_capacity // 10
    pieces = []
    for number, value in enumerate((6, 5, 3, 2, 2, 2)):
        name = f'piece-{number}.bin'
        info = zipfile.ZipInfo(name)
        piece = bounded_archive._Piece(name, name, tmp_path / name, 0, 1, '0' * 64,
                                       1, '0' * 64, tmp_path / 'candidate.zip', info)
        overhead = bounded_archive._info_weight(info) + len(bounded_archive._json_bytes(piece.record()))
        info.compress_size = unit * value - overhead
        pieces.append(piece)
    diagnostics = {}
    groups = bounded_archive._pack_pieces(pieces, base, index, cap, diagnostics=diagnostics)
    assert diagnostics['initial_parts'] == 3
    assert len(groups) == 2
    assert diagnostics['algorithm'] == 'bounded_exact_packing'
    assert diagnostics['fixed_piece_minimum_proved']
    assert diagnostics['search_nodes'] <= 100_001


def test_real_seven_zip_bounded_streams_reconstruct_all_content(tmp_path):
    executable = os.environ.get('CODEX_EXPORT_TEST_7Z') or shutil.which('7zz') or shutil.which('7z')
    if not executable:
        pytest.skip('Optional integration test requires a 7-Zip CLI')
    raw = random.Random(149).randbytes(1_700_000)
    source = tmp_path / 'codex_complete.bin'
    source.write_bytes(raw)
    settings = ArchiveSettings(dictionary='8m', threads=2, max_bytes=512_000)
    parts = build_bounded_archives({source.name: source}, work=tmp_path / 'private',
        basename='codex_complete.zip', max_bytes=settings.max_bytes, method='LZMA',
        compress_range=lambda path, name, original, offset, count, limit: compress_range(
            path, name, original, offset, count, limit, settings=settings,
            backend='7zip', archiver=Path(executable)))
    assert len(parts) >= 4 and max(part.size_bytes for part in parts) <= settings.max_bytes
    assert _rebuild([part.path for part in parts]) == {source.name: raw}
    assert set((tmp_path / 'private').iterdir()) == {part.path for part in parts}
