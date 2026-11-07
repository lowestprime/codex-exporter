"""Default output contracts, persisted review overrides, and progress isolation."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

from codex_exporter import archive_pipeline, cli


SID = '11111111-2222-4333-8444-555555555555'


@pytest.fixture
def session(tmp_path, monkeypatch):
    source = tmp_path / f'rollout-2026-11-06T00-00-00-{SID}.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Synthetic export request'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message',
         'message': 'Complete synthetic answer with Unicode 🧠.\n' * 1000}},
    ]
    source.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'unused-codex-home')
    return source


def _invoke(monkeypatch, capsys, *arguments):
    monkeypatch.setattr(sys, 'argv', ['codex-export', *map(str, arguments)])
    assert cli.main() == 0
    return capsys.readouterr()


def _export(monkeypatch, capsys, source, output, *extra):
    captured = _invoke(monkeypatch, capsys, '--jsonl', source, '--mode', 'thread', '--fast',
                       '--tokenizer', 'regex', '--no-progress', '--json', '--name', 'Synthetic defaults',
                       '--out-dir', output, '--no-remember-out-dir', *extra)
    return json.loads(captured.out), captured


def _set_portable(monkeypatch, capsys, *extra):
    return _invoke(monkeypatch, capsys,
                   '--set-archive-default', 'backend=python',
                   '--set-archive-default', 'method=Deflate',
                   '--set-archive-default', 'level=6', *extra)


def test_factory_defaults_restore_full_lzma_controls_and_disable_review():
    settings = archive_pipeline.ArchiveSettings()
    assert asdict(settings) == {
        'archive': True, 'copy_source': True, 'cleanup': True, 'archiver': '',
        'backend': 'auto', 'method': 'LZMA', 'level': 9,
        'dictionary': '256m', 'word_size': 64, 'threads': 19,
        'review': False, 'review_chunk_bytes': 1_000_000,
    }
    assert settings.effective_backend() == '7zip'


def test_persisted_configuration_restores_exact_external_lzma_preferences(session, monkeypatch, capsys):
    expected = {
        'archive': True, 'copy_source': True, 'cleanup': True,
        'archiver': r'C:\Program Files\7-Zip-Zstandard\7z.exe',
        'backend': 'auto', 'method': 'LZMA', 'level': 9,
        'dictionary': '256m', 'word_size': 64, 'threads': 19, 'review': False,
    }
    settings = []
    for key, value in expected.items():
        raw = str(value).lower() if isinstance(value, bool) else str(value)
        settings += ['--set-archive-default', f'{key}={raw}']
    _invoke(monkeypatch, capsys, *settings)
    actual = json.loads(_invoke(monkeypatch, capsys, '--show-archive-config').out)
    assert {key: actual[key] for key in expected} == expected
    assert actual['review_chunk_bytes'] == 1_000_000


def test_persisted_review_is_embedded_and_cli_switches_override_it(session, tmp_path, monkeypatch, capsys):
    before = session.read_bytes()
    _set_portable(monkeypatch, capsys,
                  '--set-archive-default', 'review=true',
                  '--set-archive-default', 'review_chunk_bytes=8192')
    result, _ = _export(monkeypatch, capsys, session, tmp_path / 'persisted')
    archive_path = Path(result['archive'])
    assert result['review_archive'] == str(archive_path)
    assert result['review_directory'] is None
    assert list(archive_path.parent.iterdir()) == [archive_path]
    with zipfile.ZipFile(archive_path) as archive:
        index = json.loads(archive.read('review-index.json'))
        manifest = json.loads(archive.read(result['archived_manifest']))
        assert index['chunk_bytes_limit'] == 8192
        assert set(manifest['archive_plan']['members']) == set(archive.namelist())
        assert 'original-export.manifest.json' not in archive.namelist()
        rebuilt = b''.join(archive.read(part['filename'])[part['header_bytes']:]
                           for part in index['parts'])
        assert rebuilt == archive.read(result['archived_markdown'])
        assert index['manifest']['sha256'] == hashlib.sha256(archive.read(result['archived_manifest'])).hexdigest()

    ordinary, _ = _export(monkeypatch, capsys, session, tmp_path / 'disabled', '--no-review')
    with zipfile.ZipFile(ordinary['archive']) as archive:
        assert len(archive.namelist()) == 3
        assert 'review-index.json' not in archive.namelist()
    assert not ordinary.get('review_archive')
    _invoke(monkeypatch, capsys, '--set-archive-default', 'review=false')
    opted_in, _ = _export(monkeypatch, capsys, session, tmp_path / 'enabled',
                          '--review', '--review-chunk-bytes', '4096')
    with zipfile.ZipFile(opted_in['archive']) as archive:
        assert json.loads(archive.read('review-index.json'))['chunk_bytes_limit'] == 4096
    stored = json.loads(_invoke(monkeypatch, capsys, '--show-archive-config').out)
    assert stored['review'] is False and stored['review_chunk_bytes'] == 8192
    assert session.read_bytes() == before


def test_json_progress_is_separate_from_result_and_reports_completed_stages(
    session, tmp_path, monkeypatch, capsys,
):
    _set_portable(monkeypatch, capsys)
    captured = _invoke(monkeypatch, capsys, '--jsonl', session, '--mode', 'thread', '--fast',
                       '--tokenizer', 'regex', '--json', '--progress', '--progress-style', 'json',
                       '--name', 'Synthetic progress', '--out-dir', tmp_path / 'progress',
                       '--no-remember-out-dir')
    result = json.loads(captured.out)
    assert result['archive_verified'] is True
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert events and events[-1]['event'] == 'export_complete'
    completed = [event for event in events if event['event'] == 'stage_complete']
    assert len(completed) >= 5
    assert any('Compress' in event['stage'] for event in completed)
    assert any('Verify' in event['stage'] or 'verify' in event['stage'] for event in completed)
    for event in completed:
        assert event['status'] == 'complete'
        assert event['completed'] >= 0
        if event['total'] is not None:
            assert event['completed'] == event['total']
        if event['percent'] is not None:
            assert 0 <= event['percent'] <= 100
    assert list(Path(result['archive']).parent.iterdir()) == [Path(result['archive'])]
