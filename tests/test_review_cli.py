"""CLI review companions respect rendering scope and work without native tools."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

from codex_exporter import archive_pipeline, cli, review


SID = '11111111-2222-4333-8444-555555555555'
OLD_PROMPT = 'HISTORICAL_USER_MARKER'
OLD_ANSWER = 'HISTORICAL_ASSISTANT_MARKER'
NEW_ANSWER = 'LATEST_ASSISTANT_MARKER'
SECRET = 'synthetic-secret-value-for-review-test'


@pytest.fixture
def synthetic_source(tmp_path: Path, monkeypatch) -> Path:
    source = tmp_path / f'rollout-2026-11-06T00-00-00-{SID}.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': OLD_PROMPT}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': OLD_ANSWER}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Latest synthetic prompt'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message',
         'message': f'{NEW_ANSWER}: password={SECRET}'}},
    ]
    source.write_bytes(b''.join((json.dumps(row) + '\n').encode('utf-8') for row in rows))
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'unavailable-codex-home')
    return source


def _invoke(monkeypatch, capsys, args: list[str]) -> dict:
    monkeypatch.setattr(sys, 'argv', ['codex-export', *args, '--json'])
    assert cli.main() == 0
    return json.loads(capsys.readouterr().out)


def _export(monkeypatch, capsys, source: Path, output: Path, *,
            mode: str = 'thread', fast: bool = False, extra: tuple[str, ...] = ()) -> dict:
    return _invoke(monkeypatch, capsys, [
        '--jsonl', str(source), '--mode', mode, '--fast' if fast else '--no-fast',
        '--no-progress', '--tokenizer', 'regex', '--name', 'Synthetic review',
        '--out-dir', str(output), '--no-remember-out-dir', '--review-chunk-bytes', '8192',
        '--archive-backend', 'python', *extra,
    ])


def _read_review(result: dict) -> tuple[dict, bytes]:
    directory = Path(result['review_directory'])
    index = json.loads(Path(result['review_index']).read_text(encoding='utf-8'))
    bodies = [(directory / part['filename']).read_bytes()[part['header_bytes']:]
              for part in index['parts']]
    reconstructed = b''.join(bodies)
    assert hashlib.sha256(reconstructed).hexdigest() == index['markdown']['sha256']
    assert len(reconstructed) == index['markdown']['bytes']
    assert result['review_markdown_sha256'] == index['markdown']['sha256']
    with zipfile.ZipFile(result['review_archive']) as archive:
        for part in index['parts']:
            assert archive.read(part['filename']) == (directory / part['filename']).read_bytes()
    return index, reconstructed


@pytest.mark.parametrize('fast', [False, True], ids=['buffered', 'streamed'])
def test_redacted_review_matches_archival_markdown_without_native_leak(
    synthetic_source: Path, tmp_path: Path, monkeypatch, capsys, fast: bool,
):
    before = synthetic_source.read_bytes()
    result = _export(monkeypatch, capsys, synthetic_source, tmp_path / 'export', fast=fast,
                     extra=('--redact', '--no-source-copy'))
    index, reconstructed = _read_review(result)
    assert index['redacted'] is True
    assert result['redacted'] is True
    assert SECRET.encode() not in reconstructed
    assert b'password=[REDACTED]' in reconstructed
    assert NEW_ANSWER.encode() in reconstructed
    assert index['source_sha256'] == hashlib.sha256(before).hexdigest()
    with zipfile.ZipFile(result['archive']) as archive:
        assert not any(name.endswith('.jsonl') for name in archive.namelist())
        assert archive.read(result['archived_markdown']) == reconstructed
        manifest_raw = archive.read(result['archived_manifest'])
    review_manifest = Path(result['review_directory']) / 'original-export.manifest.json'
    assert review_manifest.read_bytes() == manifest_raw
    assert index['original_manifest_sha256'] == hashlib.sha256(manifest_raw).hexdigest()
    assert json.loads(manifest_raw)['archive_plan']['archive_filename'] == Path(result['archive']).name
    for path in Path(result['review_directory']).iterdir():
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
    assert synthetic_source.read_bytes() == before


def test_last_response_review_uses_only_selected_rendered_content(
    synthetic_source: Path, tmp_path: Path, monkeypatch, capsys,
):
    before = synthetic_source.read_bytes()
    result = _export(monkeypatch, capsys, synthetic_source, tmp_path / 'export', mode='last-response')
    index, reconstructed = _read_review(result)
    assert NEW_ANSWER.encode() in reconstructed
    assert OLD_PROMPT.encode() not in reconstructed
    assert OLD_ANSWER.encode() not in reconstructed
    for path in Path(result['review_directory']).iterdir():
        if path.is_file():
            assert OLD_PROMPT.encode() not in path.read_bytes()
            assert OLD_ANSWER.encode() not in path.read_bytes()
    with zipfile.ZipFile(result['archive']) as archive:
        assert archive.read(result['archived_markdown']) == reconstructed
        # The separate archival source remains complete, independently of the
        # selected rendered/review mode; it is never copied into review parts.
        assert archive.read(result['archived_source']) == before
    assert index['source_sha256'] == hashlib.sha256(before).hexdigest()
    assert synthetic_source.read_bytes() == before


def test_no_file_creates_neither_review_nor_archival_outputs(
    synthetic_source: Path, tmp_path: Path, monkeypatch, capsys,
):
    def forbidden(*args, **kwargs):
        raise AssertionError('--no-file attempted file packaging')

    monkeypatch.setattr(review, 'create_review_bundle', forbidden)
    monkeypatch.setattr(archive_pipeline, 'package_export', forbidden)
    output = tmp_path / 'no-file-output'
    result = _export(monkeypatch, capsys, synthetic_source, output,
                     extra=('--no-file', '--no-manifest'))
    assert result['file'] is None
    assert result['manifest'] is None
    assert not result.get('review_archive')
    assert not result.get('archive')
    assert not output.exists() or not list(output.iterdir())


def test_retrofit_cli_needs_neither_codex_home_tokenizer_nor_archiver(
    tmp_path: Path, monkeypatch, capsys,
):
    raw = '# Existing transcript\r\nUnicode 🧠 and preserved text.\r\n'.encode('utf-8')
    source = tmp_path / 'old-lzma-export.zip'
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_LZMA) as archive:
        archive.writestr('thread.md', raw)
        archive.writestr('thread.manifest.json', json.dumps({'source_sha256': 'a' * 64}))
        archive.writestr('thread.jsonl', b'Unneeded native snapshot')
    before = source.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError('Retrofit attempted a native-session dependency')

    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', forbidden)
    monkeypatch.setattr(cli, 'resolve_source_paths', forbidden)
    monkeypatch.setattr(cli, 'configure_token_counter', forbidden)
    monkeypatch.setattr(archive_pipeline, 'find_archiver', forbidden)
    result = _invoke(monkeypatch, capsys, [
        '--review-from', str(source), '--out-dir', str(tmp_path / 'converted'),
        '--archive-method', 'LZMA', '--archiver-path', str(tmp_path / 'missing-7z'),
    ])
    index, reconstructed = _read_review(result)
    assert reconstructed == raw
    assert index['source_sha256'] == 'a' * 64
    assert 'native_jsonl_not_opened' in index['input_verification']
    assert source.read_bytes() == before


@pytest.mark.parametrize('extra', [
    ('--redact',),
    ('--no-file',),
    ('--mode', 'last-response'),
    ('--mode=last-response',),
    ('--mo=last-response',),
    ('--mode', 'thread'),
    ('--last-n-turns', '1'),
    ('--response', '1'),
    ('--message', '1'),
    ('--from-message', '1'),
], ids=['redact', 'no-file', 'mode-default-value', 'mode-equals', 'mode-abbreviated', 'mode-thread',
        'last-n-turns', 'response', 'message', 'range'])
def test_retrofit_rejects_flags_that_would_misstate_scope_or_privacy(
    tmp_path: Path, monkeypatch, capsys, extra: tuple[str, ...],
):
    raw = b'Historical text requiring explicit scope.\n'
    source = tmp_path / 'existing.md'
    source.write_bytes(raw)
    output = tmp_path / 'converted'
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(sys, 'argv', [
        'codex-export', '--review-from', str(source), '--out-dir', str(output), *extra,
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert '--review-from' in capsys.readouterr().err
    assert source.read_bytes() == raw
    assert not output.exists() or not list(output.iterdir())


def test_review_failure_retains_verified_archive_and_every_loose_export(
    synthetic_source: Path, tmp_path: Path, monkeypatch, capsys,
):
    before = synthetic_source.read_bytes()
    output = tmp_path / 'export'

    def fail_review(*args, **kwargs):
        raise RuntimeError('Synthetic review creation failure')

    monkeypatch.setattr(review, 'create_review_bundle', fail_review)
    with pytest.raises(RuntimeError, match='Synthetic review creation failure'):
        _export(monkeypatch, capsys, synthetic_source, output)
    archives = list(output.glob('*.zip'))
    assert len(archives) == 1
    assert not archives[0].name.endswith('.review.zip')
    assert len(list(output.glob('*.md'))) == 1
    assert len(list(output.glob('*.manifest.json'))) == 1
    assert len(list(output.glob('*.jsonl'))) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            assert archive.read(name) == (output / name).read_bytes()
        native_name = next(name for name in archive.namelist() if name.endswith('.jsonl'))
        assert archive.read(native_name) == before
    assert synthetic_source.read_bytes() == before
    assert not list(output.glob('*.review'))


def test_redaction_rejects_default_unredacted_native_copy(
    synthetic_source: Path, tmp_path: Path, monkeypatch, capsys,
):
    before = synthetic_source.read_bytes()
    output = tmp_path / 'export'
    with pytest.raises(SystemExit) as exc:
        _export(monkeypatch, capsys, synthetic_source, output, extra=('--redact',))
    assert exc.value.code == 2
    assert '--no-source-copy' in capsys.readouterr().err
    assert synthetic_source.read_bytes() == before
    assert not output.exists() or not list(output.iterdir())
