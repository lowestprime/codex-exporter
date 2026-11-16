"""Pack several completed session exports together without losing their provenance."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .archive_pipeline import (ArchiveSettings, _check_member_signatures, _compress_archive,
                               _file_signature, _publish_archive, _verify_members, find_archiver,
                               _create_archive_parts)
from .bounded_archive import bound_archive, publish_parts
from .file_access import validate_export_paths
from .progress import ProgressReporter


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _package_batch_impl(results: list[dict[str, Any]], destination: Path, settings: ArchiveSettings,
                  *, reporter=None) -> dict[str, Any]:
    """Compress once, bound actual sizes, verify all parts, then clean owned files.

    Callers stage source copies while their snapshots are alive. Each session
    keeps its own MD/manifest/JSONL names; BATCH_INDEX.json supplies navigation.
    """
    settings.validate()
    reporter = reporter or ProgressReporter(enabled=False)
    exports = [item for item in results if item.get('file') and not item.get('skipped')]
    if not exports:
        return {'exports': results, 'archives': [], 'archive_count': 0, 'skipped': True}
    destination = Path(destination).resolve()
    protected = [Path(item['source_jsonl']) for item in exports if item.get('source_jsonl')]
    validate_export_paths({'archive': destination}, protected)
    if destination.exists():
        raise FileExistsError(f'Refusing to replace existing batch archive: {destination}')
    members: dict[str, Path] = {}
    for item in exports:
        for key in ('file', 'manifest', 'source_copy', 'unknown_sidecar'):
            if not item.get(key):
                continue
            path = Path(item[key]).resolve()
            if not path.is_file():
                raise FileNotFoundError(f'Batch export member missing: {path}')
            if path.name in members:
                raise ValueError(f'Batch member filename collision: {path.name}')
            members[path.name] = path
        if not item.get('manifest'):
            raise ValueError('Batch ZIP packaging requires a manifest for every session')
    validate_export_paths({f'member:{name}': path for name, path in members.items()}, protected)
    loose_members = dict(members)
    backend = settings.effective_backend()
    archiver = find_archiver(settings.archiver) if backend == '7zip' else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    from .storage import private_prefix, work_directory_parent
    work_parent = work_directory_parent(destination.parent, settings.work_dir)
    with tempfile.TemporaryDirectory(prefix=private_prefix(destination.stem, 'batch'), dir=work_parent) as temporary:
        work = Path(temporary)
        staging = work / 'members'
        staging.mkdir()
        navigation = []
        for number, item in enumerate(exports, 1):
            md = Path(item['file']).resolve()
            manifest = Path(item['manifest']).resolve()
            details = json.loads(manifest.read_text(encoding='utf-8'))
            names = [Path(item[key]).name for key in ('file', 'manifest', 'source_copy', 'unknown_sidecar') if item.get(key)]
            details['archive_plan'] = {
                'format': 'zip', 'method': settings.method, 'level': settings.level,
                'dictionary': settings.dictionary, 'word_size': settings.word_size,
                'dictionary_policy': 'configured maximum, limited to known input length as with 7-Zip file input',
                'threads': settings.threads, 'backend': backend,
                'archive_filename': destination.name, 'members': names,
                'batch_index': 'BATCH_INDEX.json', 'max_archive_bytes': settings.max_bytes,
                'source_copy_sha256': item.get('source_copy_sha256'),
                'source_copy_bytes': item.get('source_copy_bytes'),
                'native_source_member': Path(item['source_copy']).name if item.get('source_copy') else None,
                'review_included': settings.review,
                'overflow_policy': 'independent_zip_parts_with_lossless_offset_and_sha256_provenance',
            }
            _write_json(manifest, details)
            entry = {'session_id': item.get('session_id'), 'title': item.get('title'),
                     'mode': item.get('mode'), 'markdown': md.name, 'manifest': manifest.name,
                     'native_jsonl': Path(item['source_copy']).name if item.get('source_copy') else None,
                     'source_sha256': item.get('source_sha256')}
            if settings.review:
                from .review import prepare_review_files
                review = prepare_review_files(md, manifest, directory=work / f'review-{number}',
                    chunk_bytes=settings.review_chunk_bytes, embedded=True, progress=reporter,
                    member_prefix=f'session-{number:04d}.')
                for name, path in review['members'].items():
                    if name in members:
                        raise ValueError(f'Batch review filename collision: {name}')
                    members[name] = path
                entry['review_index'] = review['review_index_member']
                entry['review_start'] = review['review_start_member']
            navigation.append(entry)
        index_path = staging / 'BATCH_INDEX.json'
        if index_path.name in members:
            raise ValueError('BATCH_INDEX.json conflicts with an export member')
        _write_json(index_path, {
            'schema_version': 1, 'format': 'codex_batch_export',
            'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'sessions': navigation, 'max_archive_bytes': settings.max_bytes,
            'instructions': 'Each session has its own rendered Markdown, provenance manifest, and optional matching native JSONL. For multipart sets, read ARCHIVE_PART.json in each ZIP and reconstruct any segmented original by byte offset before parsing it as a whole file. Archive upload success alone does not prove complete context ingestion.',
        })
        members[index_path.name] = index_path
        expected_native = {
            Path(item['source_copy']).name: (item.get('source_copy_sha256'), item.get('source_copy_bytes'))
            for item in exports if item.get('source_copy')
        }
        if any(not digest or not isinstance(size, int) for digest, size in expected_native.values()):
            raise ValueError('Batch native JSONL requires captured snapshot SHA-256 and byte counts')
        parts, signatures, strategy = _create_archive_parts(
            members, settings, work=work, destination=destination, backend=backend,
            archiver=archiver, reporter=reporter, expected_checksums=expected_native,
            compress_whole=_compress_archive)
        _check_member_signatures(members, signatures)
        destinations, publications = publish_parts(parts, destination, max_bytes=settings.max_bytes,
            reporter=reporter, publisher=_publish_archive, consume=True)
        result = {
            'archive': str(destinations[0]), 'archives': [str(path) for path in destinations],
            'archive_count': len(parts), 'archive_verified': True,
            'archive_compression_planning': parts[0].compression_planning,
            'archive_packing': parts[0].packing or {'parts': 1, 'fixed_piece_minimum_proved': True,
                'scope': 'One complete archive; no multipart packing needed'},
            'archive_max_bytes': settings.max_bytes, 'archive_method': settings.method,
            'archive_backend': backend, 'archiver': str(archiver) if archiver else 'python.zipfile',
            'archive_member_dictionary_bytes': {name: size for part in parts
                for name, size in part.lzma_dictionary_bytes.items()},
            'archive_max_dictionary_bytes': max((size for part in parts
                for size in part.lzma_dictionary_bytes.values()), default=0),
            'archive_bytes': parts[0].size_bytes, 'archive_total_bytes': sum(part.size_bytes for part in parts),
            'archive_parts': [{'archive': str(path), 'bytes': part.size_bytes, 'members': part.members,
                               'provenance': part.provenance} for path, part in zip(destinations, parts)],
            'archive_members': sorted({name for part in parts for name in part.members}),
            'archive_publications': publications, 'batch_index': index_path.name,
            'exports': results, 'loose_files_removed': False,
            'archive_work_strategy': strategy, 'archive_work_directory': str(work_parent),
        }
        if settings.cleanup:
            _check_member_signatures(members, signatures)
            with reporter.stage('Clean batch export files', total=len(loose_members), unit='files',
                                detail='Every bounded archive verified; original sessions retained') as meter:
                for path in loose_members.values():
                    path.unlink()
                    meter.advance(1)
            for item in exports:
                item.update(file=None, manifest=None, source_copy=None, unknown_sidecar=None,
                            loose_files_removed=True)
            result['loose_files_removed'] = True
        return result


def package_batch(results: list[dict[str, Any]], destination: Path, settings: ArchiveSettings,
                  *, reporter=None) -> dict[str, Any]:
    """Publish a verified aggregate set with actionable volume-specific errors."""
    from .storage import storage_failure
    output = Path(destination).parent
    work = Path(settings.work_dir).expanduser() if settings.work_dir else output
    try:
        return _package_batch_impl(results, destination, settings, reporter=reporter)
    except BaseException as error:
        enriched = storage_failure(error, stage='batch archive packaging', work_dir=work, output_dir=output)
        if enriched is not error:
            raise enriched from error
        raise
