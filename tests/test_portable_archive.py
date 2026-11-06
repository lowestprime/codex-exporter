"""Portable ZIP creation and safe publication across filesystem capabilities."""
from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from codex_exporter import archive_pipeline as pipeline


@pytest.fixture
def export(tmp_path: Path):
    source = tmp_path / 'app-owned.jsonl'
    source.write_bytes(b'{"type":"synthetic","text":"original source"}\n' * 10000)
    output = tmp_path / 'out'
    output.mkdir()
    markdown = output / 'export.md'
    markdown.write_text('# Example\n\nComplete transcript.\n' * 1000, encoding='utf-8')
    manifest = output / 'export.manifest.json'
    manifest.write_text('{"synthetic":true}\n', encoding='utf-8')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = {'file': str(markdown), 'manifest': str(manifest), 'source_sha256': digest}
    return source, markdown, manifest, result


def _no_links(*args, **kwargs):
    raise OSError(errno.EXDEV, 'Synthetic filesystem without hardlink support')


def test_default_deflate_roundtrips_without_external_archiver(export, monkeypatch):
    source, markdown, manifest, result = export
    expected_markdown = markdown.read_bytes()
    original = source.read_bytes()
    monkeypatch.setattr(pipeline, 'find_archiver', lambda *args: pytest.fail('External archiver used'))
    settings = pipeline.ArchiveSettings()
    assert settings.method == 'Deflate' and settings.level == 6
    result = pipeline.package_export(result, source, settings)
    assert result['archive_verified'] and result['archive_backend'] == 'python'
    assert result['archiver'] == 'python.zipfile'
    assert result['loose_files_removed']
    assert result['archive_settings']['threads'] == 1
    with zipfile.ZipFile(result['archive']) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_DEFLATED}
        assert max(info.extract_version for info in archive.infolist()) <= 20
        assert archive.read(markdown.name) == expected_markdown
        assert archive.read(markdown.with_suffix('.jsonl').name) == original
        detail = json.loads(archive.read(manifest.name))
        assert detail['archive_plan']['backend'] == 'python'
        assert detail['archive_plan']['method'] == 'Deflate'
        assert detail['archive_plan']['dictionary'] is None
    assert source.read_bytes() == original
    assert not markdown.exists() and not manifest.exists()
    # When the legacy system extractor is available, test real interoperability.
    if shutil.which('unzip'):
        check = subprocess.run(['unzip', '-t', result['archive']], capture_output=True,
                               text=True, timeout=30)
        assert check.returncode == 0, check.stdout + check.stderr


def test_python_backend_overrides_retained_external_path(export, monkeypatch):
    source, markdown, manifest, result = export
    monkeypatch.setattr(pipeline, 'find_archiver', lambda *args: pytest.fail('Stale path consulted'))
    settings = pipeline.ArchiveSettings(backend='python', archiver='missing-seven-zip')
    result = pipeline.package_export(result, source, settings)
    assert result['archive_backend'] == 'python'


@pytest.mark.parametrize('settings, message', [
    (pipeline.ArchiveSettings(backend='unknown'), 'archive backend'),
    (pipeline.ArchiveSettings(backend='python', method='LZMA'), 'supports Deflate'),
])
def test_invalid_backend_combination_fails_before_writes(export, settings, message):
    source, markdown, manifest, result = export
    with pytest.raises(ValueError, match=message):
        pipeline.package_export(result, source, settings)
    assert markdown.exists() and manifest.exists()
    assert not markdown.with_suffix('.jsonl').exists()
    assert not markdown.with_suffix('.zip').exists()


def test_compression_level_is_respected_without_changing_content(tmp_path):
    source = tmp_path / 'repeated.txt'
    source.write_text('abc0123456789 Complete repeatable transcript\n' * 20000)
    sizes = []
    for level in (1, 9):
        archive = tmp_path / f'level-{level}.zip'
        pipeline._write_python_archive(archive, {source.name: source}, level)
        assert pipeline._verify_members(archive, {source.name: source}, 'Deflate') == {
            source.name: 'Deflate'}
        sizes.append(archive.stat().st_size)
    assert sizes[1] < sizes[0]


def test_archive_publication_without_hardlinks_is_verified(export, monkeypatch):
    source, markdown, manifest, result = export
    monkeypatch.setattr(pipeline.os, 'link', _no_links)
    result = pipeline.package_export(result, source, pipeline.ArchiveSettings())
    assert result['archive_publication'] == 'verified_copy'
    assert result['archive_verified'] and result['loose_files_removed']
    with zipfile.ZipFile(result['archive']) as archive:
        assert archive.read(markdown.with_suffix('.jsonl').name) == source.read_bytes()
    assert not list(markdown.parent.glob('.*.zip'))


def test_publication_race_preserves_existing_archive_and_loose_files(export, monkeypatch):
    source, markdown, manifest, result = export
    other = b'Archive published by another process'

    def competing_link(staged, destination):
        destination.write_bytes(other)
        raise OSError(errno.EXDEV, 'Synthetic link failure after a competing publication')

    monkeypatch.setattr(pipeline.os, 'link', competing_link)
    with pytest.raises(FileExistsError):
        pipeline.package_export(result, source, pipeline.ArchiveSettings())
    assert markdown.with_suffix('.zip').read_bytes() == other
    assert markdown.exists() and manifest.exists() and markdown.with_suffix('.jsonl').exists()
    assert source.exists()
    assert not list(markdown.parent.glob('.*.zip'))


def test_copy_verification_failure_removes_only_new_partial_destination(tmp_path, monkeypatch):
    source = tmp_path / 'stage.zip'
    source.write_bytes(b'Previously verified staging bytes')
    destination = tmp_path / 'published.zip'
    original_hash = pipeline._hash_file

    def wrong_readback(path):
        return ('wrong hash', 0) if path == destination else original_hash(path)

    monkeypatch.setattr(pipeline.os, 'link', _no_links)
    monkeypatch.setattr(pipeline, '_hash_file', wrong_readback)
    with pytest.raises(RuntimeError, match='copy verification failed'):
        pipeline._publish_archive(source, destination)
    assert source.read_bytes() == b'Previously verified staging bytes'
    assert not destination.exists()


def test_changed_member_after_verification_prevents_publication_and_cleanup(export, monkeypatch):
    source, markdown, manifest, result = export
    verify = pipeline._verify_members

    def verify_then_change(*args, **kwargs):
        methods = verify(*args, **kwargs)
        with markdown.open('ab') as output:
            output.write(b'Newer export data')
        return methods

    monkeypatch.setattr(pipeline, '_verify_members', verify_then_change)
    with pytest.raises(RuntimeError, match='member changed during packaging'):
        pipeline.package_export(result, source, pipeline.ArchiveSettings())
    assert markdown.read_bytes().endswith(b'Newer export data')
    assert manifest.exists() and markdown.with_suffix('.jsonl').exists()
    assert not markdown.with_suffix('.zip').exists()
    assert source.exists()


def test_changed_member_after_publication_preserves_all_loose_files(export, monkeypatch):
    source, markdown, manifest, result = export
    publish = pipeline._publish_archive
    expected = markdown.read_bytes()

    def publish_then_change(*args, **kwargs):
        strategy = publish(*args, **kwargs)
        with markdown.open('ab') as output:
            output.write(b'Newer export data')
        return strategy

    monkeypatch.setattr(pipeline, '_publish_archive', publish_then_change)
    with pytest.raises(RuntimeError, match='member changed during packaging'):
        pipeline.package_export(result, source, pipeline.ArchiveSettings())
    assert markdown.read_bytes() == expected + b'Newer export data'
    assert manifest.exists() and markdown.with_suffix('.jsonl').exists()
    with zipfile.ZipFile(markdown.with_suffix('.zip')) as archive:
        assert archive.read(markdown.name) == expected
    assert source.exists()


def test_failed_python_writer_retains_export_files(export, monkeypatch):
    source, markdown, manifest, result = export

    def fail(staged, members, level):
        staged.write_bytes(b'Incomplete archive')
        raise OSError('Synthetic write failure')

    monkeypatch.setattr(pipeline, '_write_python_archive', fail)
    with pytest.raises(OSError, match='Synthetic write failure'):
        pipeline.package_export(result, source, pipeline.ArchiveSettings())
    assert markdown.exists() and manifest.exists() and markdown.with_suffix('.jsonl').exists()
    assert not markdown.with_suffix('.zip').exists()
    assert not list(markdown.parent.glob('.*.zip'))
    assert source.exists()


def test_explicit_lzma_keeps_external_dictionary_controls(export, monkeypatch):
    from types import SimpleNamespace

    source, markdown, manifest, result = export
    executable = markdown.parent / 'synthetic-seven-zip'
    called = []
    monkeypatch.setattr(pipeline, 'find_archiver', lambda explicit: executable)

    def external(command, *, cwd, **kwargs):
        called.append(command)
        target = next(part for part in command if part.endswith('.zip'))
        names = command[command.index(target) + 1:]
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_LZMA) as archive:
            for name in names:
                archive.write(Path(cwd) / name, arcname=name)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(pipeline.subprocess, 'run', external)
    result = pipeline.package_export(result, source, pipeline.ArchiveSettings(method='LZMA', level=9))
    assert result['archive_backend'] == '7zip'
    assert result['archive_method'] == 'LZMA'
    assert {'-mm=LZMA', '-md=256m', '-mfb=64', '-mmt=19', '-mx=9'} <= set(called[0])
    with zipfile.ZipFile(result['archive']) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_LZMA}
        assert archive.read(markdown.with_suffix('.jsonl').name) == source.read_bytes()
