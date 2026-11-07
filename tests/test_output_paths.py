"""Output path collisions must not overwrite live input or another artifact."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from codex_exporter import cli
from codex_exporter.file_access import validate_export_paths


def test_output_paths_reject_resolved_source_alias(tmp_path):
    source = tmp_path / 'rollout.jsonl'
    source.write_bytes(b'original bytes')
    alias = source.parent / '.' / source.name
    with pytest.raises(ValueError, match='aliases a source file'):
        validate_export_paths({'manifest': alias}, [source])
    assert source.read_bytes() == b'original bytes'


def test_output_paths_reject_hardlink_to_source(tmp_path):
    source, alias = tmp_path / 'rollout.jsonl', tmp_path / 'export.manifest.json'
    source.write_bytes(b'original bytes')
    os.link(source, alias)
    with pytest.raises(ValueError, match='aliases a source file'):
        validate_export_paths({'manifest': alias}, [source])
    assert source.read_bytes() == alias.read_bytes() == b'original bytes'


def test_output_paths_reject_aliases_between_generated_files(tmp_path):
    output = tmp_path / 'codex_example.md'
    with pytest.raises(ValueError, match='refer to the same file'):
        validate_export_paths({'Markdown': output, 'manifest': tmp_path / '.' / output.name})
    assert not output.exists()
    output.write_bytes(b'previous export')
    manifest = tmp_path / 'codex_example.manifest.json'
    os.link(output, manifest)
    with pytest.raises(ValueError, match='refer to the same file'):
        validate_export_paths({'Markdown': output, 'manifest': manifest})
    assert output.read_bytes() == manifest.read_bytes() == b'previous export'


def test_output_paths_reject_symlink_alias(tmp_path):
    source = tmp_path / 'rollout.jsonl'
    source.write_bytes(b'original bytes')
    link = tmp_path / 'export.manifest.json'
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f'Creating a symbolic link requires permission on this runner: {exc}')
    with pytest.raises(ValueError, match='aliases a source file'):
        validate_export_paths({'manifest': link}, [source])
    assert source.read_bytes() == b'original bytes'


def test_output_paths_allow_distinct_content_identical_files_without_writes(tmp_path):
    source, existing = tmp_path / 'rollout.jsonl', tmp_path / 'codex_example.md'
    source.write_bytes(b'same bytes')
    existing.write_bytes(b'same bytes')
    future = tmp_path / 'new' / 'codex_example.manifest.json'
    before = existing.stat()
    validate_export_paths({'Markdown': existing, 'manifest': future}, [source])
    assert not future.parent.exists()
    assert existing.read_bytes() == source.read_bytes() == b'same bytes'
    assert existing.stat().st_mtime_ns == before.st_mtime_ns


@pytest.fixture
def source(tmp_path, monkeypatch):
    original = tmp_path / 'rollout.jsonl'
    rows = [{'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Synthetic preserved reply'}}]
    original.write_bytes(b''.join((json.dumps(row) + '\n').encode() for row in rows))
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'unavailable-codex-home')
    return original


def _invoke(monkeypatch, source: Path, output: Path, manifest: Path, extra=()):
    monkeypatch.setattr(sys, 'argv', [
        'codex-export', '--jsonl', str(source), '--mode', 'thread', '--no-fast',
        '--no-progress', '--tokenizer', 'regex', '--name', 'Synthetic',
        '--filename-template', 'codex_alias.md', '--out-dir', str(output),
        '--no-remember-out-dir', '--manifest-path', str(manifest),
        '--archive-backend', 'python', '--archive-method', 'Deflate', '--archive-level', '6',
        *extra,
    ])
    return cli.main()


@pytest.mark.parametrize('hardlink', [False, True])
def test_cli_manifest_alias_of_source_is_rejected_before_snapshot(source, tmp_path, monkeypatch, hardlink):
    from codex_exporter import snapshot

    original = source.read_bytes()
    manifest = tmp_path / 'aliased.manifest.json' if hardlink else source
    if hardlink:
        os.link(source, manifest)
    output = tmp_path / 'output'

    def never_capture(*args, **kwargs):
        pytest.fail('Source/output aliases must be rejected before snapshot capture')

    monkeypatch.setattr(snapshot, 'snapshot_source', never_capture)
    with pytest.raises((ValueError, SystemExit)):
        _invoke(monkeypatch, source, output, manifest, ['--no-archive', '--no-source-copy'])
    assert source.read_bytes() == original
    assert manifest.read_bytes() == original
    assert not output.exists()


def test_cli_manifest_cannot_overwrite_rendered_markdown(source, tmp_path, monkeypatch):
    original = source.read_bytes()
    output = tmp_path / 'output'
    manifest = output / 'codex_alias.md'
    with pytest.raises((ValueError, SystemExit)):
        _invoke(monkeypatch, source, output, manifest)
    assert source.read_bytes() == original
    assert not manifest.exists()
    assert not list(output.glob('*.zip'))
