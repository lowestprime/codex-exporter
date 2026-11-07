"""Publication must not carry private Windows staging ACLs into exported files."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

import pytest

from codex_exporter import file_access


def test_copy_publication_is_exclusive_verified_and_reports_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    raw = b'export bytes\n' * 400_000
    source.write_bytes(raw)
    updates = []
    assert file_access.publish_file(source, output, progress=lambda n, total: updates.append((n, total))) == 'verified_copy'
    assert source.read_bytes() == output.read_bytes() == raw
    assert not os.path.samefile(source, output)
    assert updates[0] == (0, len(raw))
    assert updates[-1] == (len(raw), len(raw))
    assert [n for n, _ in updates] == sorted(n for n, _ in updates)
    assert all(total == len(raw) for _, total in updates)
    with pytest.raises(FileExistsError):
        file_access.publish_file(source, output)
    assert output.read_bytes() == raw


def test_copy_failure_removes_only_new_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    source.write_bytes(b'preserve staging')

    def cancel(done, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        file_access.publish_file(source, output, progress=cancel)
    assert not output.exists()
    assert source.read_bytes() == b'preserve staging'


def test_copy_rejects_changed_stage_and_retains_source(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    source.write_bytes(b'initial staging bytes')

    def change(done, total):
        if not done:
            with source.open('ab') as stream:
                stream.write(b'changed')

    with pytest.raises(RuntimeError, match='changed during publication'):
        file_access.publish_file(source, output, progress=change)
    assert not output.exists()
    assert source.read_bytes().endswith(b'changed')


def test_copy_readback_failure_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    source.write_bytes(b'verified staging')
    original_digest = file_access._digest
    calls = 0

    def bad_readback(stream, **kwargs):
        nonlocal calls
        calls += 1
        digest, size = original_digest(stream, **kwargs)
        return ('0' * 64, size) if calls == 2 else (digest, size)

    monkeypatch.setattr(file_access, '_digest', bad_readback)
    with pytest.raises(RuntimeError, match='copy verification failed'):
        file_access.publish_file(source, output)
    assert not output.exists()
    assert source.read_bytes() == b'verified staging'


@pytest.mark.skipif(os.name == 'nt', reason='Windows open-handle deletion has different sharing semantics')
def test_copy_does_not_remove_a_replacement_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    source.write_bytes(b'staging')

    def replace(done, total):
        if not done:
            output.unlink()
            output.write_bytes(b'unrelated replacement')

    with pytest.raises(RuntimeError, match='replaced during verification'):
        file_access.publish_file(source, output, progress=replace)
    assert output.read_bytes() == b'unrelated replacement'


def test_copy_empty_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_empty.zip'
    source.write_bytes(b'')
    updates = []
    assert file_access.publish_file(source, output, progress=lambda *args: updates.append(args)) == 'verified_copy'
    assert output.read_bytes() == b''
    assert updates == [(0, 0)]


def test_diagnose_reads_file_without_modification(tmp_path):
    path = tmp_path / 'codex_example.md'
    path.write_text('readable export', encoding='utf-8')
    before = path.stat()
    report = file_access.diagnose_access(path)
    assert report['readable'] is True
    assert report['kind'] == 'file'
    assert report['size_bytes'] == before.st_size
    assert path.read_text(encoding='utf-8') == 'readable export'
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert file_access.diagnose_access(tmp_path / 'missing.zip')['readable'] is False


def test_repair_plan_rejects_app_sources_and_unrecognized_directories(tmp_path):
    app = tmp_path / '.codex' / 'sessions'
    app.mkdir(parents=True)
    original = app / 'codex_example.jsonl'
    original.write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='app-owned'):
        file_access._repair_plan([original])
    with pytest.raises(ValueError, match='named codex_'):
        file_access._repair_plan([tmp_path])
    broad = tmp_path / 'codex_exports'
    broad.mkdir()
    with pytest.raises(ValueError, match='specifically named .review'):
        file_access._repair_plan([broad])


def test_repair_plan_rejects_hardlinks_and_unexpected_review_entries(tmp_path):
    original, linked = tmp_path / 'original.jsonl', tmp_path / 'codex_example.jsonl'
    original.write_text('{}\n', encoding='utf-8')
    os.link(original, linked)
    with pytest.raises(ValueError, match='multiply linked'):
        file_access._repair_plan([linked])
    directory = tmp_path / 'codex_example.review'
    directory.mkdir()
    for name in ('START_HERE.txt', 'review-index.json', 'unrelated.txt'):
        (directory / name).write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected review export entry'):
        file_access._repair_plan([directory])
    assert original.read_text(encoding='utf-8') == '{}\n'


def test_repair_plan_accepts_only_explicit_flat_review_tree(tmp_path):
    archive = tmp_path / 'codex_example.zip'
    archive.write_bytes(b'archive')
    directory = tmp_path / 'codex_example.review'
    directory.mkdir()
    for name in ('START_HERE.txt', 'review-index.json', 'part-0001.txt'):
        (directory / name).write_text('{}', encoding='utf-8')
    plan = file_access._repair_plan([archive, directory, archive])
    assert plan == [archive, directory, *sorted(directory.iterdir())]


@pytest.mark.skipif(os.name == 'nt', reason='Symlink creation may require a privilege on Windows')
def test_repair_refuses_symlinks_in_any_component(tmp_path):
    directory = tmp_path / 'real'
    directory.mkdir()
    source = directory / 'codex_example.zip'
    source.write_bytes(b'archive')
    link = tmp_path / 'link'
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match='links and reparse points'):
        file_access._repair_plan([link / source.name])
    leaf = tmp_path / 'codex_link.zip'
    leaf.symlink_to(source)
    with pytest.raises(ValueError, match='links and reparse points'):
        file_access._repair_plan([leaf])
    with pytest.raises(ValueError, match='regular staging file'):
        file_access.publish_file(leaf, tmp_path / 'output.zip')


# These tests exercise real NTFS DACLs on Windows CI, not mocked chmod behavior.
# Parent ACLs explicitly name the process user SID, so the test also works when
# the process token's default owner is Administrators (the reported failure).
_WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='Requires Windows NTFS ACLs')


def _current_user_sid() -> str:
    whoami = str(Path(file_access._icacls_executable()).with_name('whoami.exe'))
    result = subprocess.run([whoami, '/user', '/fo', 'csv', '/nh'],
                            capture_output=True, text=True, errors='replace', check=True)
    found = re.search(r'S-\d(?:-\d+)+', result.stdout)
    assert found, result.stdout
    return found.group()


def _set_protected_dacl(path: Path, sddl: str) -> None:
    from ctypes import wintypes

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        ptr, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ptr), ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ptr, ptr, ptr, ptr,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    descriptor, dacl = ptr(), ptr()
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None)
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        assert advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                ctypes.byref(dacl), ctypes.byref(defaulted))
        code = advapi.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000,
                                            None, None, dacl, None)
        if code:
            raise ctypes.WinError(code)
    finally:
        kernel.LocalFree(descriptor)


def _dacl(path: Path) -> str:
    return file_access._windows_security(path)['sddl'].split('D:', 1)[1]


def _effective_file_dacl(path: Path) -> str:
    """Ignore only OI/CI on regular-file ACEs, which have no child objects.

    icacls /reset can preserve these inheritance flags where ordinary creation
    removes them. Keep trustee, access mask, allow/deny type, ID/IO/other flags,
    ACE order, and DACL protection/auto-inheritance flags unchanged.
    """
    def normalize(match):
        fields = match.group(1).split(';')
        flags = fields[1]
        fields[1] = ''.join(flags[i:i + 2] for i in range(0, len(flags), 2)
                            if flags[i:i + 2] not in ('OI', 'CI'))
        return '(' + ';'.join(fields) + ')'

    assert path.is_file()
    return re.sub(r'\(([^()]*)\)', normalize, _dacl(path))


@pytest.fixture
def windows_acl_dirs(tmp_path):
    if os.name != 'nt':
        pytest.skip('Requires Windows NTFS ACLs')
    public = tmp_path / 'exports'
    public.mkdir()
    _set_protected_dacl(public, f'D:P(A;OICI;FA;;;{_current_user_sid()})(A;OICI;FA;;;SY)')
    private = Path(tempfile.mkdtemp(prefix='.private-', dir=tmp_path))
    # Precisely reproduce the OWNER RIGHTS security descriptor used by CPython
    # mkdir(0700); set it explicitly so Python 3.10 CI exercises the same case.
    _set_protected_dacl(private, 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)')
    return private, public


@_WINDOWS
def test_windows_published_archive_inherits_destination_acl(windows_acl_dirs):
    private, public = windows_acl_dirs
    source = private / 'staged.zip'
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_LZMA) as archive:
        archive.writestr('thread.md', '# Export\n\nReadable text.\n')
        archive.writestr('thread.manifest.json', '{"complete":true}\n')
        archive.writestr('thread.jsonl', '{"type":"event_msg"}\n')
    destination = public / 'codex_example.zip'
    control = public / 'control.txt'
    control.write_text('ordinary destination creation', encoding='utf-8')
    parent_before = file_access._windows_security(public)
    source_before = file_access._windows_security(source)
    assert _dacl(source) != _dacl(control)
    assert file_access.publish_file(source, destination) == 'verified_copy'
    assert _dacl(destination) == _dacl(control)
    security = file_access._windows_security(destination)
    assert not security['dacl_protected']
    assert security['explicit_aces'] == 0
    assert security['inherited_aces'] > 0
    assert source.stat().st_nlink == destination.stat().st_nlink == 1
    assert file_access._windows_security(source) == source_before
    assert file_access._windows_security(public) == parent_before
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert archive.read('thread.md').endswith(b'Readable text.\n')


@_WINDOWS
def test_windows_publication_avoids_all_hardlinks(windows_acl_dirs, monkeypatch):
    private, public = windows_acl_dirs
    source = private / 'staged.txt'
    source.write_text('review text', encoding='utf-8')

    def no_link(*args, **kwargs):
        pytest.fail('Windows publication must not retain a staging security descriptor')

    monkeypatch.setattr(file_access.os, 'link', no_link)
    assert file_access.publish_file(source, public / 'part-0001.txt') == 'verified_copy'


@_WINDOWS
def test_windows_repair_resets_only_selected_export_dacl(windows_acl_dirs):
    private, public = windows_acl_dirs
    source = private / 'staged.zip'
    source.write_bytes(b'existing exported bytes')
    destination = public / 'codex_old.zip'
    os.rename(source, destination)  # The old publication retained private ACLs.
    original = private / 'original.jsonl'
    original.write_text('{}\n', encoding='utf-8')
    control = public / 'control.txt'
    control.write_text('normal inherited ACL', encoding='utf-8')
    source_security = file_access._windows_security(original)
    parent_security = file_access._windows_security(public)
    before = file_access.diagnose_access(destination)
    assert _dacl(destination) != _dacl(control)
    reports = file_access.repair_export_access([destination])
    assert len(reports) == 1 and reports[0]['repaired']
    assert _effective_file_dacl(destination) == _effective_file_dacl(control)
    assert reports[0]['after']['security']['owner_sid'] == before['security']['owner_sid']
    assert destination.read_bytes() == b'existing exported bytes'
    assert file_access._windows_security(original) == source_security
    assert file_access._windows_security(public) == parent_security


@_WINDOWS
def test_windows_repair_old_flat_review_exports(windows_acl_dirs):
    private, public = windows_acl_dirs
    directory = public / 'codex_old.review'
    directory.mkdir()
    for name in ('START_HERE.txt', 'review-index.json', 'part-0001.txt'):
        staged = private / name
        staged.write_text('{}\n', encoding='utf-8')
        os.rename(staged, directory / name)
    control = directory / 'control.txt'
    control.write_text('normal inherited ACL', encoding='utf-8')
    expected = _effective_file_dacl(control)
    control.unlink()
    assert _effective_file_dacl(directory / 'part-0001.txt') != expected
    reports = file_access.repair_export_access([directory])
    assert len(reports) == 4
    assert all(report['repaired'] for report in reports)
    assert all(report['before']['security']['owner_sid'] ==
               report['after']['security']['owner_sid'] for report in reports)
    for item in directory.iterdir():
        assert _effective_file_dacl(item) == expected
        assert item.read_text(encoding='utf-8') == '{}\n'


def test_copy_progress_distinguishes_checksum_copy_and_readback(tmp_path, monkeypatch):
    import io
    import json
    from codex_exporter.progress import ProgressReporter

    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    source, destination = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    source.write_bytes(b'progress payload' * 100)
    output = io.StringIO()
    with ProgressReporter(style='json', stream=output) as reporter:
        assert file_access.publish_file(source, destination, reporter=reporter) == 'verified_copy'
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    completed = [event for event in events if event['event'] == 'stage_complete']
    assert [event['stage'] for event in completed] == [
        'Publish archive: source checksum', 'Publish archive: copy', 'Publish archive: verify',
    ]
    assert all(event['completed'] == event['total'] == source.stat().st_size for event in completed)


@pytest.mark.parametrize('mutate_source', [False, True])
def test_copy_accepts_stat_api_time_asymmetry_but_detects_changes(tmp_path, monkeypatch, mutate_source):
    from types import SimpleNamespace

    monkeypatch.setattr(file_access, 'hardlink_publication_allowed', lambda: False)
    original_fstat = os.fstat

    def differing_fstat(fd):
        result = original_fstat(fd)
        values = {field: getattr(result, field) for field in dir(result) if field.startswith('st_')}
        # Windows CPython can report birth time for path stat and metadata change
        # time for fstat. Simulate a stable API difference without altering IDs.
        values['st_ctime_ns'] += 123_000_000
        values['st_ctime'] += 0.123
        return SimpleNamespace(**values)

    monkeypatch.setattr(file_access.os, 'fstat', differing_fstat)
    source, output = tmp_path / 'staged.zip', tmp_path / 'codex_example.zip'
    raw = b'unchanged bytes must publish despite timestamp API asymmetry'
    source.write_bytes(raw)

    def progress(done, total):
        if mutate_source and not done:
            with source.open('ab') as changed:
                changed.write(b'changed')

    if mutate_source:
        with pytest.raises(RuntimeError, match='changed during publication'):
            file_access.publish_file(source, output, progress=progress)
        assert not output.exists()
        assert source.read_bytes() == raw + b'changed'
    else:
        assert file_access.publish_file(source, output, progress=progress) == 'verified_copy'
        assert source.read_bytes() == output.read_bytes() == raw
