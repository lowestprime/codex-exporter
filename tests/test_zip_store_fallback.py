"""ZIP per-member compression-method verification regressions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_exporter.archive_pipeline import _verify_members
import pytest


def _create_zip(tmp_path: Path, method_by_name: dict[str, int], tamper: str = ''):
    contents = {
        'record.md': b'Markdown\n' * 11000,
        'record.manifest.json': b'{"title":"test"}\n',
        'record.jsonl': b'{"type":"session_meta"}\n' * 11000,
    }
    expected = {}
    archive = tmp_path / 'record.zip'
    for name, content in contents.items():
        path = tmp_path / name
        path.write_bytes(content)
        expected[name] = path
    with zipfile.ZipFile(archive, 'w') as z:
        for name, method in method_by_name.items():
            content = contents[name]
            if name == tamper:
                content = b'tamper' + content
            z.writestr(name, content, compress_type=method)
    return archive, expected


def test_mixed_store_and_lzma_validated(tmp_path):
    methods = {
        'record.md': zipfile.ZIP_LZMA,
        'record.manifest.json': zipfile.ZIP_STORED,
        'record.jsonl': zipfile.ZIP_LZMA,
    }
    archive, expected = _create_zip(tmp_path, methods)
    got = _verify_members(archive, expected, 'LZMA')
    assert got == {'record.md': 'LZMA', 'record.manifest.json': 'Store', 'record.jsonl': 'LZMA'}


def test_tiny_all_store_allowed(tmp_path):
    names = ('record.md', 'record.manifest.json', 'record.jsonl')
    archive, expected = _create_zip(tmp_path, {name: zipfile.ZIP_STORED for name in names})
    # Only 17 bytes of manifest are small, but other records are big: fail.
    with pytest.raises(RuntimeError, match='Substantive ZIP uses Store'):
        _verify_members(archive, expected, 'LZMA')
    for name in names:
        expected[name].write_bytes(b'A')
    with zipfile.ZipFile(archive, 'w') as z:
        for name in names:
            z.write(expected[name], arcname=name, compress_type=zipfile.ZIP_STORED)
    assert set(_verify_members(archive, expected, 'LZMA').values()) == {'Store'}


def test_wrong_method_and_corruption_rejected(tmp_path):
    methods = {'record.md': zipfile.ZIP_LZMA,
               'record.manifest.json': zipfile.ZIP_STORED,
               'record.jsonl': zipfile.ZIP_DEFLATED}
    archive, expected = _create_zip(tmp_path, methods)
    with pytest.raises(RuntimeError, match='compression method mismatch'):
        _verify_members(archive, expected, 'LZMA')
    methods['record.jsonl'] = zipfile.ZIP_LZMA
    archive, expected = _create_zip(tmp_path, methods, tamper='record.manifest.json')
    with pytest.raises(RuntimeError, match='Archive content mismatch'):
        _verify_members(archive, expected, 'LZMA')


def test_duplicate_name_rejected(tmp_path):
    path = tmp_path / 'a.md'
    path.write_bytes(b'a')
    dest = tmp_path / 'a.zip'
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(dest, 'w') as z:
            z.write(path, arcname='a.md', compress_type=zipfile.ZIP_STORED)
            z.write(path, arcname='a.md', compress_type=zipfile.ZIP_STORED)
    with pytest.raises(RuntimeError, match='inventory mismatch'):
        _verify_members(dest, {'a.md': path}, 'LZMA')


def test_full_cli_archive_with_stored_manifest(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('legacy_archive_test', Path(__file__).with_name('test_archive_pipeline.py'))
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    source, source_hash, archiver, env = legacy.prepare(tmp_path)
    if sys.platform == 'win32':
        # The Windows launcher invokes mock-seven-zip.py; edit the actual mock.
        stub = tmp_path / 'mock-seven-zip.py'
    else:
        stub = archiver
    text = stub.read_text()
    assert 'z.write(f,arcname=f)' in text
    stub.write_text(text.replace('z.write(f,arcname=f)',
        "z.write(f,arcname=f,compress_type=zipfile.ZIP_STORED if f.endswith('.manifest.json') else zipfile.ZIP_LZMA)"))
    result = legacy.run(env, '--mode', 'thread', legacy.SID, '--fast', '--no-progress',
        '--archiver-path', str(archiver), '--out-dir', str(tmp_path/'out'), '--json')
    assert result.returncode == 0, result.stderr
    val = json.loads(result.stdout)
    assert val['archive_verified'] is True
    assert val['loose_files_removed'] is True
    assert val['archive_used_store_fallback'] is True
    assert sorted(val['archive_member_methods'].values()) == ['LZMA', 'LZMA', 'Store']
    assert Path(val['archive']).is_file()
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
