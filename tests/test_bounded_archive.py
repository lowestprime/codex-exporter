"""Adversarial size/coverage/publication checks using small, real LZMA ZIPs."""
from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path
import random
import struct
import zipfile

import pytest

from codex_exporter import archive_pipeline, bounded_archive, review
from codex_exporter.archive_pipeline import ArchiveSettings
from codex_exporter.bounded_archive import (
    MAX_ARCHIVE_BYTES, MIN_ARCHIVE_BYTES, archive_destinations, bound_archive, publish_parts,
)


def compress(path, members):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_LZMA) as archive:
        for name, source in members.items():
            archive.write(source, name)


def prepare(tmp_path, payloads):
    members = {}
    for name, content in payloads.items():
        source = tmp_path / name
        source.write_bytes(content)
        members[name] = source
    initial = tmp_path / 'initial.zip'
    compress(initial, members)
    return initial, members


def rebuild(parts):
    pieces = {}
    for part in parts:
        with zipfile.ZipFile(part.path) as archive:
            index = json.loads(archive.read('ARCHIVE_PART.json'))
            assert index['parts'] == len(parts)
            for entry in index['members']:
                data = archive.read(entry['member'])
                assert hashlib.sha256(data).hexdigest() == entry['sha256']
                pieces.setdefault(entry['original'], []).append((entry['offset'], data))
    result = {}
    for name, entries in pieces.items():
        offset = 0
        output = bytearray()
        for start, data in sorted(entries):
            assert start == offset
            output.extend(data)
            offset += len(data)
        result[name] = bytes(output)
    return result


def compressed_stream(path, name):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(name)
    with path.open('rb') as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        name_length, extra_length = struct.unpack_from('<HH', header, 26)
        stream.seek(name_length + extra_length, 1)
        return stream.read(info.compress_size)


def test_fitting_triplet_is_unchanged_and_never_recompressed(tmp_path):
    payloads = {'sample.md': b'# Sample\n' * 100,
                'sample.manifest.json': b'{"schema":1}\n',
                'sample.jsonl': b'{"text":"example"}\n' * 100}
    initial, members = prepare(tmp_path, payloads)
    before = initial.read_bytes()

    def forbidden(*args):
        raise AssertionError('A fitting ZIP must not be recompressed')

    parts = bound_archive(initial, members, max_bytes=MIN_ARCHIVE_BYTES,
                          work=tmp_path / 'parts', compress=forbidden, method='LZMA')
    assert len(parts) == 1 and parts[0].path == initial
    assert parts[0].provenance is None
    assert parts[0].members == sorted(payloads)
    assert initial.read_bytes() == before


def test_fitting_compressed_members_are_reused_bit_for_bit(tmp_path):
    randomizer = random.Random(17)
    payloads = {f'data-{i}.bin': randomizer.randbytes(7000) for i in range(3)}
    initial, members = prepare(tmp_path, payloads)

    def forbidden(*args):
        raise AssertionError('Individually fitting compressed members must be reused')

    parts = bound_archive(initial, members, max_bytes=MIN_ARCHIVE_BYTES,
                          work=tmp_path / 'parts', compress=forbidden, method='LZMA')
    assert len(parts) == 2
    assert rebuild(parts) == payloads
    for part in parts:
        assert part.size_bytes <= MIN_ARCHIVE_BYTES
        for piece in part.provenance['members']:
            assert piece['kind'] == 'complete_file'
            assert compressed_stream(part.path, piece['member']) == compressed_stream(initial, piece['original'])


def test_record_aligned_jsonl_parts_are_independently_parseable(tmp_path):
    randomizer = random.Random(23)
    rows = [{'turn': number, 'body': randomizer.randbytes(500).hex()} for number in range(160)]
    raw = b''.join((json.dumps(row) + '\n').encode() for row in rows)
    initial, members = prepare(tmp_path, {'rollout.jsonl': raw})
    parts = bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                          compress=compress, method='LZMA')
    assert len(parts) >= 2
    assert rebuild(parts) == {'rollout.jsonl': raw}
    seen = []
    for part in parts:
        assert part.size_bytes <= 32_768
        with zipfile.ZipFile(part.path) as archive:
            for piece in part.provenance['members']:
                assert piece['kind'] == 'line_segment'
                assert piece['member'].endswith('.jsonl')
                seen.extend(json.loads(line) for line in archive.read(piece['member']).splitlines())
    assert sorted(seen, key=lambda row: row['turn']) == rows


def test_single_giant_record_is_lossless_explicit_byte_fragments(tmp_path):
    randomizer = random.Random(31)
    raw = ('{"text":"漢字🙂' + randomizer.randbytes(180_000).hex() + '"}\n').encode()
    initial, members = prepare(tmp_path, {'rollout.jsonl': raw})
    parts = bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                          compress=compress, method='LZMA')
    assert len(parts) >= 2
    assert rebuild(parts) == {'rollout.jsonl': raw}
    for part in parts:
        assert part.size_bytes <= 32_768
        for piece in part.provenance['members']:
            assert piece['kind'] == 'byte_fragment'
            assert piece['member'].endswith('.bytes')
    assert json.loads(rebuild(parts)['rollout.jsonl'])['text'].startswith('漢字🙂')


def test_mixed_compressibility_adapts_to_observed_output(tmp_path):
    randomizer = random.Random(37)
    raw = b'{"a":"' + b'x' * 300_000 + randomizer.randbytes(180_000).hex().encode() + b'"}\n'
    initial, members = prepare(tmp_path, {'rollout.jsonl': raw})
    calls = []

    def counted(path, inputs):
        calls.append(sum(source.stat().st_size for source in inputs.values()))
        compress(path, inputs)

    parts = bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                          compress=counted, method='LZMA')
    assert max(part.size_bytes for part in parts) <= 32_768
    assert rebuild(parts)['rollout.jsonl'] == raw
    assert calls


@pytest.mark.parametrize('value', [True, 0, MIN_ARCHIVE_BYTES - 1, MAX_ARCHIVE_BYTES + 1,
                                   512_000_000.0, '512000000'])
def test_archive_cap_cannot_be_disabled_or_exceed_upload_ceiling(value):
    with pytest.raises(ValueError, match='max_bytes'):
        ArchiveSettings(max_bytes=value).validate()


def test_partial_publication_rolls_back_only_our_own_parts(tmp_path):
    randomizer = random.Random(41)
    initial, members = prepare(tmp_path, {'data.bin': randomizer.randbytes(70_000)})
    parts = bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                          compress=compress, method='LZMA')
    output = tmp_path / 'output.zip'
    destinations = archive_destinations(output, len(parts))
    from codex_exporter.file_access import publish_file
    count = 0

    def racing_publisher(source, target, *, reporter=None):
        nonlocal count
        count += 1
        if count == 2:
            target.write_bytes(b'created by another process')
            raise FileExistsError(str(target))
        return publish_file(source, target)

    with pytest.raises(FileExistsError):
        publish_parts(parts, output, max_bytes=32_768, publisher=racing_publisher)
    assert not destinations[0].exists()
    assert destinations[1].read_bytes() == b'created by another process'
    assert all(path.exists() for path in members.values())


def test_mutation_while_segmenting_prevents_any_success(tmp_path):
    randomizer = random.Random(43)
    original = randomizer.randbytes(80_000)
    initial, members = prepare(tmp_path, {'data.bin': original})
    checksums = {'data.bin': (hashlib.sha256(original).hexdigest(), len(original))}
    members['data.bin'].write_bytes(randomizer.randbytes(len(original)))
    with pytest.raises(RuntimeError, match='reconstruction mismatch'):
        bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                      compress=compress, method='LZMA', checksums=checksums)
    assert not list(tmp_path.glob('*.part*-of-*.zip'))
    assert members['data.bin'].exists()


def test_corrupt_part_is_detected_before_publication(tmp_path, monkeypatch):
    randomizer = random.Random(47)
    initial, members = prepare(tmp_path, {'data.bin': randomizer.randbytes(70_000)})
    original = bounded_archive._assemble

    def corrupt(path, pieces, index_name, metadata):
        original(path, pieces, index_name, metadata)
        if metadata['part'] == 2:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo(pieces[0].name)
            with path.open('r+b') as file:
                file.seek(info.header_offset)
                header = file.read(30)
                name_len, extra_len = struct.unpack_from('<HH', header, 26)
                offset = info.header_offset + 30 + name_len + extra_len + info.compress_size // 2
                file.seek(offset)
                value = file.read(1)
                file.seek(offset)
                file.write(bytes([value[0] ^ 0xff]))

    monkeypatch.setattr(bounded_archive, '_assemble', corrupt)
    with pytest.raises((RuntimeError, zipfile.BadZipFile, EOFError, lzma.LZMAError)):
        bound_archive(initial, members, max_bytes=32_768, work=tmp_path / 'parts',
                      compress=compress, method='LZMA')
    assert not list(tmp_path.glob('*.part*-of-*.zip'))
    assert members['data.bin'].exists()


def test_complete_pipeline_cleans_only_after_all_parts_verified(tmp_path, monkeypatch):
    randomizer = random.Random(53)
    source = tmp_path / 'live.jsonl'
    source.write_bytes(b'{"text":"' + randomizer.randbytes(70_000).hex().encode() + b'"}\n')
    markdown = tmp_path / 'export.md'
    markdown.write_text('# transcript\n' + randomizer.randbytes(40_000).hex())
    manifest = tmp_path / 'export.manifest.json'
    manifest.write_text('{"schema_version":1}\n')
    expected = {source.name: source.read_bytes(), markdown.name: markdown.read_bytes()}
    monkeypatch.setattr(archive_pipeline, 'find_archiver', lambda _: Path('synthetic-7z'))
    monkeypatch.setattr(archive_pipeline, '_compress_archive',
                        lambda path, inputs, settings, **kwargs: compress(path, inputs))
    result = archive_pipeline.package_export(
        {'file': str(markdown), 'manifest': str(manifest),
         'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}, source,
        ArchiveSettings(max_bytes=32_768))
    assert result['archive_count'] >= 2 and result['archive_verified']
    assert all(Path(path).stat().st_size <= 32_768 for path in result['archives'])
    assert source.read_bytes() == expected[source.name]
    assert not markdown.exists() and not manifest.exists()
    assert not (tmp_path / 'export.jsonl').exists()
    assert result['loose_files_removed']
    assert not list(tmp_path.glob('*.review*'))


def test_explicit_review_archives_also_obey_the_cap(tmp_path):
    randomizer = random.Random(59)
    markdown = tmp_path / 'review-input.md'
    markdown.write_text('# transcript\n' + '\n'.join(randomizer.randbytes(500).hex() for _ in range(200)))
    result = review.create_review_bundle(markdown, chunk_bytes=4_096, max_bytes=32_768)
    assert len(result['review_archives']) >= 2
    assert all(Path(path).stat().st_size <= 32_768 for path in result['review_archives'])
    assert Path(result['review_directory']).is_dir()
    assert markdown.exists()


def test_repacked_unicode_zip64_headers_preserve_payloads(tmp_path, monkeypatch):
    # A small threshold exercises genuine ZIP64 fields without a multi-GB test.
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 1_024)
    randomizer = random.Random(61)
    payloads = {f'données-{i}.bin': randomizer.randbytes(7000) for i in range(3)}
    initial, members = prepare(tmp_path, payloads)
    parts = bound_archive(initial, members, max_bytes=MIN_ARCHIVE_BYTES,
                          work=tmp_path / 'parts', compress=compress, method='LZMA')
    assert len(parts) >= 2
    assert rebuild(parts) == payloads
    assert all(part.size_bytes <= MIN_ARCHIVE_BYTES for part in parts)


def test_data_descriptors_are_repacked_to_self_contained_headers(tmp_path):
    import io
    class Unseekable(io.BytesIO):
        def seek(self, *args):
            raise io.UnsupportedOperation('streaming output')
    randomizer = random.Random(67)
    payloads = {f'data-{i}.bin': randomizer.randbytes(7000) for i in range(3)}
    initial, members = prepare(tmp_path, payloads)
    stream = Unseekable()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_LZMA) as archive:
        for name, raw in payloads.items():
            archive.writestr(name, raw)
    initial.write_bytes(stream.getvalue())
    with zipfile.ZipFile(initial) as archive:
        assert all(info.flag_bits & 8 for info in archive.infolist())
    parts = bound_archive(initial, members, max_bytes=MIN_ARCHIVE_BYTES,
                          work=tmp_path / 'parts', compress=compress, method='LZMA')
    assert rebuild(parts) == payloads
    for part in parts:
        with zipfile.ZipFile(part.path) as archive:
            assert not any(info.flag_bits & 8 for info in archive.infolist())


@pytest.mark.parametrize('stem', ['x' * 237, '漢' * 80])
def test_long_multipart_names_are_portable_and_collision_aware(tmp_path, stem):
    markdown = tmp_path / (stem + '.md')
    base = markdown.with_suffix('.zip')
    destinations = archive_destinations(base, 2)
    assert len(set(destinations)) == 2
    assert all(len(path.name.encode('utf-8')) <= 255 for path in destinations)
    assert all(path.name.endswith(f'.part{number:04d}-of-0002.zip')
               for number, path in enumerate(destinations, 1))
    assert all(bounded_archive.archive_stem_digest(base) in path.name for path in destinations)
    destinations[0].write_bytes(b'existing verified archive')
    renamed = archive_pipeline.avoid_existing_archive(markdown, 'rename')
    assert renamed != markdown
    assert archive_destinations(renamed.with_suffix('.zip'), 2)[0] != destinations[0]
    assert destinations[0].read_bytes() == b'existing verified archive'
    with pytest.raises(FileExistsError):
        archive_pipeline.avoid_existing_archive(markdown, 'error')
