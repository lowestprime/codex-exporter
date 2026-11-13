"""Exercise default aggregation and all-part stdout through the real CLI parser."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import sys
import zipfile

import pytest

from codex_exporter import archive_pipeline, batch_archive, cli


def _zip(path, members, settings, **kwargs):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_LZMA) as archive:
        for name, source in members.items():
            archive.write(source, name)


@pytest.fixture
def setup_cli(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'nonexistent-codex')
    for module in (archive_pipeline, batch_archive):
        monkeypatch.setattr(module, 'find_archiver', lambda _: Path('synthetic-7z'))
        monkeypatch.setattr(module, '_compress_archive', _zip)

    def make_sources(entropy=0):
        sources = []
        for number in (1, 2):
            sid = f'10000000-0000-4000-8000-{number:012d}'
            source = tmp_path / f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
            body = random.Random(number).randbytes(entropy).hex() if entropy else f'Complete synthetic answer {number}'
            rows = [
                {'type': 'session_meta', 'payload': {'id': sid, 'cwd': '/synthetic/project'}},
                {'type': 'event_msg', 'timestamp': '2026-01-01T00:00:00Z',
                 'payload': {'type': 'user_message', 'message': f'Synthetic prompt {number}'}},
                {'type': 'event_msg', 'timestamp': '2026-01-01T00:00:01Z',
                 'payload': {'type': 'agent_message', 'message': body}},
            ]
            source.write_bytes(b''.join((json.dumps(row) + '\n').encode() for row in rows))
            sources.append(source)
        return sources

    return make_sources


def invoke(monkeypatch, capsys, sources, output, *extra):
    argv = ['codex-export', '--mode', 'thread', '--fast', '--tokenizer', 'regex',
            '--no-progress', '--no-remember-out-dir', '--out-dir', str(output)]
    for source in sources:
        argv.extend(['--jsonl', str(source)])
    argv.extend(extra)
    monkeypatch.setattr(sys, 'argv', argv)
    assert cli.main() == 0
    return capsys.readouterr().out


def test_cli_default_batch_prints_one_zip_with_seven_members(tmp_path, monkeypatch, capsys, setup_cli):
    sources = setup_cli()
    hashes = {source: hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}
    output = tmp_path / 'exports'
    stdout = invoke(monkeypatch, capsys, sources, output)
    paths = [Path(line) for line in stdout.splitlines()]
    assert len(paths) == 1 and paths[0].is_file()
    assert list(output.iterdir()) == paths
    with zipfile.ZipFile(paths[0]) as archive:
        assert len(archive.namelist()) == 7
        assert 'BATCH_INDEX.json' in archive.namelist()
        index = json.loads(archive.read('BATCH_INDEX.json'))
        assert len(index['sessions']) == 2
        native_hashes = {hashlib.sha256(archive.read(entry['native_jsonl'])).hexdigest()
                         for entry in index['sessions']}
        assert native_hashes == set(hashes.values())
    assert all(hashlib.sha256(source.read_bytes()).hexdigest() == expected for source, expected in hashes.items())


def test_cli_explicit_separate_archives_preserves_two_triplets(tmp_path, monkeypatch, capsys, setup_cli):
    sources = setup_cli()
    stdout = invoke(monkeypatch, capsys, sources, tmp_path / 'separate', '--separate-archives')
    paths = [Path(line) for line in stdout.splitlines()]
    assert len(paths) == 2 and len(set(paths)) == 2
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            assert len(archive.namelist()) == 3
            assert 'BATCH_INDEX.json' not in archive.namelist()
    assert all(source.exists() for source in sources)


def test_cli_small_cap_prints_every_independent_part(tmp_path, monkeypatch, capsys, setup_cli):
    sources = setup_cli(entropy=40_000)
    output = tmp_path / 'bounded'
    stdout = invoke(monkeypatch, capsys, sources, output, '--archive-max-bytes', '32768')
    paths = [Path(line) for line in stdout.splitlines()]
    assert len(paths) > 1 and len(set(paths)) == len(paths)
    assert set(output.iterdir()) == set(paths)
    part_numbers = set()
    export_ids = set()
    for path in paths:
        assert path.stat().st_size <= 32_768
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            metadata = json.loads(archive.read('ARCHIVE_PART.json'))
            assert metadata['parts'] == len(paths)
            assert metadata['max_archive_bytes'] == 32_768
            part_numbers.add(metadata['part'])
            export_ids.add(metadata['export_id'])
    assert part_numbers == set(range(1, len(paths) + 1)) and len(export_ids) == 1
    assert all(source.exists() for source in sources)
