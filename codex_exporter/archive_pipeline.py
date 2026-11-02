"""Safe ZIP LZMA archive stage for Codex session exports.

No app-owned file is changed. Loose exports are removed only after full content-
hash verification of all members. Never invokes a GUI or shell command.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


@dataclass(frozen=True)
class ArchiveSettings:
    copy_source: bool = True
    archive: bool = True
    cleanup: bool = True
    backend: str = 'auto'
    level: int = 9
    method: str = 'LZMA'
    dictionary: str = '256m'
    word_size: int = 64
    threads: int = 19
    archiver: str = ''

    def validate(self) -> None:
        if not (1 <= self.level <= 9):
            raise ValueError('archive level must be 1..9')
        if self.method.upper() not in ('LZMA', 'DEFLATE'):
            raise ValueError('archive method must be LZMA or Deflate')
        if not (5 <= self.word_size <= 273):
            raise ValueError('archive word size must be 5..273')
        if not (1 <= self.threads <= 128):
            raise ValueError('archive threads must be 1..128')
        if not __import__('re').fullmatch(r'[1-9][0-9]*[kKmMgG]', self.dictionary):
            raise ValueError('archive dictionary must be like 256m')
        if not self.archive and self.cleanup:
            # Cleanup is intentionally ineffective when archiving is disabled.
            return


def find_archiver(explicit: str = '') -> Path:
    """Prefer installed compatible CLI backends; never assume a GUI executable is a CLI."""
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            p2 = shutil.which(explicit)
            if p2:
                p = Path(p2)
        if not p.is_file():
            raise FileNotFoundError(f'7-Zip-compatible archiver not found: {explicit}')
        return p.resolve()
    for exe in ('7z', '7zz', '7za', '7z.exe', '7zz.exe', '7za.exe', '7zG.exe', 'NanaZipC.exe'):
        loc = shutil.which(exe)
        if loc and exe.lower() != '7zg.exe':
            candidates.append(Path(loc))
    program_files = [os.environ.get('ProgramFiles', ''), os.environ.get('ProgramW6432', ''),
                     os.environ.get('ProgramFiles(x86)', ''), os.environ.get('LOCALAPPDATA', '')]
    suffixes = ('7-Zip/7z.exe', '7-Zip-Zstandard/7z.exe', '7-Zip-ZS/7z.exe',
                'PeaZip/res/bin/7z/7z.exe', 'PeaZip/res/bin/7z/7z64/7z.exe',
                'Programs/PeaZip/res/bin/7z/7z.exe')
    for root in filter(None, program_files):
        candidates.extend(Path(root) / x for x in suffixes)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError('No 7-Zip-compatible CLI found. Supply --archiver-path PATH; '
                            'PeaZip/NanaZip GUI executables alone are not sufficient.')


def _copy_decoded(source: Path, dest: Path) -> tuple[str, int]:
    """Bounded-memory decode of .jsonl.zst or copy of plain .jsonl."""
    h = hashlib.sha256()
    n = 0
    with source.open('rb') as physical:
        if source.name.lower().endswith('.jsonl.zst'):
            try:
                import zstandard as zstd
            except ImportError as exc:
                raise RuntimeError('Install the optional zstandard dependency for .jsonl.zst') from exc
            decoded = zstd.ZstdDecompressor().stream_reader(physical, read_across_frames=True, closefd=False)
        else:
            decoded = physical
        try:
            with dest.open('xb') as target:
                for chunk in iter(lambda: decoded.read(1024 * 1024), b''):
                    target.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if decoded is not physical:
                decoded.close()
    return h.hexdigest(), n


def _verify_members(archive: Path, expected: dict[str, Path], method: str) -> dict[str, str]:
    """Independently verify full ZIP members, accepting only safe Store fallback.

    7-Zip can choose ZIP Store (method 0) for inputs that do not benefit from
    compression, even when -mm=LZMA is requested. Requiring method 14 for each
    member rejects valid archives with tiny manifest files. We still reject any
    unrequested compression algorithm, enforce the exact flat file inventory,
    and SHA-256-verify the fully decoded bytes against each loose source file.
    """
    requested = zipfile.ZIP_LZMA if method.upper() == 'LZMA' else zipfile.ZIP_DEFLATED
    allowed = {requested, zipfile.ZIP_STORED}
    methods = {zipfile.ZIP_STORED: 'Store', zipfile.ZIP_LZMA: 'LZMA',
               zipfile.ZIP_DEFLATED: 'Deflate'}
    with zipfile.ZipFile(archive, 'r', allowZip64=True) as z:
        infos = z.infolist()
        if (len(infos) != len(expected) or len({i.filename for i in infos}) != len(infos)
                or {i.filename for i in infos} != set(expected)):
            raise RuntimeError('ZIP member inventory mismatch: expected only distinct flat export files')
        if any(i.is_dir() or '/' in i.filename or '\\' in i.filename or
               i.flag_bits & 1 or i.compress_type not in allowed for i in infos):
            raise RuntimeError('ZIP member path, encryption or compression method mismatch')
        # The three tiny inputs in the synthetic probe can all be stored. For
        # substantive exports, if *none* use the requested method, reject the
        # archive rather than silently call a full Store ZIP an LZMA ZIP.
        if (all(i.compress_type == zipfile.ZIP_STORED for i in infos)
                and sum(i.file_size for i in infos) >= 64 * 1024):
            raise RuntimeError('Substantive ZIP uses Store for every member; '
                               'requested compression method was not demonstrated')
        for info in infos:
            digest = hashlib.sha256()
            size = 0
            with z.open(info, 'r') as entry:
                for block in iter(lambda: entry.read(1024 * 1024), b''):
                    digest.update(block)
                    size += len(block)
            original_hash, original_size = _hash_file(expected[info.filename])
            if digest.hexdigest() != original_hash or size != original_size:
                raise RuntimeError(f'Archive content mismatch: {info.filename}')
        return {info.filename: methods[info.compress_type] for info in infos}



def avoid_existing_archive(md_path: Path, collision: str) -> Path:
    """Reserve matching basename on retries when an earlier run removed loose files.

    Never overwrite an existing ZIP, even when --collision overwrite is selected.
    """
    if not md_path.with_suffix('.zip').exists():
        return md_path
    if collision != 'rename':
        raise FileExistsError('An archive already exists for this export basename: '
                              + str(md_path.with_suffix('.zip')))
    n = 1
    while True:
        candidate = md_path.with_name(f'{md_path.stem}-{n}{md_path.suffix}')
        if not candidate.exists() and not candidate.with_suffix('.zip').exists():
            return candidate
        n += 1

def package_export(result: dict[str, Any], original_source: Path,
                   settings: ArchiveSettings) -> dict[str, Any]:
    """Copy source; build/test a new flat ZIP; only then unlink three loose outputs.

    On an exception, originals are never touched, existing exports are kept,
    and a staging ZIP is discarded. No ZIP is ever silently overwritten.
    """
    settings.validate()
    if not settings.copy_source and not settings.archive:
        return result
    if result.get('source_changed_during_export'):
        raise RuntimeError('Codex source changed during export; refusing archival cleanup')
    if not result.get('file'):
        return result
    md = Path(result['file']).resolve()
    if not md.is_file():
        raise FileNotFoundError(f'Exported Markdown missing: {md}')
    manifest_value = result.get('manifest')
    manifest = Path(manifest_value).resolve() if manifest_value else None
    if settings.archive and (not manifest or not manifest.is_file()):
        raise RuntimeError('ZIP packaging requires an exported manifest; use --no-archive with --no-manifest')
    members: dict[str, Path] = {md.name: md}
    if manifest and manifest.is_file():
        if manifest.parent != md.parent:
            raise RuntimeError('Export manifest must be in Markdown output directory for flat ZIP')
        members[manifest.name] = manifest
    source_copy: Path | None = None
    # Conservative free-space preflight for the additional loose source copy.
    # For compressed sources, the streaming manifest supplies the decoded size.
    if settings.copy_source:
        expected_bytes = original_source.stat().st_size
        if original_source.name.lower().endswith('.jsonl.zst'):
            try:
                detail = __import__('json').loads(manifest.read_text(encoding='utf-8')) if manifest else {}
                expected_bytes = int(detail.get('decoded_source_bytes') or expected_bytes)
            except (ValueError, OSError, TypeError):
                pass
        if shutil.disk_usage(md.parent).free < expected_bytes + 8 * 1024 * 1024:
            raise OSError('Insufficient free disk space to stage decoded source copy')
    source_stat_before_copy = original_source.stat()
    # Classic .zst manifests identify the physical compressed bytes; streamed
    # sources identify the decoded bytes. Hash the physical file only when
    # needed for that classic compressed-manifest check.
    if (original_source.name.lower().endswith('.jsonl.zst')
            and 'source_lines' not in result and result.get('source_sha256')
            and _hash_file(original_source)[0] != result['source_sha256']):
        raise RuntimeError('Compressed Codex source changed since classic export')
    if settings.copy_source:
        source_copy = md.with_suffix('.jsonl')
        if source_copy.exists():
            raise FileExistsError(f'Refusing to overwrite source copy: {source_copy}')
        try:
            decoded_sha, decoded_bytes = _copy_decoded(original_source, source_copy)
            if not source_copy.is_file() or decoded_bytes == 0:
                raise RuntimeError('Copied source was empty')
            if result.get('source_sha256') and (not original_source.name.endswith('.zst') or 'source_lines' in result) and decoded_sha != result['source_sha256']:
                raise RuntimeError('Source changed since export; copy hash does not match transcript source')
            # Streaming exports hash decoded bytes; classic compressed exports may hash physical .zst bytes.
            if original_source.name.endswith('.zst') and result.get('decoded_source_bytes') is not None:
                if decoded_bytes != int(result['decoded_source_bytes']):
                    raise RuntimeError('Decoded source byte count mismatch')
                if result.get('source_sha256') and decoded_sha != result['source_sha256']:
                    raise RuntimeError('Decoded source SHA-256 mismatch')
            after_copy = original_source.stat()
            if (after_copy.st_size, after_copy.st_mtime_ns) != (source_stat_before_copy.st_size, source_stat_before_copy.st_mtime_ns):
                raise RuntimeError('Codex rollout changed during source copy; no cleanup performed')
            members[source_copy.name] = source_copy
        except Exception:
            # Preserve the copied bytes for diagnosis, rather than assert success.
            raise
        result['source_copy'] = str(source_copy)
        result['source_copy_sha256'] = decoded_sha
        result['source_copy_bytes'] = decoded_bytes
    if not settings.archive:
        return result
    if any(p.parent != md.parent for p in members.values()):
        raise RuntimeError('All ZIP members must reside in one output directory')
    archive = md.with_suffix('.zip')
    # Record intended archive provenance INSIDE its manifest before packaging.
    # Verified extraction still compares every member byte-for-byte against the
    # loose files. No external provenance document is required to interpret ZIP.
    if manifest is not None:
        details = json.loads(manifest.read_text(encoding='utf-8'))
        details['archive_plan'] = {
            'format': 'zip', 'method': settings.method, 'level': settings.level,
            'dictionary': settings.dictionary if settings.method.upper() == 'LZMA' else None,
            'word_size': settings.word_size if settings.method.upper() == 'LZMA' else None,
            'threads': settings.threads,
            'archive_filename': archive.name, 'members': sorted(members),
            'source_copy_sha256': result.get('source_copy_sha256'),
            'source_copy_bytes': result.get('source_copy_bytes'),
            'cleanup_after_full_archive_verification': settings.cleanup,
        }
        with manifest.open('w', encoding='utf-8', newline='\n') as f:
            json.dump(details, f, indent=2, ensure_ascii=False)
            f.write('\n')
    result['archive_settings'] = {
        'method': settings.method, 'level': settings.level,
        'dictionary': settings.dictionary, 'word_size': settings.word_size,
        'threads': settings.threads,
    }
    if archive.exists():
        raise FileExistsError(f'Refusing to replace an existing ZIP: {archive}')
    archiver = find_archiver(settings.archiver)
    # Generate a unique staging ZIP name *ending in .zip* for 7-Zip format recognition.
    handle, staging_name = tempfile.mkstemp(prefix=f'.{md.stem}-', suffix='.zip', dir=md.parent)
    os.close(handle)
    staged = Path(staging_name)
    staged.unlink()  # 7-Zip must create a fresh ZIP, never update one.
    try:
        cmd = [str(archiver), 'a', '-tzip', '-mx=' + str(settings.level),
               '-mm=' + settings.method, '-mmt=' + str(settings.threads),
               '-y', '-bd', '-bb0']
        if settings.method.upper() == 'LZMA':
            cmd.extend(['-md=' + settings.dictionary, '-mfb=' + str(settings.word_size)])
        cmd.extend([str(staged), *members.keys()])
        run = subprocess.run(cmd, cwd=md.parent, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
        if run.returncode != 0:
            raise RuntimeError(f'Archiver error {run.returncode} ({archiver}): '
                               + (run.stderr or run.stdout)[-1600:])
        member_methods = _verify_members(staged, members, settings.method)
        # A recheck before cleanup catches concurrent writes to exported files.
        if archive.exists():
            raise FileExistsError(f'Archive appeared while packaging: {archive}')
        # Exclusive create: never replace an archive produced by another process.
        os.link(staged, archive)
        staged.unlink()
    finally:
        staged.unlink(missing_ok=True)
    result['archive'] = str(archive)
    result['archive_verified'] = True
    result['archiver'] = str(archiver)
    result['archive_method'] = settings.method
    result['archive_members'] = sorted(members)
    result['archive_member_methods'] = member_methods
    result['archive_used_store_fallback'] = ('Store' in member_methods.values())
    result['archived_markdown'] = md.name
    result['archived_manifest'] = manifest.name if manifest else None
    result['archived_source'] = source_copy.name if source_copy else None
    result['loose_files_removed'] = False
    if settings.cleanup:
        # Exact exported paths only. Never remove app-owned source or unknown-event sidecars.
        for file in members.values():
            file.unlink()
        result['loose_files_removed'] = True
        result['file'] = None
        result['manifest'] = None
        result['source_copy'] = None
    return result
