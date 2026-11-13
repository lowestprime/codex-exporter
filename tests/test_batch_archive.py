"""Aggregate archives preserve session boundaries, navigation and cleanup safety."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import zipfile

import pytest

from codex_exporter import batch_archive
from codex_exporter.archive_pipeline import ArchiveSettings


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def compress(path, members, settings, **kwargs):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_LZMA) as archive:
        for name, source in members.items():
            archive.write(source, name)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(batch_archive, 'find_archiver', lambda _: Path('synthetic-7z'))
    monkeypatch.setattr(batch_archive, '_compress_archive', compress)


def staged(tmp_path, name, *, entropy=0):
    output = tmp_path / 'exports'
    output.mkdir(exist_ok=True)
    live_dir = tmp_path / 'live'
    live_dir.mkdir(exist_ok=True)
    randomizer = random.Random(name)
    content = randomizer.randbytes(entropy).hex() if entropy else 'Complete synthetic transcript.'
    source = live_dir / f'{name}.jsonl'
    native = (json.dumps({'type': 'user', 'text': content}) + '\n').encode()
    source.write_bytes(native)
    markdown = output / f'{name}.md'
    markdown.write_bytes(b'# Synthetic transcript\n' + content.encode() + b'\n')
    source_copy = output / f'{name}.jsonl'
    source_copy.write_bytes(native)
    manifest = output / f'{name}.manifest.json'
    manifest.write_text(json.dumps({'source_sha256': digest(native), 'schema_version': 1}))
    return {'session_id': 'synthetic-' + name, 'title': name, 'mode': 'thread',
            'source_jsonl': str(source), 'source_sha256': digest(native),
            'file': str(markdown), 'manifest': str(manifest),
            'source_copy': str(source_copy), 'source_copy_sha256': digest(native),
            'source_copy_bytes': len(native)}


def original_payloads(items):
    return {Path(item[key]).name: Path(item[key]).read_bytes()
            for item in items for key in ('file', 'source_copy') if item.get(key)}


def reconstruct(paths):
    pieces = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            index = json.loads(archive.read('ARCHIVE_PART.json'))
            for entry in index['members']:
                raw = archive.read(entry['member'])
                assert len(raw) == entry['bytes'] and digest(raw) == entry['sha256']
                pieces.setdefault(entry['original'], []).append((entry['offset'], raw))
    result = {}
    for name, entries in pieces.items():
        offset = 0
        output = bytearray()
        for start, raw in sorted(entries):
            assert start == offset
            output.extend(raw)
            offset += len(raw)
        result[name] = bytes(output)
    return result


def test_two_small_sessions_make_one_zip_with_two_triplets(tmp_path, backend):
    items = [staged(tmp_path, 'one'), staged(tmp_path, 'two')]
    expected = original_payloads(items)
    live = {item['source_jsonl']: Path(item['source_jsonl']).read_bytes() for item in items}
    result = batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip', ArchiveSettings())
    assert len(result['archives']) == 1 and result['archive_verified']
    assert result['loose_files_removed']
    with zipfile.ZipFile(result['archive']) as archive:
        assert len(archive.namelist()) == 7
        assert set(archive.namelist()) == {'BATCH_INDEX.json', 'one.md', 'one.jsonl', 'one.manifest.json',
                                        'two.md', 'two.jsonl', 'two.manifest.json'}
        for name, raw in expected.items():
            assert archive.read(name) == raw
        index = json.loads(archive.read('BATCH_INDEX.json'))
        assert [entry['session_id'] for entry in index['sessions']] == ['synthetic-one', 'synthetic-two']
        for entry in index['sessions']:
            plan = json.loads(archive.read(entry['manifest']))['archive_plan']
            assert plan['native_source_member'] == entry['native_jsonl']
    assert all(item['file'] is None and item['source_copy'] is None for item in items)
    assert all(Path(path).read_bytes() == raw for path, raw in live.items())
    assert list((tmp_path / 'exports').iterdir()) == [Path(result['archive'])]


def test_batch_cap_covers_all_sessions_without_dropping_bytes(tmp_path, backend):
    items = [staged(tmp_path, 'one', entropy=55_000), staged(tmp_path, 'two', entropy=50_000)]
    expected = original_payloads(items)
    result = batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip',
                                         ArchiveSettings(max_bytes=32_768))
    assert len(result['archives']) > 1
    assert all(Path(path).stat().st_size <= 32_768 for path in result['archives'])
    restored = reconstruct(result['archives'])
    for name, raw in expected.items():
        assert restored[name] == raw
    assert len(json.loads(restored['BATCH_INDEX.json'])['sessions']) == 2
    assert not (tmp_path / 'exports' / 'batch.zip').exists()
    assert all(Path(item['source_jsonl']).exists() for item in items)


def test_failed_batch_verification_keeps_all_loose_files(tmp_path, backend, monkeypatch):
    items = [staged(tmp_path, 'one'), staged(tmp_path, 'two')]
    paths = [Path(item[key]) for item in items for key in ('file', 'manifest', 'source_copy')]

    def corrupt(path, members, settings, **kwargs):
        with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_LZMA) as archive:
            for number, (name, source) in enumerate(members.items()):
                archive.writestr(name, b'wrong bytes' if number == 0 else source.read_bytes())

    monkeypatch.setattr(batch_archive, '_compress_archive', corrupt)
    with pytest.raises(RuntimeError, match='content mismatch'):
        batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip', ArchiveSettings())
    assert all(path.exists() for path in paths)
    assert not list((tmp_path / 'exports').glob('*.zip'))


def test_batch_rejects_modified_staged_native_copy(tmp_path, backend):
    items = [staged(tmp_path, 'one'), staged(tmp_path, 'two')]
    changed = Path(items[0]['source_copy'])
    changed.write_bytes(b'{"text":"changed since the snapshot"}\n')
    with pytest.raises(RuntimeError, match='(?i)(source|snapshot|staged|native)'):
        batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip', ArchiveSettings())
    assert changed.exists()
    assert not list((tmp_path / 'exports').glob('*.zip'))
    assert all(Path(item['source_jsonl']).exists() for item in items)


def test_batch_review_navigation_and_body_hashes_are_consistent(tmp_path, backend):
    items = [staged(tmp_path, 'one', entropy=5000), staged(tmp_path, 'two', entropy=5000)]
    result = batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip',
                                         ArchiveSettings(review=True, review_chunk_bytes=4_096))
    with zipfile.ZipFile(result['archive']) as archive:
        batch = json.loads(archive.read('BATCH_INDEX.json'))
        for entry in batch['sessions']:
            index_name = entry['review_index']
            index = json.loads(archive.read(index_name))
            assert index['native_source_member'] == entry['native_jsonl']
            assert digest(archive.read(entry['manifest'])) == index['manifest']['sha256']
            body = bytearray()
            for part in index['parts']:
                raw = archive.read(part['filename'])
                assert digest(raw) == part['sha256']
                assert index_name.encode() in raw[:part['header_bytes']]
                value = raw[part['header_bytes']:]
                assert digest(value) == part['body_sha256']
                body.extend(value)
            assert bytes(body) == archive.read(entry['markdown'])
            assert digest(bytes(body)) == index['markdown']['sha256']
            recent = archive.read(index['recent']['filename'])
            assert digest(recent) == index['recent']['sha256']
            assert index_name.encode() in recent[:index['recent']['header_bytes']]
            assert entry['review_start'].encode() in recent[:index['recent']['header_bytes']]
            start = archive.read(entry['review_start'])
            assert index_name.encode() in start and index['recent']['filename'].encode() in start


def test_unknown_sidecar_is_kept_but_never_mislabeled_native(tmp_path, backend):
    items = [staged(tmp_path, 'one'), staged(tmp_path, 'two')]
    for item in items:
        Path(item.pop('source_copy')).unlink()
        item.pop('source_copy_sha256')
        item.pop('source_copy_bytes')
        sidecar = Path(item['file']).with_suffix('.unknown.jsonl')
        sidecar.write_text('{"type":"new_schema","data":"retain me"}\n')
        item['unknown_sidecar'] = str(sidecar)
    result = batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip',
                                         ArchiveSettings(copy_source=False, review=True))
    with zipfile.ZipFile(result['archive']) as archive:
        batch = json.loads(archive.read('BATCH_INDEX.json'))
        for entry in batch['sessions']:
            assert entry['native_jsonl'] is None
            index = json.loads(archive.read(entry['review_index']))
            assert index['native_source_member'] is None
            assert Path(entry['markdown']).with_suffix('.unknown.jsonl').name in archive.namelist()


def test_batch_rejects_any_generated_member_aliasing_live_source(tmp_path, backend):
    items = [staged(tmp_path, 'one'), staged(tmp_path, 'two')]
    live = Path(items[0]['source_jsonl'])
    before = live.read_bytes()
    items[0]['file'] = str(live)
    # Remove the staged native member's basename collision so the source guard
    # itself, rather than duplicate-name detection, must protect the live file.
    items[0]['source_copy'] = None
    with pytest.raises((ValueError, RuntimeError), match='(?i)(source|alias|input|protect|same)'):
        batch_archive.package_batch(items, tmp_path / 'exports' / 'batch.zip', ArchiveSettings())
    assert live.read_bytes() == before
    assert not list((tmp_path / 'exports').glob('*.zip'))
