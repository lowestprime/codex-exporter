"""CLI transaction edges: prior outputs, explicit paths and configuration recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'Export-CodexThread.py'


def run_cli(tmp_path, *args):
    return subprocess.run([sys.executable, str(ENTRY), *map(str, args)],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
        env={**os.environ, 'CODEX_HOME': str(tmp_path / 'codex-home'),
             'CODEX_EXPORT_CONFIG': str(tmp_path / 'config.json')})


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'rollout-11111111-2222-4333-8444-555555555555.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': '11111111-2222-4333-8444-555555555555'}},
        {'type': 'event_msg', 'timestamp': '2026-11-15T01:50:00Z',
         'payload': {'type': 'user_message', 'message': 'Requested original prompt'}},
        {'type': 'event_msg', 'timestamp': '2026-11-15T01:50:01Z',
         'payload': {'type': 'agent_message', 'message': 'Requested original answer'}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return path


def export_args(tmp_path, source):
    return ['--jsonl', source, '--mode', 'thread', '--no-fast', '--no-progress',
            '--tokenizer', 'regex', '--no-remember-out-dir', '--out-dir', tmp_path / 'out',
            '--filename-template', 'fixed.md', '--no-filename-counts', '--json']


@pytest.mark.parametrize('saved', [
    {'display_defaults': {'timestamp_timezone': 'Unavailable/Timezone'}},
    {'archive_defaults': {'level': 0}},
])
@pytest.mark.parametrize('operation', ['--help', '--version', '--show-config', '--reset-config'])
def test_diagnostics_and_reset_work_with_invalid_saved_values(tmp_path, saved, operation):
    config = tmp_path / 'config.json'
    original = json.dumps(saved).encode()
    config.write_bytes(original)
    run = run_cli(tmp_path, operation)
    assert run.returncode == 0, run.stderr
    assert 'Traceback' not in run.stderr
    if operation == '--reset-config':
        assert not config.exists()
    else:
        assert config.read_bytes() == original
    if operation == '--show-config':
        assert all(key in json.loads(run.stdout) for key in saved)


def test_valid_cli_timezone_override_recovers_unavailable_saved_timezone(tmp_path, source):
    config = tmp_path / 'config.json'
    original = json.dumps({'display_defaults': {'timestamp_timezone': 'Unavailable/Timezone'}}).encode()
    config.write_bytes(original)
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--no-archive', '--no-source-copy',
                  '--timestamp-timezone', 'UTC')
    assert run.returncode == 0, run.stderr
    assert 'November 15, 2026 at 01:50:00 AM UTC' in Path(json.loads(run.stdout)['file']).read_text()
    assert config.read_bytes() == original


def test_corrective_preference_setter_can_replace_bad_saved_timezone(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'display_defaults': {'timestamp_timezone': 'Unavailable/Timezone'}}))
    run = run_cli(tmp_path, '--set-default', 'timestamp_timezone=UTC')
    assert run.returncode == 0, run.stderr
    assert json.loads(config.read_text())['display_defaults']['timestamp_timezone'] == 'UTC'


def test_corrective_archive_setter_can_replace_bad_saved_level(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'archive_defaults': {'level': 0}}))
    run = run_cli(tmp_path, '--set-archive-default', 'level=9')
    assert run.returncode == 0, run.stderr
    assert json.loads(config.read_text())['archive_defaults']['level'] == 9


def test_skipped_markdown_never_rewrites_explicit_existing_manifest(tmp_path, source):
    output = tmp_path / 'out'
    output.mkdir()
    markdown = output / 'fixed.md'
    markdown.write_bytes(b'Previously published Markdown\n')
    manifest = tmp_path / 'custom.manifest.json'
    manifest.write_bytes(b'{"previous":"manifest must not change"}\n')
    before = {path: path.read_bytes() for path in (markdown, manifest, source)}
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--no-archive', '--no-source-copy',
                  '--collision', 'skip', '--manifest-path', manifest)
    assert run.returncode == 0, run.stderr
    assert {path: path.read_bytes() for path in before} == before
    assert json.loads(run.stdout).get('skipped') is True
    assert list(output.iterdir()) == [markdown]


@pytest.mark.parametrize('fast', [False, True])
def test_skip_existing_archive_is_successful_and_never_overwrites(tmp_path, source, fast):
    args = export_args(tmp_path, source)
    if fast:
        args[args.index('--no-fast')] = '--fast'
    args += ['--archive-method', 'Deflate', '--archive-backend', 'python', '--no-source-copy']
    first = run_cli(tmp_path, *args)
    assert first.returncode == 0, first.stderr
    prior = Path(json.loads(first.stdout)['archive'])
    prior_bytes = prior.read_bytes()
    retry = run_cli(tmp_path, *args, '--collision', 'skip')
    assert retry.returncode == 0, retry.stderr
    result = json.loads(retry.stdout)
    assert result.get('skipped') is True
    assert prior.read_bytes() == prior_bytes
    assert list((tmp_path / 'out').glob('*.zip')) == [prior]
    assert not list((tmp_path / 'out').glob('.codex-job-*'))


def test_external_manifest_path_in_archival_mode_is_rejected_without_writes(tmp_path, source):
    external = tmp_path / 'elsewhere' / 'custom.manifest.json'
    external.parent.mkdir()
    external.write_bytes(b'{"previous":"do not overwrite"}')
    before = external.read_bytes()
    run = run_cli(tmp_path, *export_args(tmp_path, source),
                  '--archive-method', 'Deflate', '--archive-backend', 'python',
                  '--manifest-path', external)
    assert run.returncode != 0
    assert 'manifest' in run.stderr.casefold()
    assert external.read_bytes() == before
    assert not [p for p in (tmp_path / 'out').rglob('*') if p.is_file()]


def test_loose_export_honors_external_manifest_path(tmp_path, source):
    external = tmp_path / 'elsewhere' / 'custom.manifest.json'
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--no-archive', '--no-source-copy',
                  '--manifest-path', external)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert Path(result['manifest']) == external
    assert external.is_file()
    assert not Path(result['file']).with_suffix('.manifest.json').exists()


def test_stdout_and_json_conflict_before_emitting_or_writing(tmp_path, source):
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--stdout', '--no-file')
    assert run.returncode != 0
    assert run.stdout == ''
    assert 'stdout' in run.stderr.casefold() and 'json' in run.stderr.casefold()
    assert not [p for p in (tmp_path / 'out').rglob('*') if p.is_file()]


def test_renamed_archive_and_members_share_resolved_basename(tmp_path, source):
    output = tmp_path / 'out'
    output.mkdir()
    existing = output / 'fixed.zip'
    existing.write_bytes(b'Previously published archive sentinel')
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--collision', 'rename',
                  '--archive-method', 'Deflate', '--archive-backend', 'python')
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    new_archive = Path(result['archive'])
    assert existing.read_bytes() == b'Previously published archive sentinel'
    assert new_archive != existing and new_archive.stem.startswith('fixed-')
    with zipfile.ZipFile(new_archive) as archive:
        assert set(archive.namelist()) == {
            new_archive.stem + '.md', new_archive.stem + '.manifest.json', new_archive.stem + '.jsonl'}
        assert archive.read(new_archive.stem + '.jsonl') == source.read_bytes()
    assert sorted(p.name for p in output.iterdir()) == sorted([existing.name, new_archive.name])


def test_archive_overwrite_does_not_replace_prior_zip(tmp_path, source):
    output = tmp_path / 'out'
    output.mkdir()
    existing = output / 'fixed.zip'
    existing.write_bytes(b'Existing verified archive sentinel')
    run = run_cli(tmp_path, *export_args(tmp_path, source), '--collision', 'overwrite',
                  '--archive-method', 'Deflate', '--archive-backend', 'python')
    assert run.returncode != 0
    assert existing.read_bytes() == b'Existing verified archive sentinel'
    assert list(output.iterdir()) == [existing]


def test_no_file_clipboard_receives_text_without_publishing(tmp_path, source, monkeypatch, capsys):
    from codex_exporter import cli
    copied = []
    monkeypatch.setattr(cli, 'copy_to_clipboard', copied.append)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    args = ['--jsonl', str(source), '--mode', 'thread', '--no-fast', '--no-progress',
            '--tokenizer', 'regex', '--no-file', '--clipboard', '--no-remember-out-dir',
            '--out-dir', str(tmp_path / 'out'), '--json']
    monkeypatch.setattr(sys, 'argv', ['codex-export', *args])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert len(copied) == 1 and 'Requested original prompt' in copied[0]
    assert result['file'] is None and result['manifest'] is None
    assert not (tmp_path / 'out').exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_batch_private_staging_reserves_duplicate_requested_basenames(tmp_path):
    inputs = []
    for number in (1, 2):
        path = tmp_path / f'no-id-{number}.jsonl'
        path.write_text(json.dumps({'type': 'event_msg', 'payload': {
            'type': 'user_message', 'message': f'Distinct original prompt {number}'}}) + '\n')
        inputs += ['--jsonl', path]
    run = run_cli(tmp_path, *inputs, '--mode', 'thread', '--no-fast', '--no-progress',
                  '--tokenizer', 'regex', '--no-remember-out-dir', '--out-dir', tmp_path / 'out',
                  '--filename-template', 'fixed.md', '--no-filename-counts', '--name', 'Same title',
                  '--archive-method', 'Deflate', '--archive-backend', 'python', '--no-source-copy', '--json')
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    with zipfile.ZipFile(result['archive']) as archive:
        markdown = [name for name in archive.namelist() if name.endswith('.md')]
        manifests = [name for name in archive.namelist() if name.endswith('.manifest.json')]
        assert len(markdown) == len(set(markdown)) == 2
        assert len(manifests) == 2
        content = '\n'.join(archive.read(name).decode() for name in markdown)
        assert 'Distinct original prompt 1' in content
        assert 'Distinct original prompt 2' in content
        assert {Path(name).stem + '.manifest.json' for name in markdown} == set(manifests)
    assert not list((tmp_path / 'out').glob('.codex-job-*'))
