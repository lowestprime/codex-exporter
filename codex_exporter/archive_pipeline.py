"""Portable, verified ZIP archives for Codex session exports.

No app-owned file is changed. Loose exports are removed only after full content-
hash verification of all members. Never invokes a GUI or shell command.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING
import hashlib
import json
import os
import shutil
import tempfile
import zipfile

if TYPE_CHECKING:
    from .snapshot import SourceSnapshot


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
    review: bool = False
    review_chunk_bytes: int = 1_000_000
    max_bytes: int = 512_000_000
    work_dir: str = ''

    def validate(self) -> None:
        from .review import _validate_chunk_bytes
        from .bounded_archive import validate_max_bytes
        validate_max_bytes(self.max_bytes)
        if not isinstance(self.work_dir, (str, Path)):
            raise ValueError('archive work_dir must be a directory path or empty for the output volume')
        if not isinstance(self.review, bool):
            raise ValueError('review must be a boolean')
        _validate_chunk_bytes(self.review_chunk_bytes)
        if self.backend not in ('auto', 'python', '7zip'):
            raise ValueError('archive backend must be auto, python or 7zip')
        if self.backend == 'python' and self.method.upper() != 'DEFLATE':
            raise ValueError('Python archive backend supports Deflate; use auto or 7zip for LZMA')
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


    def effective_backend(self) -> str:
        """Resolve the validated backend for preflight and packaging alike."""
        self.validate()
        if self.backend == 'auto':
            return '7zip' if self.method.upper() == 'LZMA' or self.archiver else 'python'
        return self.backend


def _file_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _check_member_signatures(members: dict[str, Path], expected: dict[str, tuple]) -> None:
    """Retain loose files if a detected concurrent change invalidates verification."""
    for name, path in members.items():
        try:
            unchanged = _file_signature(path) == expected[name]
        except OSError:
            unchanged = False
        if not unchanged:
            raise RuntimeError(f'Exported member changed during packaging: {name}; no cleanup performed')


def _publish_archive(staged: Path, destination: Path, *, reporter=None) -> str:
    """Publish exclusively with the destination folder's Windows permissions."""
    from .file_access import publish_file
    return publish_file(staged, destination, reporter=reporter, stage_label='Publish archive')


def _write_python_archive(staged: Path, members: dict[str, Path], level: int, *, reporter=None) -> None:
    """ZIP method 8 with measured input-byte progress and bounded memory."""
    from .progress import ProgressReporter
    reporter = reporter or ProgressReporter(enabled=False)
    total = sum(path.stat().st_size for path in members.values())
    with reporter.stage('Compress archive', total=total, detail=f'Deflate level {level}; input bytes') as meter:
        with zipfile.ZipFile(staged, 'x', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True, strict_timestamps=False) as archive:
            for number, (name, path) in enumerate(members.items(), 1):
                info = zipfile.ZipInfo.from_file(path, arcname=name, strict_timestamps=False)
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = level
                meter.update(detail=f'{number}/{len(members)}: {name}')
                with path.open('rb') as source, archive.open(info, 'w', force_zip64=info.file_size >= zipfile.ZIP64_LIMIT) as item:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        item.write(block)
                        meter.advance(len(block))


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


def _copy_decoded(source: Path, dest: Path, *, meter=None) -> tuple[str, int]:
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
                    if meter is not None:
                        meter.update(n)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if decoded is not physical:
                decoded.close()
    return h.hexdigest(), n


def _verify_members(archive: Path, expected: dict[str, Path], method: str, *, reporter=None,
                    checksums: dict[str, tuple[str, int]] | None = None) -> dict[str, str]:
    """Independently verify full ZIP members, accepting only safe Store fallback.

    7-Zip can choose ZIP Store (method 0) for inputs that do not benefit from
    compression, even when -mm=LZMA is requested. Requiring method 14 for each
    member rejects valid archives with tiny manifest files. We still reject any
    unrequested compression algorithm, enforce the exact flat file inventory,
    and SHA-256-verify the fully decoded bytes against each loose source file.
    """
    from .progress import ProgressReporter
    reporter = reporter or ProgressReporter(enabled=False)
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
        total = sum(info.file_size for info in infos) * 2
        with reporter.stage('Verify archive I/O (two passes)', total=total,
                            detail='Processed bytes count ZIP decoding plus source reads; not export size') as meter:
            for number, info in enumerate(infos, 1):
                digest = hashlib.sha256()
                size = 0
                meter.update(detail=f'{number}/{len(infos)}: decode {info.filename}')
                with z.open(info, 'r') as entry:
                    for block in iter(lambda: entry.read(1024 * 1024), b''):
                        digest.update(block)
                        size += len(block)
                        meter.advance(len(block))
                original = hashlib.sha256()
                original_size = 0
                meter.update(detail=f'{number}/{len(infos)}: compare {info.filename}')
                with expected[info.filename].open('rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        original.update(block)
                        original_size += len(block)
                        meter.advance(len(block))
                if digest.hexdigest() != original.hexdigest() or size != original_size:
                    raise RuntimeError(f'Archive content mismatch: {info.filename}')
                if checksums is not None:
                    checksums[info.filename] = (digest.hexdigest(), size)
        return {info.filename: methods[info.compress_type] for info in infos}



def avoid_existing_archive(md_path: Path, collision: str) -> Path:
    """Reserve matching basename on retries when an earlier run removed loose files.

    Never overwrite an existing ZIP, even when --collision overwrite is selected.
    """
    def occupied(path: Path) -> bool:
        import glob
        from .bounded_archive import archive_stem_digest
        return (path.with_suffix('.zip').exists() or
                any(path.parent.glob(glob.escape(path.stem) + '.part*-of-*.zip')) or
                any(path.parent.glob('*-' + archive_stem_digest(path) + '.part*-of-*.zip')))
    if not occupied(md_path):
        return md_path
    if collision != 'rename':
        raise FileExistsError('An archive already exists for this export basename: '
                              + str(md_path.with_suffix('.zip')))
    n = 1
    while True:
        candidate = md_path.with_name(f'{md_path.stem}-{n}{md_path.suffix}')
        if not candidate.exists() and not occupied(candidate):
            return candidate
        n += 1

def _stage_snapshot_copy(snapshot: SourceSnapshot, destination: Path, *, reporter=None,
                         private: bool = False) -> tuple[str, int, str]:
    """Promote only an owned snapshot; Windows creates a fresh inherited-DACL file."""
    from .file_access import publish_file
    from .progress import ProgressReporter
    from .snapshot import SourceSnapshot
    if not isinstance(snapshot, SourceSnapshot) or not snapshot.owns_staged_path():
        raise ValueError('Source-copy promotion requires an owned private snapshot')
    reporter = reporter or ProgressReporter(enabled=False)
    before = snapshot.path.stat()
    with reporter.stage('Check captured source', total=snapshot.size_bytes) as meter:
        state = hashlib.sha256()
        size = 0
        with snapshot.path.open('rb') as source:
            for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                state.update(block)
                size += len(block)
                meter.update(size)
        digest = state.hexdigest()
    if (digest, size) != (snapshot.sha256, snapshot.size_bytes):
        raise RuntimeError('Snapshot content changed before source-copy staging')
    publication = None
    if private:
        try:
            os.link(snapshot.path, destination)
            publication = 'hardlink'
        except FileExistsError:
            raise
        except OSError:
            pass
    if publication is None:
        from .storage import require_space
        if os.name == 'nt' or private:
            require_space(destination.parent, size, stage='stage native JSONL copy')
        publication = publish_file(snapshot.path, destination, reporter=reporter, stage_label='Stage native JSONL')
    after = snapshot.path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise RuntimeError('Snapshot changed during source-copy staging; no cleanup performed')
    return digest, size, 'snapshot_hardlink' if publication == 'hardlink' else 'snapshot_copy'



def _compress_archive(staged: Path, members: dict[str, Path], settings: ArchiveSettings, *,
                      backend: str, archiver: Path | None, reporter=None,
                      max_bytes: int | None = None) -> None:
    """Create a private flat ZIP with the exact requested codec configuration."""
    from .progress import ProgressReporter, run_seven_zip, SEVEN_ZIP_PROGRESS_ARGS
    reporter = reporter or ProgressReporter(enabled=False)
    if backend == 'python':
        _write_python_archive(staged, members, settings.level, reporter=reporter)
        return
    parents = {path.parent for path in members.values()}
    if len(parents) != 1 or any(name != path.name for name, path in members.items()):
        raise ValueError('External archiver inputs must be flat files in one private directory')
    command = [str(archiver), 'a', '-tzip', '-mx=' + str(settings.level),
               '-mm=' + settings.method, '-mmt=' + str(settings.threads),
               '-y', *SEVEN_ZIP_PROGRESS_ARGS]
    if settings.method.upper() == 'LZMA':
        command.extend(['-md=' + settings.dictionary, '-mfb=' + str(settings.word_size)])
    command.extend([str(staged), *members.keys()])
    with reporter.stage('Compress archive', unit='percent',
                        detail=f'{settings.method} level {settings.level}; '
                               f'{len(members)} files; requested threads {settings.threads}') as meter:
        def monitor() -> None:
            if max_bytes is not None and staged.exists() and staged.stat().st_size > max_bytes:
                from .archive_stream import RangeTooLarge
                raise RangeTooLarge(0, staged.stat().st_size, max_bytes)
        run = run_seven_zip(command, cwd=next(iter(parents)), progress=meter,
                            on_tick=monitor if max_bytes is not None else None)
        if run.returncode != 0:
            raise RuntimeError(f'Archiver error {run.returncode} ({archiver}): '
                               + (run.stderr or run.stdout)[-1600:])


def _package_export_impl(result: dict[str, Any], original_source: Path,
                   settings: ArchiveSettings, *, snapshot: SourceSnapshot | None = None,
                   review: bool | None = None, review_chunk_bytes: int | None = None,
                   reporter=None, destination: Path | None = None,
                   private_output: bool = False) -> dict[str, Any]:
    """Copy source; build/test a new flat ZIP; only then unlink three loose outputs.

    On an exception, originals are never touched, existing exports are kept,
    and a staging ZIP is discarded. No ZIP is ever silently overwritten.
    """
    from .progress import ProgressReporter, run_seven_zip, SEVEN_ZIP_PROGRESS_ARGS
    reporter = reporter or ProgressReporter(enabled=False)
    review = settings.review if review is None else review
    review_chunk_bytes = settings.review_chunk_bytes if review_chunk_bytes is None else review_chunk_bytes

    def add_review() -> None:
        if review and result.get('file') and not result.get('skipped'):
            from .review import create_review_bundle
            result.update(create_review_bundle(Path(result['file']),
                Path(result['manifest']) if result.get('manifest') else None,
                chunk_bytes=review_chunk_bytes, progress=reporter, archive=False))

    settings.validate()
    if not settings.copy_source and not settings.archive:
        add_review()
        return result
    if result.get('source_changed_during_export'):
        raise RuntimeError('Codex source changed during export; refusing archival cleanup')
    if snapshot is not None:
        from .snapshot import SourceSnapshot
        if (not isinstance(snapshot, SourceSnapshot) or not snapshot.owns_staged_path()
                or original_source.resolve() != snapshot.path.resolve()
                or result.get('source_sha256') != snapshot.sha256):
            raise ValueError('Archive input must match the owned source snapshot and its rendered digest')
    if not result.get('file'):
        return result
    md = Path(result['file']).resolve()
    if not md.is_file():
        raise FileNotFoundError(f'Exported Markdown missing: {md}')
    manifest_value = result.get('manifest')
    manifest = Path(manifest_value).resolve() if manifest_value else None
    if settings.archive and (not manifest or not manifest.is_file()):
        raise RuntimeError('ZIP packaging requires an exported manifest; use --no-archive with --no-manifest')
    from .file_access import validate_export_paths
    outputs = {'Markdown': md}
    unknown_sidecar = Path(result['unknown_sidecar']).resolve() if result.get('unknown_sidecar') else None
    if unknown_sidecar is not None:
        if not unknown_sidecar.is_file():
            raise FileNotFoundError(f'Unknown-event sidecar missing: {unknown_sidecar}')
        outputs['unknown-event sidecar'] = unknown_sidecar
    if manifest is not None:
        outputs['manifest'] = manifest
    if settings.copy_source:
        outputs['native JSONL'] = md.with_suffix('.jsonl')
    if settings.archive:
        outputs['archive'] = Path(destination).resolve() if destination is not None else md.with_suffix('.zip')
    validate_export_paths(outputs, [original_source, *([snapshot.original_path] if snapshot else [])])
    members: dict[str, Path] = {md.name: md}
    if manifest and manifest.is_file():
        if manifest.parent != md.parent:
            raise RuntimeError('Export manifest must be in Markdown output directory for flat ZIP')
        members[manifest.name] = manifest
    if unknown_sidecar is not None:
        if unknown_sidecar.parent != md.parent or unknown_sidecar.name in members:
            raise RuntimeError('Unknown-event sidecar must be a distinct flat export file')
        members[unknown_sidecar.name] = unknown_sidecar
    source_copy: Path | None = None
    borrowed_snapshot = False
    if snapshot is not None:
        if settings.copy_source:
            source_copy = md.with_suffix('.jsonl')
            if settings.archive and settings.cleanup:
                # The snapshot already owns the exact captured bytes. Archive
                # them under the requested export name without a loose duplicate.
                decoded_sha, decoded_bytes = snapshot.sha256, snapshot.size_bytes
                strategy = 'borrowed_verified_snapshot'
                borrowed_snapshot = True
                members[source_copy.name] = snapshot.path
                result['source_copy'] = None
            else:
                decoded_sha, decoded_bytes, strategy = _stage_snapshot_copy(
                    snapshot, source_copy, reporter=reporter, private=private_output)
                members[source_copy.name] = source_copy
                result['source_copy'] = str(source_copy)
            result['source_copy_sha256'] = decoded_sha
            result['source_copy_bytes'] = decoded_bytes
            result['source_copy_strategy'] = strategy
    else:
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
            from .storage import require_space
            require_space(md.parent, expected_bytes, stage='stage decoded native JSONL')
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
        add_review()
        return result
    loose_members = {name: path for name, path in members.items()
                     if not (borrowed_snapshot and source_copy and name == source_copy.name)}
    archive = Path(destination).resolve() if destination is not None else md.with_suffix('.zip')
    archive.parent.mkdir(parents=True, exist_ok=True)
    backend = settings.effective_backend()
    archiver = find_archiver(settings.archiver) if backend == '7zip' else None
    if archive.exists():
        raise FileExistsError(f'Refusing to replace an existing ZIP: {archive}')
    details = json.loads(manifest.read_text(encoding='utf-8'))
    details['archive_plan'] = {
        'format': 'zip', 'method': settings.method, 'level': settings.level,
        'backend': backend,
        'dictionary': settings.dictionary if settings.method.upper() == 'LZMA' else None,
        'dictionary_policy': 'configured maximum, limited to known input length as with 7-Zip file input',
        'word_size': settings.word_size if settings.method.upper() == 'LZMA' else None,
        'threads': settings.threads if backend == '7zip' else 1,
        'archive_filename': archive.name, 'members': sorted(members),
        'native_source_member': source_copy.name if source_copy else None,
        'source_copy_sha256': result.get('source_copy_sha256'),
        'source_copy_bytes': result.get('source_copy_bytes'),
        'source_copy_strategy': result.get('source_copy_strategy'),
        'review_included': bool(review),
        'cleanup_after_full_archive_verification': settings.cleanup,
        'publication_permissions': 'inherit_destination_folder_on_windows',
        'max_archive_bytes': settings.max_bytes,
        'overflow_policy': 'independent_zip_parts_with_lossless_offset_and_sha256_provenance',
        'overflow_filename_pattern': archive.stem + '.partNNNN-of-NNNN.zip',
    }
    def save_manifest() -> None:
        with manifest.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(details, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
    save_manifest()
    result['archive_settings'] = {
        'method': settings.method, 'level': settings.level, 'backend': backend,
        'dictionary': settings.dictionary if settings.method.upper() == 'LZMA' else None,
        'word_size': settings.word_size if settings.method.upper() == 'LZMA' else None,
        'threads': settings.threads if backend == '7zip' else 1,
        'max_bytes': settings.max_bytes, 'work_dir': str(settings.work_dir),
    }
    from .storage import private_prefix, work_directory_parent
    work_parent = work_directory_parent(archive.parent, settings.work_dir)
    with tempfile.TemporaryDirectory(prefix=private_prefix(archive.stem, 'archive'), dir=work_parent) as temporary:
        work = Path(temporary)
        if review:
            from .review import prepare_review_files, refresh_embedded_manifest
            prepared = prepare_review_files(md, manifest, directory=work / 'review-members',
                                             chunk_bytes=review_chunk_bytes, progress=reporter, embedded=True)
            extra_members = prepared['members']
            if set(members) & set(extra_members):
                raise RuntimeError('Review filenames conflict with core archive members')
            members.update(extra_members)
            details['archive_plan']['members'] = sorted(members)
            save_manifest()
            refresh_embedded_manifest(prepared, manifest)
            result.update({key: value for key, value in prepared.items() if key != 'members'})
        expected_native = ({source_copy.name: (result['source_copy_sha256'], result['source_copy_bytes'])}
                           if source_copy else {})
        parts, member_signatures, strategy = _create_archive_parts(
            members, settings, work=work, destination=archive, backend=backend,
            archiver=archiver, reporter=reporter, expected_checksums=expected_native)
        _check_member_signatures(members, member_signatures)
        from .bounded_archive import publish_parts
        destinations, publications = publish_parts(parts, archive, max_bytes=settings.max_bytes,
                                                   reporter=reporter, publisher=_publish_archive,
                                                   consume=True)
        result['archive_work_strategy'] = strategy
        result['archive_work_directory'] = str(work_parent)
        member_methods = {name: method for part in parts for name, method in part.member_methods.items()}
        payload_store = any(
            part.member_methods[name] == 'Store'
            for part in parts
            for name in ([entry['member'] for entry in part.provenance['members']]
                         if part.provenance else part.members))
        archive = destinations[0]
        result.update({
            'archive': str(archive), 'archives': [str(path) for path in destinations],
            'archive_verified': True, 'archive_count': len(parts),
            'archive_packing': parts[0].packing or {'parts': 1, 'fixed_piece_minimum_proved': True,
                'scope': 'One complete archive; no multipart packing needed'},
            'archive_parts': [
                {'archive': str(path), 'bytes': part.size_bytes, 'members': part.members,
                 'provenance': part.provenance}
                for path, part in zip(destinations, parts)],
            'archiver': str(archiver) if archiver else 'python.zipfile',
            'archive_backend': backend, 'archive_publication': publications[0],
            'archive_publications': publications,
            'archive_method': settings.method, 'archive_members': sorted(member_methods),
            'archive_member_methods': member_methods,
            'archive_member_dictionary_bytes': {name: size for part in parts
                for name, size in part.lzma_dictionary_bytes.items()},
            'archive_max_dictionary_bytes': max((size for part in parts
                for size in part.lzma_dictionary_bytes.values()), default=0),
            'archive_used_store_fallback': payload_store,
            'archive_metadata_method': 'Store' if any(part.provenance for part in parts) else None,
            'archive_bytes': archive.stat().st_size,
            'archive_total_bytes': sum(part.size_bytes for part in parts),
            'archive_max_bytes': settings.max_bytes,
            'archive_uncompressed_bytes': sum(path.stat().st_size for path in members.values()),
            'archived_markdown': md.name,
            'archived_manifest': manifest.name if manifest else None,
            'archived_source': source_copy.name if source_copy else None,
            'archived_unknown_sidecar': unknown_sidecar.name if unknown_sidecar else None,
            'loose_files_removed': False,
        })
        if review:
            result.update(review_archive=str(archive), review_directory=None, review_layout='embedded')
        if settings.cleanup:
            _check_member_signatures(members, member_signatures)
            with reporter.stage('Clean exported files', total=len(loose_members), unit='files',
                                detail='Verified archive retained; live source untouched') as meter:
                # Remove only the exact generated triplet, after all checks pass.
                for file in loose_members.values():
                    file.unlink()
                    meter.advance(1)
            result.update(loose_files_removed=True, file=None, manifest=None, source_copy=None,
                          unknown_sidecar=None)
    return result


def _flat_private_inputs(members: dict[str, Path], directory: Path) -> dict[str, Path] | None:
    """Alias small immutable inputs with private links; never duplicate raw bytes."""
    if len({path.parent for path in members.values()}) == 1 and all(
            name == path.name for name, path in members.items()):
        return members
    directory.mkdir()
    aliases = {}
    try:
        for name, source in members.items():
            target = directory / name
            os.link(source, target)
            aliases[name] = target
        return aliases
    except OSError:
        # Cross-volume or hard-link-restricted filesystems use streamed ranges.
        # Link creation/removal finishes before mutation signatures are captured.
        shutil.rmtree(directory)
        return None


def _create_archive_parts(members: dict[str, Path], settings: ArchiveSettings, *,
                          work: Path, destination: Path, backend: str,
                          archiver: Path | None, reporter,
                          expected_checksums: dict[str, tuple[str, int]] | None = None,
                          compress_whole=None):
    """Choose bounded streaming before creating an oversized whole archive."""
    from .archive_stream import compress_range
    from .bounded_archive import BoundedPart, build_bounded_archives
    from .storage import require_space
    expected_checksums = expected_checksums or {}
    raw_bytes = sum(path.stat().st_size for path in members.values())
    small = raw_bytes * 1.02 + len(members) * 512 + 22 <= settings.max_bytes
    inputs = members
    if backend == '7zip':
        inputs = _flat_private_inputs(members, work / 'compression-inputs')
    signatures = {name: _file_signature(path) for name, path in members.items()}
    ratios = {}
    if not small:
        # A tiny real-codec probe avoids learning the first range's ratio by
        # repeatedly overflowing a large candidate. It is planning only; every
        # final archive still has an actual-size check and independent decoding.
        for number, (name, source) in enumerate(members.items(), 1):
            size = source.stat().st_size
            if size <= min(65536, settings.max_bytes // 4):
                ratios[name] = 1.02
                continue
            count = min(size, 256 * 1024, max(4096, settings.max_bytes // 8))
            sample = work / f'ratio-sample-{number:04d}.zip'
            try:
                compress_range(sample, name, source, 0, count, count * 2 + 4096,
                    settings=settings, backend=backend, archiver=archiver,
                    reporter=reporter, adaptive=False, stage_label='Sample compression ratio')
                with zipfile.ZipFile(sample) as archive:
                    ratios[name] = archive.getinfo(name).compress_size / max(1, count)
            finally:
                sample.unlink(missing_ok=True)
    estimated = sum(path.stat().st_size * ratios.get(name, 1.02)
                    for name, path in members.items()) + len(members) * 512 + 22
    whole = inputs is not None and (small or (backend == '7zip' and estimated < settings.max_bytes * .90))
    checksums: dict[str, tuple[str, int]] = {}
    if whole:
        from .archive_stream import RangeTooLarge
        staged = work / destination.name
        require_space(work, len(members) * 512 + 22, stage='start export archive')
        try:
            (compress_whole or _compress_archive)(staged, inputs, settings, backend=backend,
                archiver=archiver, reporter=reporter, max_bytes=None if small else settings.max_bytes)
            fits = staged.stat().st_size <= settings.max_bytes
        except RangeTooLarge:
            fits = False
        if fits:
            _verify_members(staged, members, settings.method, reporter=reporter, checksums=checksums)
            _check_member_signatures(members, signatures)
            for name, expected in expected_checksums.items():
                if checksums.get(name) != expected:
                    raise RuntimeError(f'Native JSONL no longer matches its captured snapshot: {name}')
            with zipfile.ZipFile(staged) as archive:
                names = {zipfile.ZIP_LZMA: 'LZMA', zipfile.ZIP_DEFLATED: 'Deflate', zipfile.ZIP_STORED: 'Store'}
                methods = {info.filename: names[info.compress_type] for info in archive.infolist()}
            strategy = 'single_small_archive' if small else 'single_archive_after_ratio_probe'
            parts = [BoundedPart(staged, methods)]
            _record_archive_dictionaries(parts)
            return parts, signatures, strategy
        staged.unlink(missing_ok=True)
    total = sum(path.stat().st_size for name, path in members.items() if name not in expected_checksums)
    with reporter.stage('Checksum archive inputs', total=total,
                        detail='Reuse captured native SHA-256; hash rendered members') as meter:
        done = 0
        for name, path in members.items():
            if name in expected_checksums:
                if path.stat().st_size != expected_checksums[name][1]:
                    raise RuntimeError(f'Native JSONL size no longer matches its snapshot: {name}')
                checksums[name] = expected_checksums[name]
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    size += len(block)
                    done += len(block)
                    meter.update(done, detail=name)
            checksums[name] = digest.hexdigest(), size
    parts = build_bounded_archives(
        members, work=work / 'bounded', basename=destination.name,
        max_bytes=settings.max_bytes, method=settings.method, reporter=reporter, checksums=checksums,
        estimated_ratios=ratios,
        compress_range=lambda path, name, source, offset, count, limit: compress_range(
            path, name, source, offset, count, limit, settings=settings,
            backend=backend, archiver=archiver, reporter=reporter))
    _check_member_signatures(members, signatures)
    _record_archive_dictionaries(parts)
    return parts, signatures, 'streamed_bounded_members'


def package_export(result: dict[str, Any], original_source: Path,
                   settings: ArchiveSettings, *, snapshot: SourceSnapshot | None = None,
                   review: bool | None = None, review_chunk_bytes: int | None = None,
                   reporter=None, destination: Path | None = None,
                   private_output: bool = False) -> dict[str, Any]:
    """Package generated files; the caller owns its render/snapshot transaction.

    ``destination`` separates private rendering from final ZIP publication.
    ``private_output`` permits private hard links for retained batch JSONL only;
    publicly visible Windows exports always inherit the destination folder DACL.
    """
    from .storage import storage_failure
    output = (Path(destination).parent if destination is not None else
              Path(result.get('file') or original_source).parent)
    work = Path(settings.work_dir).expanduser() if settings.work_dir else output
    try:
        return _package_export_impl(result, original_source, settings, snapshot=snapshot,
            review=review, review_chunk_bytes=review_chunk_bytes, reporter=reporter,
            destination=destination, private_output=private_output)
    except BaseException as error:
        enriched = storage_failure(error, stage='export packaging', work_dir=work, output_dir=output)
        if enriched is not error:
            raise enriched from error
        raise


def _record_archive_dictionaries(parts) -> None:
    """Record encoded decoder-memory properties separately from requested limits."""
    from .archive_diagnostics import _lzma_properties
    for part in parts:
        with zipfile.ZipFile(part.path) as archive:
            for info in archive.infolist():
                if info.compress_type == zipfile.ZIP_LZMA:
                    value = _lzma_properties(archive.fp, info, archive.start_dir)
                    part.lzma_dictionary_bytes[info.filename] = value['dictionary_bytes']
