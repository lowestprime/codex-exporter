"""Synthetic archive tests distinguish access, codec, policy, and integrity failures."""
from __future__ import annotations

import binascii
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import zipfile

import pytest

from codex_exporter import archive_diagnostics as diagnostics


HAS_LZMA = getattr(zipfile, 'lzma', None) is not None
lzma_required = pytest.mark.skipif(not HAS_LZMA, reason='Fixture needs the standard LZMA encoder')


def make_archive(tmp_path, *, method=zipfile.ZIP_STORED, members=None, name='sample.zip'):
    path = tmp_path / name
    members = members if members is not None else [('note.txt', b'Synthetic archive content\n')]
    with zipfile.ZipFile(path, 'w', compression=method) as archive:
        for member, content in members:
            archive.writestr(member, content)
    return path


def first_member_layout(path):
    """Locate fields in a small synthetic ZIP without depending on diagnostic helpers."""
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()[0]
        local = info.header_offset
        central = archive.start_dir
    name_size, extra_size = struct.unpack_from('<HH', data, local + 26)
    return data, local, local + 30 + name_size + extra_size, central


def mutate_method(path, method):
    data, local, _, central = first_member_layout(path)
    struct.pack_into('<H', data, local + 8, method)
    struct.pack_into('<H', data, central + 10, method)
    path.write_bytes(data)


def assert_failed(report, classification, phase=None):
    assert report['ok'] is False
    assert report['content_verified'] is False
    assert report['classification'] == classification
    assert report['errors']
    if phase is not None:
        assert report['errors'][0]['phase'] == phase


def test_missing_archive_is_access_failure_before_any_decoding(tmp_path):
    missing = tmp_path / 'missing attachment.zip'
    report = diagnostics.diagnose_archive(missing)
    assert_failed(report, 'path_unavailable', 'open')
    assert report['members'] == []
    assert report['archive_sha256'] is None
    assert report['errors'][0]['exception_type'] == 'FileNotFoundError'
    assert report['errors'][0]['errno'] == errno.ENOENT
    assert 'No decompression result' in report['next_step']


def test_permission_failure_preserves_os_error_without_codec_claim(tmp_path, monkeypatch):
    path = make_archive(tmp_path)
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(errno.EACCES, 'Synthetic denied read', str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', denied)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'access_denied', 'open')
    assert report['errors'][0]['errno'] == errno.EACCES
    assert report['archive_sha256'] is None
    assert report['members'] == []


def test_directory_is_invalid_input_not_a_missing_archive(tmp_path):
    report = diagnostics.diagnose_archive(tmp_path)
    assert_failed(report, 'invalid_input', 'open')
    assert report['errors'][0]['exception_type'] == 'ValueError'


def test_non_zip_bytes_are_invalid_after_access_and_hashing(tmp_path):
    path = tmp_path / 'not-a-zip.zip'
    content = b'This file exists, but it is ordinary text.\n'
    path.write_bytes(content)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'invalid_zip', 'inventory')
    assert report['archive_bytes'] == len(content)
    assert report['archive_sha256'] == hashlib.sha256(content).hexdigest()
    assert report['errors'][0]['exception_type'] == 'BadZipFile'


@pytest.mark.parametrize('method', [zipfile.ZIP_STORED,
    pytest.param(zipfile.ZIP_LZMA, marks=lzma_required)])
def test_streams_all_members_and_verifies_independent_hashes_and_crc(tmp_path, monkeypatch, method):
    payload = (b'ordinary synthetic data\n' * 100_000) + b'final bytes'
    records = [('large.txt', payload), ('folder/', b''), ('folder/empty.txt', b''),
               ('unicode-\u03b2.json', b'{"ok":true}\n')]
    path = make_archive(tmp_path, method=method, members=records)
    original_bytes = path.read_bytes()
    read_sizes = []
    original_read = zipfile.ZipExtFile.read

    def tracked_read(self, n=-1):
        read_sizes.append(n)
        return original_read(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, 'read', tracked_read)
    report = diagnostics.diagnose_archive(path)
    assert report['ok'] is True
    assert report['classification'] == 'verified'
    assert report['content_verified'] is True
    assert report['archive_sha256'] == hashlib.sha256(original_bytes).hexdigest()
    assert report['file_count'] == 3
    assert report['directory_count'] == 1
    assert report['uncompressed_bytes'] == sum(len(data) for _, data in records)
    assert len(report['members']) == len(records)
    assert report['errors'] == []
    for actual, (name, expected) in zip(report['members'], records):
        assert actual['name'] == name
        assert actual['verified'] is True
        assert actual['decoded_bytes'] == len(expected)
        assert actual['sha256'] == hashlib.sha256(expected).hexdigest()
        assert actual['actual_crc32'] == f'{binascii.crc32(expected):08x}'
        assert actual['expected_crc32'] == actual['actual_crc32']
        assert actual['method_id'] == method
    assert read_sizes and set(read_sizes) == {diagnostics.CHUNK_BYTES}
    assert path.read_bytes() == original_bytes
    assert set(tmp_path.iterdir()) == {path}
    assert report['runtime']['scope'] == 'current_process_only'
    assert report['runtime']['remote_chatgpt_compatibility'] == 'not_tested'
    assert report['policy']['extracts_files'] is False


@lzma_required
def test_lzma_properties_come_from_member_stream_not_a_configured_default(tmp_path):
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA)
    data, _, payload, _ = first_member_layout(path)
    expected_properties = bytes(data[payload + 4:payload + 9])
    expected_dictionary = int.from_bytes(expected_properties[1:5], 'little')
    report = diagnostics.diagnose_archive(path)
    member = report['members'][0]
    props = member['lzma']
    assert report['ok'] is True
    assert props['properties_hex'] == expected_properties.hex()
    assert props['dictionary_bytes'] == expected_dictionary
    assert props['lc'] == expected_properties[0] % 9
    assert props['lp'] == (expected_properties[0] // 9) % 5
    assert props['pb'] == expected_properties[0] // 45
    assert props['end_of_stream_marker'] == bool(member['flags'] & 2)
    assert report['max_lzma_dictionary_bytes'] == expected_dictionary
    assert 'cannot be recovered' in props['note']
    assert not {'level', 'word_size', 'threads'}.intersection(props)


@lzma_required
def test_declared_256_mib_dictionary_is_visible_without_allocating_decoder(tmp_path, monkeypatch):
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA)
    data, _, payload, _ = first_member_layout(path)
    struct.pack_into('<I', data, payload + 5, 256 * 1024 * 1024)
    path.write_bytes(data)

    def forbidden_decode(*args, **kwargs):
        pytest.fail('Metadata and dictionary-guard checks must not start a decoder')

    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden_decode)
    metadata = diagnostics.diagnose_archive(path, full=False, max_dictionary_mib=64)
    assert metadata['ok'] is True
    assert metadata['classification'] == 'metadata_readable'
    assert metadata['members'][0]['lzma']['dictionary_mib'] == 256.0
    assert metadata['content_verified'] is False
    assert metadata['archive_sha256'] is None
    assert metadata['members'][0]['verified'] is False
    guarded = diagnostics.diagnose_archive(path, max_dictionary_mib=64)
    assert_failed(guarded, 'dictionary_policy_limit', 'decode')
    assert 'does not establish a decoder failure' in guarded['errors'][0]['message']
    assert guarded['archive_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


@lzma_required
def test_metadata_only_never_reads_member_contents_or_claims_hash_verification(tmp_path, monkeypatch):
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA)

    def forbidden_decode(*args, **kwargs):
        pytest.fail('Metadata-only inspection must not read member content')

    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden_decode)
    report = diagnostics.diagnose_archive(path, full=False)
    assert report['ok'] is True
    assert report['classification'] == 'metadata_readable'
    assert report['content_verified'] is False
    assert report['archive_sha256'] is None
    assert report['members'][0]['verified'] is False
    assert 'sha256' not in report['members'][0]
    assert 'actual_crc32' not in report['members'][0]
    assert report['members'][0]['lzma']['dictionary_bytes'] > 0


def test_unknown_method_is_distinct_from_dictionary_policy_or_absent_file(tmp_path):
    path = make_archive(tmp_path)
    mutate_method(path, 222)
    metadata = diagnostics.diagnose_archive(path, full=False)
    assert metadata['ok'] is True
    assert metadata['content_verified'] is False
    assert metadata['members'][0]['method_id'] == 222
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'unsupported_compression', 'decode')
    assert report['errors'][0]['exception_type'] == 'NotImplementedError'
    assert 'does not add missing method-14 support' in report['next_step']


@lzma_required
def test_missing_lzma_module_is_decoder_unavailable(tmp_path, monkeypatch):
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA)
    monkeypatch.setattr(zipfile, 'lzma', None)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'decoder_unavailable', 'decode')
    assert report['members'][0]['lzma']['dictionary_bytes'] > 0
    assert report['runtime']['zip_methods']['14']['available'] is False


def test_observed_allocation_failure_is_distinct_from_dictionary_guard(tmp_path, monkeypatch):
    path = make_archive(tmp_path)

    def exhausted(*args, **kwargs):
        raise MemoryError('Synthetic decoder allocation failure')

    monkeypatch.setattr(zipfile.ZipFile, 'open', exhausted)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'memory_exhausted', 'decode')
    assert report['errors'][0]['exception_type'] == 'MemoryError'


@lzma_required
def test_decoder_error_preserves_concrete_lzma_exception(tmp_path, monkeypatch):
    import lzma
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA)

    def broken_decoder(*args, **kwargs):
        raise lzma.LZMAError('Synthetic invalid data stream')

    monkeypatch.setattr(zipfile.ZipFile, 'open', broken_decoder)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'decode_failed', 'decode')
    assert report['errors'][0]['exception_type'] == 'LZMAError'


def test_payload_corruption_fails_crc_but_metadata_is_still_only_metadata(tmp_path):
    path = make_archive(tmp_path)
    data, _, payload, _ = first_member_layout(path)
    data[payload] ^= 1
    path.write_bytes(data)
    metadata = diagnostics.diagnose_archive(path, full=False)
    assert metadata['classification'] == 'metadata_readable'
    assert metadata['content_verified'] is False
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'integrity_failed', 'decode')
    assert report['members'][0]['verified'] is False
    assert report['errors'][0]['exception_type'] == 'BadZipFile'


@lzma_required
@pytest.mark.parametrize('malformation', ['properties_length', 'properties_byte', 'local_method',
                                         'local_flags', 'central_overlap', 'next_member_overlap'])
def test_malformed_lzma_metadata_never_passes_as_verified(tmp_path, malformation):
    records = [('first.txt', b'first synthetic payload'), ('second.txt', b'second payload')]
    path = make_archive(tmp_path, method=zipfile.ZIP_LZMA, members=records)
    data, local, payload, central = first_member_layout(path)
    if malformation == 'properties_length':
        struct.pack_into('<H', data, payload + 2, 6)
    elif malformation == 'properties_byte':
        data[payload + 4] = 225
    elif malformation == 'local_method':
        struct.pack_into('<H', data, local + 8, zipfile.ZIP_STORED)
    elif malformation == 'local_flags':
        flags = struct.unpack_from('<H', data, local + 6)[0]
        struct.pack_into('<H', data, local + 6, flags ^ 4)
    elif malformation == 'central_overlap':
        struct.pack_into('<I', data, central + 20, len(data))
    else:
        with zipfile.ZipFile(path) as archive:
            next_local = archive.infolist()[1].header_offset
        struct.pack_into('<I', data, central + 20, next_local - payload + 1)
    path.write_bytes(data)
    report = diagnostics.diagnose_archive(path, full=False)
    assert_failed(report, 'integrity_failed', 'member_metadata')
    assert report['members'][0]['verified'] is False
    assert report['errors'][0]['exception_type'] == 'BadZipFile'
    if malformation.endswith('overlap'):
        assert 'overlaps' in report['errors'][0]['message']


def test_encrypted_member_is_reported_without_attempting_password_or_decoding(tmp_path, monkeypatch):
    path = make_archive(tmp_path)
    data, local, _, central = first_member_layout(path)
    struct.pack_into('<H', data, local + 6, struct.unpack_from('<H', data, local + 6)[0] | 1)
    struct.pack_into('<H', data, central + 8, struct.unpack_from('<H', data, central + 8)[0] | 1)
    path.write_bytes(data)

    def forbidden_decode(*args, **kwargs):
        pytest.fail('Encrypted-member classification must not try passwordless decoding')

    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden_decode)
    report = diagnostics.diagnose_archive(path)
    assert_failed(report, 'encrypted_member', 'decode')
    assert report['encrypted_member_count'] == 1
    assert report['members'][0]['encrypted'] is True


def test_path_warnings_do_not_extract_members_or_execute_contents(tmp_path):
    dangerous = [('../outside.txt', b'must remain inside ZIP'),
                 ('/absolute.txt', b'absolute'),
                 ('C:\\Windows\\synthetic.txt', b'Windows path'),
                 ('a\\..\\escape.txt', b'backslash traversal'),
                 ('run.py', b'raise RuntimeError("Never execute archive members")\n')]
    path = make_archive(tmp_path, members=dangerous)
    with zipfile.ZipFile(path, 'a') as archive:
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, '../target')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = diagnostics.diagnose_archive(path)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert report['ok'] is True
    assert before == after
    warnings = {(w['member'], w['reason']) for w in report['warnings']}
    assert all((name, 'unsafe_to_extract_path') in warnings for name, _ in dangerous[:4])
    assert ('link', 'symbolic_link_member') in warnings
    assert not (tmp_path.parent / 'outside.txt').exists()


def test_duplicate_members_are_each_verified_from_their_own_record(tmp_path):
    path = make_archive(tmp_path, members=[('duplicate.txt', b'first')])
    with pytest.warns(UserWarning, match='Duplicate name'):
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('duplicate.txt', b'second')
    report = diagnostics.diagnose_archive(path)
    assert report['ok'] is True
    assert [m['sha256'] for m in report['members']] == [
        hashlib.sha256(b'first').hexdigest(), hashlib.sha256(b'second').hexdigest()]
    assert {'member': 'duplicate.txt', 'reason': 'duplicate_name'} in report['warnings']


def test_archive_changed_during_member_read_is_not_certified(tmp_path, monkeypatch):
    path = make_archive(tmp_path)
    before = path.stat()
    original_open = zipfile.ZipFile.open
    changed = False

    def open_with_changed_source(self, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'open', open_with_changed_source)
    report = diagnostics.diagnose_archive(path)
    assert changed
    assert_failed(report, 'source_changed', 'source_consistency')
    assert report['members'][0]['verified'] is True
    assert report['content_verified'] is False


def test_disappeared_path_after_read_is_source_changed_not_initial_missing_path(tmp_path, monkeypatch):
    path = make_archive(tmp_path)
    original_stat = Path.stat
    matching_calls = 0

    def vanishing_stat(self, *args, **kwargs):
        nonlocal matching_calls
        if self == path:
            matching_calls += 1
            if matching_calls >= 2:
                raise FileNotFoundError(errno.ENOENT, 'Synthetic removal after decoding', str(self))
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', vanishing_stat)
    report = diagnostics.diagnose_archive(path)
    assert matching_calls == 2
    assert_failed(report, 'source_changed', 'source_consistency')
    assert report['members'][0]['verified'] is True
    assert report['errors'][0]['exception_type'] == 'FileNotFoundError'
    assert report['archive_sha256'] is not None


@pytest.mark.parametrize('limit', [0, -1])
def test_dictionary_policy_rejects_nonpositive_limits(tmp_path, limit):
    with pytest.raises(ValueError, match='positive'):
        diagnostics.diagnose_archive(tmp_path / 'missing.zip', max_dictionary_mib=limit)


def test_main_returns_json_receipts_and_failure_for_mixed_paths(tmp_path, capsys):
    path = make_archive(tmp_path)
    missing = tmp_path / 'missing.zip'
    result = diagnostics.main([str(path), str(missing)])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == ''
    receipts = json.loads(captured.out)
    assert len(receipts) == 2
    assert receipts[0]['classification'] == 'verified'
    assert receipts[1]['classification'] == 'path_unavailable'


def test_main_single_metadata_result_does_not_claim_content_verification(tmp_path, capsys):
    path = make_archive(tmp_path)
    result = diagnostics.main([str(path), '--metadata-only', '--max-dictionary-mib', '64'])
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert isinstance(report, dict)
    assert report['classification'] == 'metadata_readable'
    assert report['content_verified'] is False
    assert report['policy']['max_dictionary_mib'] == 64


def test_main_rejects_invalid_policy_before_running(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        diagnostics.main([str(tmp_path / 'sample.zip'), '--max-dictionary-mib', '0'])
    assert exc.value.code == 2
    assert 'must be positive' in capsys.readouterr().err


def test_standard_library_script_runs_without_package_installation(tmp_path):
    path = make_archive(tmp_path, name='archive with spaces \u03b2.zip')
    script = Path(diagnostics.__file__).resolve()
    completed = subprocess.run([sys.executable, '-I', '-S', str(script), str(path)],
                               cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report['classification'] == 'verified'
    assert report['runtime']['remote_chatgpt_compatibility'] == 'not_tested'
    assert report['archive_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_standalone_json_receipt_survives_non_utf8_output_pipe(tmp_path):
    path = make_archive(tmp_path, members=[('unicode-\u03b2.txt', b'synthetic content')])
    script = Path(diagnostics.__file__).resolve()
    environment = {**os.environ, 'PYTHONIOENCODING': 'ascii'}
    completed = subprocess.run([sys.executable, '-S', str(script), str(path)],
                               cwd=tmp_path, env=environment, capture_output=True,
                               text=True, encoding='ascii', timeout=20)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report['content_verified'] is True
    assert report['members'][0]['name'] == 'unicode-\u03b2.txt'
