"""Read-only ZIP diagnostics; also runnable as a standalone standard-library script.

This checks the *current process*, not a remote ChatGPT conversation. It never
extracts archive paths, executes members, changes files, or changes export defaults.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import struct
import sys
import time
import zipfile

CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_DICTIONARY_MIB = 512
_METHODS = {0: 'Store', 8: 'Deflate', 9: 'Deflate64', 12: 'BZip2', 14: 'LZMA',
            20: 'Zstandard (legacy ID)', 93: 'Zstandard', 99: 'AES encrypted'}


def runtime_capabilities() -> dict:
    """Report the reader that actually runs this check, without private env values."""
    supported = {0: True, 8: getattr(zipfile, 'zlib', None) is not None,
                 12: getattr(zipfile, 'bz2', None) is not None,
                 14: getattr(zipfile, 'lzma', None) is not None}
    if hasattr(zipfile, 'ZIP_ZSTANDARD'):
        supported[zipfile.ZIP_ZSTANDARD] = getattr(zipfile, 'zstd', None) is not None
    return {'scope': 'current_process_only', 'python_version': platform.python_version(),
            'python_executable': sys.executable, 'platform': platform.platform(),
            'working_directory': os.getcwd(),
            'address_bits': struct.calcsize('P') * 8,
            'zip_methods': {str(k): {'name': _METHODS.get(k, str(k)), 'available': v}
                            for k, v in supported.items()},
            'remote_chatgpt_compatibility': 'not_tested'}


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _error(exc: Exception, classification: str, phase: str) -> dict:
    result = {'classification': classification, 'phase': phase,
              'exception_type': type(exc).__name__, 'message': str(exc)}
    for key in ('errno', 'winerror'):
        value = getattr(exc, key, None)
        if value is not None:
            result[key] = value
    return result


def _exception_class(exc: Exception, *, phase: str) -> str:
    if phase == 'source_consistency' and isinstance(exc, OSError):
        return 'source_changed'
    if isinstance(exc, FileNotFoundError):
        return 'path_unavailable'
    if isinstance(exc, PermissionError):
        return 'access_denied'
    if isinstance(exc, MemoryError):
        return 'memory_exhausted'
    if isinstance(exc, NotImplementedError):
        return 'unsupported_compression'
    if isinstance(exc, zipfile.BadZipFile):
        return 'invalid_zip' if phase == 'inventory' else 'integrity_failed'
    if phase == 'open' and isinstance(exc, ValueError):
        return 'invalid_input'
    if isinstance(exc, (EOFError, ValueError, struct.error)):
        return 'invalid_zip' if phase != 'decode' else 'integrity_failed'
    if isinstance(exc, RuntimeError) and 'requires the' in str(exc):
        return 'decoder_unavailable'
    if isinstance(exc, RuntimeError) and 'password' in str(exc).lower():
        return 'encrypted_member'
    # LZMAError subclasses Exception rather than OSError in some runtimes.
    if type(exc).__name__ in ('LZMAError', 'error'):
        return 'decode_failed'
    return 'io_error' if isinstance(exc, OSError) else 'inspection_failed'


def _lzma_properties(stream, info: zipfile.ZipInfo, data_end_limit: int) -> dict:
    """Read the ZIP-LZMA envelope and its five-byte properties, without decoding."""
    position = stream.tell()
    try:
        stream.seek(info.header_offset)
        header = stream.read(30)
        if len(header) != 30 or header[:4] != b'PK\x03\x04':
            raise zipfile.BadZipFile('Invalid local header for LZMA member')
        fields = struct.unpack('<4s5H3I2H', header)
        local_flags, method = fields[2], fields[3]
        if method != zipfile.ZIP_LZMA or local_flags != info.flag_bits:
            raise zipfile.BadZipFile('Local and central LZMA headers disagree')
        data_offset = info.header_offset + 30 + fields[-2] + fields[-1]
        if (info.header_offset < 0 or data_offset + info.compress_size > data_end_limit
                or data_offset + 9 > data_end_limit):
            raise zipfile.BadZipFile('LZMA member overlaps another entry or the central directory')
        stream.seek(data_offset)
        envelope = stream.read(4)
        if len(envelope) != 4:
            raise zipfile.BadZipFile('Truncated ZIP-LZMA envelope')
        property_size = int.from_bytes(envelope[2:4], 'little')
        if property_size != 5 or info.compress_size < 4 + property_size:
            raise zipfile.BadZipFile('Invalid ZIP-LZMA properties length')
        properties = stream.read(property_size)
        if len(properties) != property_size or properties[0] >= 9 * 5 * 5:
            raise zipfile.BadZipFile('Invalid LZMA properties')
        value = properties[0]
        dictionary = int.from_bytes(properties[1:5], 'little')
        return {'encoder_version_bytes': list(envelope[:2]),
                'properties_hex': properties.hex(), 'dictionary_bytes': dictionary,
                'dictionary_mib': round(dictionary / (1024 * 1024), 6),
                'lc': value % 9, 'lp': (value // 9) % 5, 'pb': value // 45,
                'end_of_stream_marker': bool(info.flag_bits & 2),
                'note': 'Dictionary is declared by the stream. Encoder level, word size '
                        'and thread count cannot be recovered from these properties.'}
    finally:
        stream.seek(position)


def _path_issue(name: str) -> str | None:
    normalized = name.replace('\\', '/')
    parts = PurePosixPath(normalized).parts
    if normalized.startswith('/') or '..' in parts or any(':' in p for p in parts):
        return 'unsafe_to_extract_path'
    if '\x00' in name:
        return 'nul_in_name'
    return None


def diagnose_archive(path: Path | str, *, full: bool = True,
                     max_dictionary_mib: int = DEFAULT_MAX_DICTIONARY_MIB) -> dict:
    """Inspect and optionally stream-verify every member using fixed-size reads.

    The dictionary guard is an explicit policy, not a measured runtime memory
    limit. Actual decoder memory includes the dictionary and internal buffers.
    Reads request fixed-size chunks; zipfile/LZMA may buffer additional decoded
    bytes internally. No unbounded archive.read() or member execution is used.
    """
    if max_dictionary_mib < 1:
        raise ValueError('Maximum dictionary must be a positive MiB count')
    source = Path(os.path.abspath(Path(path).expanduser()))
    started = time.monotonic()
    report = {'path': str(source), 'ok': False, 'classification': 'not_checked',
              'check': 'full' if full else 'metadata', 'runtime': runtime_capabilities(),
              'policy': {'max_dictionary_mib': max_dictionary_mib,
                         'chunk_bytes': CHUNK_BYTES, 'extracts_files': False,
                         'memory_note': 'Chunk size is the caller read request, not a process memory cap; '
                                        'decoder dictionaries and internal buffers require additional memory.'},
              'members': [], 'errors': [], 'warnings': [],
              'archive_sha256': None, 'content_verified': False}
    phase = 'open'
    try:
        # Compare path stat to path stat and descriptor stat to descriptor stat.
        # Windows versions may expose differing ctime semantics across the APIs.
        path_before = source.stat()
        if not stat.S_ISREG(path_before.st_mode):
            raise ValueError('Expected an existing regular archive file')
        with source.open('rb') as physical:
            before = os.fstat(physical.fileno())
            if not os.path.samestat(path_before, before):
                phase = 'source_consistency'
                raise OSError('Archive identity changed before opening')
            report['archive_bytes'] = before.st_size
            report['magic_hex'] = physical.read(8).hex()
            if full:
                phase = 'archive_hash'
                physical.seek(0)
                digest = hashlib.sha256()
                for block in iter(lambda: physical.read(CHUNK_BYTES), b''):
                    digest.update(block)
                report['archive_sha256'] = digest.hexdigest()
            phase = 'inventory'
            physical.seek(0)
            with zipfile.ZipFile(physical, 'r') as archive:
                infos = archive.infolist()
                offsets = sorted({i.header_offset for i in infos} | {archive.start_dir})
                end_limits = {offset: min(offsets[index + 1], before.st_size)
                              for index, offset in enumerate(offsets[:-1])}
                names: set[str] = set()
                report['file_count'] = sum(not i.is_dir() for i in infos)
                report['directory_count'] = sum(i.is_dir() for i in infos)
                report['uncompressed_bytes'] = sum(i.file_size for i in infos)
                report['packed_member_bytes'] = sum(i.compress_size for i in infos)
                report['encrypted_member_count'] = sum(bool(i.flag_bits & 1) for i in infos)
                report['zip_comment_bytes'] = len(archive.comment)
                report['max_lzma_dictionary_bytes'] = 0
                for info in infos:
                    item = {'name': info.filename, 'directory': info.is_dir(),
                            'uncompressed_bytes': info.file_size, 'compressed_bytes': info.compress_size,
                            'method_id': info.compress_type,
                            'method': _METHODS.get(info.compress_type, 'Unknown'),
                            'flags': info.flag_bits, 'encrypted': bool(info.flag_bits & 1),
                            'extract_version': info.extract_version, 'host_system': info.create_system,
                            'expected_crc32': f'{info.CRC:08x}', 'verified': False}
                    report['members'].append(item)
                    issue = _path_issue(info.filename)
                    if issue:
                        report['warnings'].append({'member': info.filename, 'reason': issue})
                    if info.filename in names:
                        report['warnings'].append({'member': info.filename, 'reason': 'duplicate_name'})
                    names.add(info.filename)
                    if stat.S_ISLNK(info.external_attr >> 16):
                        report['warnings'].append({'member': info.filename, 'reason': 'symbolic_link_member'})
                    try:
                        phase = 'member_metadata'
                        if info.compress_type == zipfile.ZIP_LZMA and not item['encrypted']:
                            item['lzma'] = _lzma_properties(physical, info,
                                end_limits.get(info.header_offset, min(archive.start_dir, before.st_size)))
                            report['max_lzma_dictionary_bytes'] = max(
                                report['max_lzma_dictionary_bytes'], item['lzma']['dictionary_bytes'])
                        if not full:
                            continue
                        phase = 'decode'
                        if item['encrypted']:
                            item['error'] = {'classification': 'encrypted_member', 'phase': phase,
                                             'message': 'Password-protected entries are not decoded.'}
                        elif item.get('lzma', {}).get('dictionary_bytes', 0) > max_dictionary_mib * 1024 * 1024:
                            item['error'] = {'classification': 'dictionary_policy_limit', 'phase': phase,
                                             'message': 'Declared dictionary exceeds the explicit diagnostic guard; '
                                                        'this does not establish a decoder failure.'}
                        else:
                            digest, crc, size = hashlib.sha256(), 0, 0
                            with archive.open(info, 'r') as member:
                                for block in iter(lambda: member.read(CHUNK_BYTES), b''):
                                    digest.update(block)
                                    crc = binascii.crc32(block, crc)
                                    size += len(block)
                            if size != info.file_size or crc != info.CRC:
                                raise zipfile.BadZipFile('Decoded member size or CRC does not match its header')
                            item.update(verified=True, decoded_bytes=size,
                                        sha256=digest.hexdigest(), actual_crc32=f'{crc:08x}')
                    except (OSError, ValueError, EOFError, RuntimeError, NotImplementedError,
                            MemoryError, struct.error, zipfile.BadZipFile) as exc:
                        item['error'] = _error(exc, _exception_class(exc, phase=phase), phase)
                    except Exception as exc:
                        # Preserve concrete decoder exceptions without mislabelling absent paths.
                        item['error'] = _error(exc, _exception_class(exc, phase=phase), phase)
                    if 'error' in item:
                        report['errors'].append({'member': info.filename, **item['error']})
            phase = 'source_consistency'
            if (_signature(os.fstat(physical.fileno())) != _signature(before) or
                    _signature(source.stat()) != _signature(path_before)):
                report['errors'].append({'classification': 'source_changed', 'phase': phase,
                                         'message': 'Archive changed during inspection; repeat on a stable file.'})
            if not report['errors']:
                report.update(ok=True, classification='verified' if full else 'metadata_readable',
                              content_verified=full)
            else:
                report['classification'] = report['errors'][0]['classification']
    except Exception as exc:
        problem = _error(exc, _exception_class(exc, phase=phase), phase)
        report['errors'].append(problem)
        report['classification'] = problem['classification']
    if report['classification'] == 'path_unavailable':
        report['next_step'] = ('Locate or materialize the exact attachment in this same runtime and '
                               'verify its byte count. No decompression result is available.')
    elif report['classification'] in ('unsupported_compression', 'decoder_unavailable'):
        report['next_step'] = ('Use a reader with the declared ZIP method enabled. Reducing the LZMA '
                               'dictionary does not add missing method-14 support.')
    elif report['classification'] in ('dictionary_policy_limit', 'memory_exhausted'):
        report['next_step'] = ('Use one streaming decoder at a time with adequate memory. A configured '
                               'guard and an observed memory-allocation failure are different findings.')
    elif report['classification'] == 'source_changed':
        report['next_step'] = 'Repeat on a stable archive; file consistency changed after the initial open.'
    else:
        report['next_step'] = ('This result applies only to this process. A different conversation must '
                               'first demonstrate access to these same bytes before its reader can be tested.')
    report['elapsed_seconds'] = round(time.monotonic() - started, 6)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', type=Path, nargs='+')
    parser.add_argument('--metadata-only', action='store_true',
                        help='Read inventory/properties; skip archive hash and full member decoding')
    parser.add_argument('--max-dictionary-mib', type=int, default=DEFAULT_MAX_DICTIONARY_MIB)
    args = parser.parse_args(argv)
    if args.max_dictionary_mib < 1:
        parser.error('--max-dictionary-mib must be positive')
    results = [diagnose_archive(p, full=not args.metadata_only,
                               max_dictionary_mib=args.max_dictionary_mib) for p in args.archives]
    print(json.dumps(results[0] if len(results) == 1 else results, ensure_ascii=True, indent=2))
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
