"""Lossless ZIP parts with an exact, conservative upload-size ceiling.

An ordinary fitting ZIP is left byte-for-byte unchanged. On overflow, fitting
compressed streams are reused, and only an oversized individual member is
compressed again in bounded pieces. Each resulting ZIP opens independently;
line-aligned JSONL pieces remain JSONL, while unavoidable partial records have
an explicit .bytes suffix. Every part carries its own reconstruction metadata.

The packing heuristic uses actual compressed sizes, not promised codec ratios.
It is first-fit decreasing, not a claim of a globally minimal bin packing or a
universal ChatGPT attachment/decoder guarantee.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import copy
import hashlib
import json
import os
import struct
import uuid
import zipfile

MAX_ARCHIVE_BYTES = 512_000_000
MIN_ARCHIVE_BYTES = 16_384
CHUNK_BYTES = 1024 * 1024
_INDEX_NAME = 'ARCHIVE_PART.json'
_PLANNING_COUNT = 9_223_372_036_854_775_807
_METHODS = {zipfile.ZIP_STORED: 'Store', zipfile.ZIP_LZMA: 'LZMA',
            zipfile.ZIP_DEFLATED: 'Deflate'}


def validate_max_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_ARCHIVE_BYTES <= value <= MAX_ARCHIVE_BYTES:
        raise ValueError(f'archive max_bytes must be {MIN_ARCHIVE_BYTES}..{MAX_ARCHIVE_BYTES} bytes')


def _stage(reporter, name: str, *, total=None, unit='bytes', detail=''):
    return (reporter.stage(name, total=total, unit=unit, detail=detail)
            if reporter is not None else nullcontext(None))


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        for block in iter(lambda: source.read(CHUNK_BYTES), b''):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _name_bytes(info: zipfile.ZipInfo) -> bytes:
    try:
        return info.filename.encode('ascii')
    except UnicodeEncodeError:
        return info.filename.encode('utf-8')


def _safe_name(name: str) -> bool:
    return bool(name) and name not in {'.', '..'} and '/' not in name and '\\' not in name and '\x00' not in name


@dataclass
class _Piece:
    name: str
    original: str
    source: Path
    offset: int
    size: int
    sha256: str
    original_size: int
    original_sha256: str
    archive: Path
    info: zipfile.ZipInfo
    starts_at_line_boundary: bool = True
    ends_at_line_boundary: bool = True

    def record(self) -> dict[str, Any]:
        whole = self.offset == 0 and self.size == self.original_size
        aligned = self.starts_at_line_boundary and self.ends_at_line_boundary
        return {
            'member': self.name, 'original': self.original,
            'offset': self.offset, 'bytes': self.size, 'sha256': self.sha256,
            'original_bytes': self.original_size, 'original_sha256': self.original_sha256,
            'kind': 'complete_file' if whole else 'line_segment' if aligned else 'byte_fragment',
            'starts_at_line_boundary': self.starts_at_line_boundary,
            'ends_at_line_boundary': self.ends_at_line_boundary,
        }


@dataclass
class BoundedPart:
    """A verified staged ZIP; publication remains the caller's responsibility."""
    path: Path
    member_methods: dict[str, str]
    provenance: dict[str, Any] | None = None
    _pieces: list[_Piece] = field(default_factory=list, repr=False)
    _size_bytes: int | None = field(default=None, repr=False)
    packing: dict[str, Any] | None = None
    lzma_dictionary_bytes: dict[str, int] = field(default_factory=dict)
    compression_planning: dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        return self._size_bytes if self._size_bytes is not None else self.path.stat().st_size

    @property
    def members(self) -> list[str]:
        return sorted(self.member_methods)


def _info_weight(info: zipfile.ZipInfo) -> int:
    """Exact normalized local-header, payload and central-directory bytes."""
    large_size = info.file_size > zipfile.ZIP64_LIMIT
    large_compressed = info.compress_size > zipfile.ZIP64_LIMIT
    local_extra = 20 if large_size or large_compressed else 0
    central_extra = 4 + 8 * (large_size + large_compressed) if large_size or large_compressed else 0
    return 30 + 46 + 2 * len(_name_bytes(info)) + local_extra + central_extra + info.compress_size


def _metadata(base: dict, pieces: list[_Piece], part: int, count: int) -> dict:
    return {**base, 'part': part, 'parts': count, 'members': [piece.record() for piece in pieces]}


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def _planned_size(base: dict, pieces: list[_Piece], index_name: str) -> int:
    # Largest feasible part counters reserve their exact serialized width. The
    # final metadata can only be shorter, so planning cannot undercount it.
    metadata = _json_bytes(_metadata(base, pieces, _PLANNING_COUNT, _PLANNING_COUNT))
    count = len(pieces) + 1
    trailer = 22 + (76 if count > zipfile.ZIP_FILECOUNT_LIMIT else 0)
    index_size = 30 + 46 + 2 * len(index_name.encode('ascii')) + len(metadata)
    return sum(_info_weight(piece.info) for piece in pieces) + index_size + trailer


def _read_info(path: Path, name: str) -> zipfile.ZipInfo:
    with zipfile.ZipFile(path, 'r') as archive:
        infos = archive.infolist()
        if len(infos) != 1 or infos[0].filename != name:
            raise RuntimeError('Segment compressor returned an unexpected member inventory')
        return copy.copy(infos[0])


def _copy_compressed(target: zipfile.ZipFile, piece: _Piece) -> None:
    """Copy a trusted ZIP stream without recompression, normalizing its headers.

    zipfile has no public compressed-entry copy operation. Its stable ZipInfo
    FileHeader and central-directory writer are used here, with data descriptors
    removed because the CRC and sizes are already known. Unknown extra fields
    are discarded; ZIP64 fields are regenerated by zipfile when required.
    The completed archive is decoded and SHA-256 checked independently below.
    """
    info = copy.copy(piece.info)
    info.filename = piece.name
    info.orig_filename = piece.name
    info.extra = b''
    info.comment = b''
    info.flag_bits &= ~0x08
    info.header_offset = target.fp.tell()
    target._writecheck(info)
    target._didModify = True
    use_zip64 = info.file_size > zipfile.ZIP64_LIMIT or info.compress_size > zipfile.ZIP64_LIMIT
    target.fp.write(info.FileHeader(zip64=use_zip64))
    with piece.archive.open('rb') as source:
        source.seek(piece.info.header_offset)
        header = source.read(30)
        if len(header) != 30 or header[:4] != b'PK\x03\x04':
            raise RuntimeError('Invalid staged ZIP local header')
        name_bytes, extra_bytes = struct.unpack_from('<HH', header, 26)
        source.seek(name_bytes + extra_bytes, os.SEEK_CUR)
        remaining = info.compress_size
        while remaining:
            block = source.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise RuntimeError('Staged ZIP was truncated while copying a compressed member')
            target.fp.write(block)
            remaining -= len(block)
    target.filelist.append(info)
    target.NameToInfo[info.filename] = info
    target.start_dir = target.fp.tell()


def _assemble(path: Path, pieces: list[_Piece], index_name: str, metadata: dict) -> None:
    with zipfile.ZipFile(path, 'x', allowZip64=True) as archive:
        for piece in pieces:
            _copy_compressed(archive, piece)
        # Tiny navigation metadata is intentionally Store; source payloads keep
        # their requested codec. This is not a Deflate or payload-Store fallback.
        index = zipfile.ZipInfo(index_name, date_time=(1980, 1, 1, 0, 0, 0))
        index.compress_type = zipfile.ZIP_STORED
        index.external_attr = 0o100644 << 16
        archive.writestr(index, _json_bytes(metadata))


def _line_end(source: Path, start: int, desired: int, limit: int) -> int:
    """Prefer the last complete line, without loading a giant record into RAM."""
    end = min(limit, start + max(1, desired))
    if end == limit:
        return end
    with source.open('rb') as stream:
        cursor = end
        while cursor > start:
            begin = max(start, cursor - CHUNK_BYTES)
            stream.seek(begin)
            block = stream.read(cursor - begin)
            newline = block.rfind(b'\n')
            if newline >= 0:
                return begin + newline + 1
            cursor = begin
    # A single record spans the proposed chunk. Do not drop or truncate it:
    # explicit byte fragments are reconstructable even for non-UTF-8 inputs.
    return end


def _line_boundaries(source: Path, start: int, end: int, size: int) -> tuple[bool, bool]:
    with source.open('rb') as stream:
        starts = start == 0
        if not starts:
            stream.seek(start - 1)
            starts = stream.read(1) == b'\n'
        ends = end == size
        if not ends:
            stream.seek(end - 1)
            ends = stream.read(1) == b'\n'
    return starts, ends


def _copy_range(source: Path, destination: Path, offset: int, count: int) -> str:
    digest = hashlib.sha256()
    with source.open('rb') as original, destination.open('xb') as output:
        original.seek(offset)
        remaining = count
        while remaining:
            block = original.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise RuntimeError('Exported member was truncated during segmentation; no cleanup performed')
            output.write(block)
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _segment(original: _Piece, *, base: dict, index_name: str, max_bytes: int,
             work: Path, compress: Callable[[Path, dict[str, Path]], None],
             reporter, number: int, used_names: set[str]) -> list[_Piece]:
    result: list[_Piece] = []
    offset = 0
    ratio = max(original.info.compress_size / max(1, original.size), 0.000001)
    target = max(1, int((max_bytes - 8192) / ratio * .98))
    ordinal = 0
    with _stage(reporter, 'Segment oversized archive member', total=original.size,
                detail=f'{original.original}; retain every byte') as meter:
        while offset < original.size:
            ordinal += 1
            attempts = 0
            while True:
                end = _line_end(original.source, offset, target, original.size)
                starts, ends = _line_boundaries(original.source, offset, end, original.size)
                suffix = Path(original.original).suffix
                # Avoid path-length growth from an already-long export basename.
                extension = suffix if starts and ends and suffix.lower() in ('.jsonl', '.md', '.txt') else suffix + '.bytes'
                name = unique_segment_member_name(original.original, ordinal, extension, used_names)
                chunk = work / name
                digest = _copy_range(original.source, chunk, offset, end - offset)
                zipped = work / f'compressed-{number:04d}-{ordinal:06d}-{attempts:02d}.zip'
                try:
                    compress(zipped, {name: chunk})
                    info = _read_info(zipped, name)
                finally:
                    chunk.unlink(missing_ok=True)
                piece = _Piece(name, original.original, original.source, offset, end - offset,
                               digest, original.original_size, original.original_sha256,
                               zipped, info, starts, ends)
                packed_size = _planned_size(base, [piece], index_name)
                if packed_size <= max_bytes:
                    result.append(piece)
                    used_names.add(name)
                    offset = end
                    if meter is not None:
                        meter.update(offset, detail=f'{original.original}: {ordinal} segments, {attempts} retries')
                    observed_ratio = max(info.compress_size / piece.size, .000001)
                    available = max_bytes - (packed_size - info.compress_size)
                    target = max(1, int(available / observed_ratio * .985))
                    break
                zipped.unlink()
                attempts += 1
                if piece.size <= 1:
                    raise ValueError('Archive byte limit is too small for the required provenance and one payload byte')
                available = max_bytes - (packed_size - info.compress_size)
                if available <= 0:
                    raise ValueError('Archive byte limit is too small for required member provenance')
                target = max(1, min(piece.size - 1,
                                   int(piece.size * available / max(1, info.compress_size) * .96)))
                if attempts >= 3:
                    # Guarantees progress for hostile local compression ratios;
                    # this is a size bound, never silent content omission.
                    target = min(target, max(1, max_bytes // 2))
    return result


def _initial_checksums(archive: Path, members: dict[str, Path], method: str) -> dict[str, tuple[str, int]]:
    from .archive_pipeline import _verify_members
    checksums: dict[str, tuple[str, int]] = {}
    _verify_members(archive, members, method, checksums=checksums)
    return checksums


def _verify_parts(parts: list[BoundedPart], checksums: dict[str, tuple[str, int]],
                  index_name: str, max_bytes: int, method: str, reporter) -> None:
    requested = zipfile.ZIP_LZMA if method.upper() == 'LZMA' else zipfile.ZIP_DEFLATED
    allowed = {requested, zipfile.ZIP_STORED}
    locations: dict[str, list[tuple[_Piece, BoundedPart]]] = {name: [] for name in checksums}
    for part in parts:
        if part.size_bytes > max_bytes:
            raise RuntimeError('Bounded archive exceeds its byte limit; nothing published')
        with zipfile.ZipFile(part.path, 'r') as archive:
            infos = archive.infolist()
            expected = {piece.name for piece in part._pieces} | {index_name}
            if (len(infos) != len(expected) or {info.filename for info in infos} != expected
                    or any(not _safe_name(info.filename) or info.is_dir() or info.flag_bits & 1
                           or info.compress_type not in allowed for info in infos)):
                raise RuntimeError('Bounded ZIP member inventory or codec mismatch')
            if archive.read(index_name) != _json_bytes(part.provenance):
                raise RuntimeError('Bounded ZIP reconstruction metadata mismatch')
            part.member_methods = {info.filename: _METHODS[info.compress_type] for info in infos}
        for piece in part._pieces:
            locations[piece.original].append((piece, part))
    with _stage(reporter, 'Verify complete archive set', total=sum(size for _, size in checksums.values()),
                detail='Decode all ZIPs; prove contiguous offsets and original SHA-256') as meter:
        done = 0
        for name, expected in checksums.items():
            digest = hashlib.sha256()
            offset = 0
            ordered = sorted(locations[name], key=lambda value: value[0].offset)
            if not ordered:
                raise RuntimeError(f'Archive set omitted an original member: {name}')
            for piece, part in ordered:
                if piece.offset != offset:
                    raise RuntimeError(f'Archive coverage gap or overlap: {name} at {offset}')
                piece_hash = hashlib.sha256()
                size = 0
                with zipfile.ZipFile(part.path, 'r') as archive, archive.open(piece.name, 'r') as source:
                    for block in iter(lambda: source.read(CHUNK_BYTES), b''):
                        digest.update(block)
                        piece_hash.update(block)
                        size += len(block)
                        done += len(block)
                        if meter is not None:
                            meter.update(done, detail=f'{part.path.name}: {piece.name}')
                if (piece_hash.hexdigest(), size) != (piece.sha256, piece.size):
                    raise RuntimeError(f'Bounded archive segment checksum mismatch: {piece.name}')
                offset += size
            if (digest.hexdigest(), offset) != expected:
                raise RuntimeError(f'Bounded archive reconstruction mismatch: {name}')


def bound_archive(staged_zip: Path, members: dict[str, Path], *, max_bytes: int,
                  work: Path, compress: Callable[[Path, dict[str, Path]], None],
                  method: str, reporter=None,
                  checksums: dict[str, tuple[str, int]] | None = None) -> list[BoundedPart]:
    """Return verified, independently readable ZIPs under the exact byte cap.

    ``compress`` must create a new ZIP containing the supplied flat member map
    with the caller's selected codec/settings. Passing ``checksums`` means the
    caller has already decoded the staged ZIP and compared every original file;
    otherwise that check is performed here. All work paths remain private.
    """
    validate_max_bytes(max_bytes)
    staged_zip, work = Path(staged_zip), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if checksums is None:
        checksums = _initial_checksums(staged_zip, members, method)
    if set(checksums) != set(members):
        raise ValueError('Archive checksums must describe exactly the original members')
    with zipfile.ZipFile(staged_zip, 'r') as archive:
        infos = archive.infolist()
        methods = {info.filename: _METHODS.get(info.compress_type, str(info.compress_type)) for info in infos}
    if staged_zip.stat().st_size <= max_bytes:
        return [BoundedPart(staged_zip, methods)]
    if not members or any(not _safe_name(name) for name in members):
        raise ValueError('Bounded archiving requires distinct flat filenames')
    if len(infos) != len(members) or {info.filename for info in infos} != set(members):
        raise RuntimeError('Staged ZIP inventory differs from the source files')
    index_name = _INDEX_NAME
    while index_name in members:
        index_name = '_' + index_name
    inventory = [(name, *checksums[name]) for name in sorted(members)]
    base = {
        'schema': 'codex-export.archive-part.v1',
        'export_id': uuid.uuid4().hex,
        'max_archive_bytes': max_bytes,
        'requested_method': method,
        'original_member_count': len(members),
        'inventory_encoding': 'UTF-8 JSON [filename,sha256,bytes] rows sorted by filename; ensure_ascii=false; no spaces',
        'inventory_sha256': hashlib.sha256(json.dumps(inventory, ensure_ascii=False,
                                                     separators=(',', ':')).encode('utf-8')).hexdigest(),
        'reassembly': 'Collect all parts with this export_id. For each original filename, concatenate members '
                      'in offset order; require no gaps/overlaps and verify original_bytes and original_sha256. '
                      'Byte fragments may split a record or UTF-8 character; reassemble them before parsing.',
    }
    pieces: list[_Piece] = []
    used_names = set(members) | {index_name}
    with _stage(reporter, 'Plan upload-sized archives', total=len(infos), unit='files',
                detail=f'Actual compressed bytes; maximum {max_bytes:,} bytes per ZIP') as meter:
        for number, info in enumerate(infos, 1):
            digest, size = checksums[info.filename]
            original = _Piece(info.filename, info.filename, members[info.filename], 0, size,
                              digest, size, digest, staged_zip, copy.copy(info))
            if _planned_size(base, [original], index_name) <= max_bytes:
                pieces.append(original)
            else:
                pieces.extend(_segment(original, base=base, index_name=index_name,
                                       max_bytes=max_bytes, work=work, compress=compress,
                                       reporter=reporter, number=number, used_names=used_names))
            if meter is not None:
                meter.update(number, detail=f'{info.filename}: {len(pieces)} payload members')
    # Exact compressed weights + per-entry provenance. This uses at most a few
    # bytes of conservative counter overhead, without recompressing fitting data.
    bins: list[list[_Piece]] = []
    bin_sizes: list[int] = []
    # Cache serialized provenance weights once: repeated full-group JSON
    # encoding would become quadratic for large project/review inventories.
    weighted = [(piece, _info_weight(piece.info) + len(_json_bytes(piece.record())))
                for piece in pieces]
    for piece, added_weight in sorted(weighted, key=lambda item: item[1], reverse=True):
        for index, group in enumerate(bins):
            # _json_bytes adds one newline; the same byte is the comma needed
            # when appending this record to a nonempty metadata member list.
            extra_zip64 = 76 if len(group) + 1 == zipfile.ZIP_FILECOUNT_LIMIT else 0
            planned = bin_sizes[index] + added_weight + extra_zip64
            if planned <= max_bytes:
                group.append(piece)
                bin_sizes[index] = planned
                break
        else:
            planned = _planned_size(base, [piece], index_name)
            if planned > max_bytes:
                raise RuntimeError('One compressed segment still exceeds the archive limit')
            bins.append([piece])
            bin_sizes.append(planned)
    parts: list[BoundedPart] = []
    with _stage(reporter, 'Assemble independent ZIP parts', total=len(bins), unit='files',
                detail='Reuse compressed payloads; no second full compression pass') as meter:
        for number, group in enumerate(bins, 1):
            path = work / f'part-{number:04d}-of-{len(bins):04d}.zip'
            metadata = _metadata(base, group, number, len(bins))
            _assemble(path, group, index_name, metadata)
            if path.stat().st_size > max_bytes:
                raise RuntimeError('Final ZIP size exceeded the cap; nothing published')
            parts.append(BoundedPart(path, {}, metadata, group))
            if meter is not None:
                meter.update(number, detail=f'{path.name}: {path.stat().st_size:,} bytes')
    _verify_parts(parts, checksums, index_name, max_bytes, method, reporter)
    return parts


def archive_stem_digest(base: Path) -> str:
    """Stable shortening identity shared by multipart names and collision checks."""
    return hashlib.sha256(Path(base).stem.encode('utf-8')).hexdigest()[:12]


def archive_destinations(base: Path, count: int) -> list[Path]:
    if count < 1:
        raise ValueError('An archive set must contain at least one ZIP')
    base = Path(base)
    if count == 1:
        return [base]
    destinations = []
    for number in range(1, count + 1):
        suffix = f'.part{number:04d}-of-{count:04d}{base.suffix}'
        name = base.stem + suffix
        if len(name.encode('utf-8')) > 255:
            # Preserve Unicode character boundaries, the complete part marker,
            # and an identity for collision checks after truncating a long stem.
            # A 255 UTF-8-byte cap is also conservative for Windows UTF-16 units.
            tail = '-' + archive_stem_digest(base) + suffix
            budget = 255 - len(tail.encode('utf-8'))
            if budget < 1:
                raise ValueError('Multipart archive suffix exceeds the filename limit')
            prefix = base.stem.encode('utf-8')[:budget].decode('utf-8', errors='ignore').rstrip(' .')
            name = prefix + tail
        destinations.append(base.with_name(name))
    return destinations


def publish_parts(parts: list[BoundedPart], destination: Path, *, max_bytes: int,
                  reporter=None, publisher=None, consume: bool = False) -> tuple[list[Path], list[str]]:
    """Publish only a fully verified set, rolling back our own files on failure."""
    from .file_access import publish_file, hardlink_publication_allowed
    from .storage import require_space, same_volume
    validate_max_bytes(max_bytes)
    publisher = publisher or publish_file
    destinations = archive_destinations(destination, len(parts))
    for part, target in zip(parts, destinations):
        if part.size_bytes > max_bytes:
            raise RuntimeError('Refusing to publish an archive over its byte limit')
        if target.exists():
            raise FileExistsError(f'Refusing to replace an existing ZIP: {target}')
    total_bytes = sum(part.size_bytes for part in parts)
    colocated = all(same_volume(part.path.parent, Path(destination).parent) for part in parts)
    # Consuming verified private parts bounds Windows publication overhead to
    # one part instead of another full archive set. POSIX links allocate no data.
    needed = (0 if colocated and hardlink_publication_allowed() else
              max(part.size_bytes for part in parts) if colocated and consume else total_bytes)
    require_space(Path(destination).parent, needed, stage='publish verified archive set')
    published: list[tuple[Path, tuple]] = []
    strategies: list[str] = []
    try:
        for part, target in zip(parts, destinations):
            strategies.append(publisher(part.path, target, reporter=reporter))
            info = target.stat()
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            published.append((target, signature))
            if consume:
                if part.path.resolve() == target.resolve():
                    raise ValueError('Cannot consume an archive at its own destination')
                part._size_bytes = part.size_bytes
                part.path.unlink()
                part.path = target
                # Removing a POSIX hard link changes the destination inode's
                # ctime. Track the post-consumption identity for safe rollback.
                info = target.stat()
                signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                published[-1] = (target, signature)
            if info.st_size > max_bytes:
                raise RuntimeError('Published archive exceeded its byte limit')
    except BaseException:
        for target, expected in reversed(published):
            try:
                info = target.lstat()
                actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if actual == expected:
                    target.unlink()
            except OSError:
                pass
        raise
    return destinations, strategies


def segment_member_name(original: str, ordinal: int, extension: str | None = None) -> str:
    """Keep each segment's original export stem, suffix and stable identity."""
    original_path = Path(original)
    suffix = extension if extension is not None else original_path.suffix
    tail = f'.segment{ordinal:06d}{suffix}'
    stem = original_path.stem
    name = stem + tail
    if len(name.encode('utf-8')) > 255:
        tail = '-' + hashlib.sha256(original.encode('utf-8')).hexdigest()[:12] + tail
        budget = 255 - len(tail.encode('utf-8'))
        if budget < 1:
            raise ValueError('Segment suffix exceeds the filename limit')
        name = stem.encode('utf-8')[:budget].decode('utf-8', errors='ignore').rstrip(' .') + tail
    return name



def unique_segment_member_name(original: str, ordinal: int, extension: str,
                               used_names: set[str]) -> str:
    name = segment_member_name(original, ordinal, extension)
    collision = 1
    while name in used_names:
        collision += 1
        path = Path(original)
        alternate = f'{path.stem}-{collision}{path.suffix}'
        name = segment_member_name(alternate, ordinal, extension)
    return name


def _provenance_base(members: dict[str, Path], checksums: dict[str, tuple[str, int]],
                     max_bytes: int, method: str) -> tuple[dict, str]:
    index_name = _INDEX_NAME
    while index_name in members:
        index_name = '_' + index_name
    inventory = [(name, *checksums[name]) for name in sorted(members)]
    return {
        'schema': 'codex-export.archive-part.v1', 'export_id': uuid.uuid4().hex,
        'max_archive_bytes': max_bytes, 'requested_method': method,
        'original_member_count': len(members),
        'inventory_encoding': 'UTF-8 JSON [filename,sha256,bytes] rows sorted by filename; ensure_ascii=false; no spaces',
        'inventory_sha256': hashlib.sha256(json.dumps(inventory, ensure_ascii=False,
                                                     separators=(',', ':')).encode('utf-8')).hexdigest(),
        'packing_strategy': 'measured compressed sizes; first-fit decreasing with bounded exact packing for modest inventories',
        'reassembly': 'Collect all parts with this export_id. For each original filename, concatenate members '
                      'in offset order; require no gaps/overlaps and verify original_bytes and original_sha256. '
                      'Byte fragments may split a record or UTF-8 character; reassemble them before parsing.',
    }, index_name


def _pack_pieces(pieces: list[_Piece], base: dict, index_name: str,
                 max_bytes: int, *, diagnostics: dict | None = None) -> list[list[_Piece]]:
    bins: list[list[_Piece]] = []
    sizes: list[int] = []
    weighted = [(piece, _info_weight(piece.info) + len(_json_bytes(piece.record())))
                for piece in pieces]
    for piece, weight in sorted(weighted, key=lambda item: item[1], reverse=True):
        for index, group in enumerate(bins):
            extra_zip64 = 76 if len(group) + 1 == zipfile.ZIP_FILECOUNT_LIMIT else 0
            proposed = sizes[index] + weight + extra_zip64
            if proposed <= max_bytes:
                group.append(piece)
                sizes[index] = proposed
                break
        else:
            proposed = _planned_size(base, [piece], index_name)
            if proposed > max_bytes:
                raise RuntimeError('A compressed segment plus its provenance exceeds the archive cap')
            bins.append([piece])
            sizes.append(proposed)
    initial_count = len(bins)
    payload_lower = max(1, (sum(piece.info.compress_size for piece in pieces) + max_bytes - 1) // max_bytes)
    stats = {'algorithm': 'first_fit_decreasing', 'initial_parts': initial_count,
             'compressed_payload_lower_bound': payload_lower, 'search_nodes': 0,
             'search_exhausted': False, 'fixed_piece_minimum_proved': initial_count == payload_lower}
    # ZIP compression streams are already fixed. For modest inventories, search
    # the remaining packing problem with actual final counter widths and a hard
    # node budget. This can improve FFD without any extra compression or disk I/O.
    ordered = sorted(weighted, key=lambda item: item[1], reverse=True)
    if len(pieces) <= 28 and len(pieces) < zipfile.ZIP_FILECOUNT_LIMIT and initial_count > payload_lower:
        total_weight = sum(weight for _, weight in ordered)
        node_budget = 100_000
        all_smaller_excluded = True
        for desired in range(payload_lower, initial_count):
            capacities = []
            for number in range(1, desired + 1):
                content = _json_bytes(_metadata(base, [], number, desired))
                empty_size = 22 + 30 + 46 + 2 * len(index_name.encode('ascii')) + len(content)
                capacities.append(max_bytes - empty_size + 1)
            if total_weight > sum(capacities) or ordered[0][1] > max(capacities):
                continue
            remaining = list(capacities)
            groups: list[list[_Piece]] = [[] for _ in capacities]
            failed = set()
            exhausted = False

            def place(index: int) -> bool:
                nonlocal exhausted
                stats['search_nodes'] += 1
                if stats['search_nodes'] > node_budget:
                    exhausted = True
                    return False
                if index == len(ordered):
                    return True
                state = (index, tuple(sorted(remaining)))
                if state in failed:
                    return False
                piece, weight = ordered[index]
                seen_capacity = set()
                for group_index in sorted(range(desired), key=lambda k: remaining[k]):
                    available = remaining[group_index]
                    if available < weight or available in seen_capacity:
                        continue
                    seen_capacity.add(available)
                    remaining[group_index] -= weight
                    groups[group_index].append(piece)
                    if place(index + 1):
                        return True
                    groups[group_index].pop()
                    remaining[group_index] += weight
                    if exhausted:
                        return False
                failed.add(state)
                return False

            if place(0):
                bins = groups
                stats['algorithm'] = 'bounded_exact_packing'
                stats['fixed_piece_minimum_proved'] = all_smaller_excluded
                break
            if exhausted:
                stats['search_exhausted'] = True
                all_smaller_excluded = False
                break
        else:
            # All smaller counts were excluded for these encoded pieces and
            # their actual metadata, even if the payload-only lower bound was loose.
            stats['fixed_piece_minimum_proved'] = all_smaller_excluded
    stats['parts'] = len(bins)
    stats['scope'] = 'Already encoded pieces, independent ZIP headers and reconstruction metadata; not alternative codecs or segmentation choices'
    if diagnostics is not None:
        diagnostics.update(stats)
    return bins


def _verify_standalone(part: BoundedPart, checksums: dict[str, tuple[str, int]],
                       method: str, reporter) -> None:
    requested = zipfile.ZIP_LZMA if method.upper() == 'LZMA' else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(part.path) as archive:
        infos = archive.infolist()
        if (len(infos) != len(checksums) or {info.filename for info in infos} != set(checksums)
                or any(not _safe_name(info.filename) or info.is_dir() or info.flag_bits & 1
                       or info.compress_type not in (requested, zipfile.ZIP_STORED) for info in infos)):
            raise RuntimeError('ZIP member inventory or requested codec mismatch')
        if (sum(info.file_size for info in infos) >= 65_536
                and all(info.compress_type == zipfile.ZIP_STORED for info in infos)):
            raise RuntimeError('Archive compressor stored all substantial payloads without the requested codec')
        part.member_methods = {info.filename: _METHODS[info.compress_type] for info in infos}
        with _stage(reporter, 'Verify complete archive', total=sum(size for _, size in checksums.values()),
                    detail='Decode every member; verify CRC, byte counts and SHA-256') as meter:
            done = 0
            for info in infos:
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(CHUNK_BYTES), b''):
                        digest.update(block)
                        size += len(block)
                        done += len(block)
                        if meter is not None:
                            meter.update(done, detail=info.filename)
                if (digest.hexdigest(), size) != checksums[info.filename]:
                    raise RuntimeError(f'Archive member content mismatch: {info.filename}')


def build_bounded_archives(members: dict[str, Path], *, work: Path, basename: str,
                           max_bytes: int, method: str, compress_range: Callable,
                           reporter=None, checksums: dict[str, tuple[str, int]] | None = None,
                           consume_candidates: bool = True,
                           estimated_ratios: dict[str, float] | None = None,
                           compression_profiles: dict[str, Any] | None = None) -> list[BoundedPart]:
    """Compress bounded ranges directly, then copy their encoded streams once.

    A highly compressible large input remains a complete member and can still
    produce one ordinary ZIP. No full oversized ZIP or raw range copies are
    required. Compressed candidates are released as their assembled ZIP takes
    ownership of the payload. Every original byte is decoded and checked before
    any final publication. Packing uses actual sizes; global optimality is not
    claimed for either arbitrary compression ratios or the bin-packing problem.
    """
    from .archive_stream import RangeTooLarge
    from .storage import require_space
    validate_max_bytes(max_bytes)
    if not members or any(not _safe_name(name) for name in members):
        raise ValueError('Bounded archiving requires distinct flat filenames')
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if checksums is None:
        checksums = {}
        total = sum(path.stat().st_size for path in members.values())
        with _stage(reporter, 'Checksum archive inputs', total=total,
                    detail='Hash original members before compression; no duplicate source files') as meter:
            done = 0
            for name, source in members.items():
                digest = hashlib.sha256()
                size = 0
                with source.open('rb') as stream:
                    for block in iter(lambda: stream.read(CHUNK_BYTES), b''):
                        digest.update(block)
                        size += len(block)
                        done += len(block)
                        if meter is not None:
                            meter.update(done, detail=name)
                checksums[name] = (digest.hexdigest(), size)
    if set(checksums) != set(members):
        raise ValueError('Checksums must describe exactly the archive members')
    base, index_name = _provenance_base(members, checksums, max_bytes, method)
    pieces: list[_Piece] = []
    candidates: set[Path] = set()
    used_names = set(members) | {index_name}
    def encode_member(original_name: str, source: Path, number: int, *,
                      force_piece_budget: bool = False) -> list[_Piece]:
        encoded = []
        original_hash, original_size = checksums[original_name]
        offset = 0
        ordinal = 0
        ratio_estimate = (estimated_ratios or {}).get(original_name)
        profile = (compression_profiles or {}).get(original_name)
        correction = 1.0
        next_target = None
        if ratio_estimate and original_size * ratio_estimate > max_bytes * .95:
            next_target = max(1, int(max_bytes / max(ratio_estimate, .000001) * .97))
        while offset < original_size or (original_size == 0 and ordinal == 0):
            ordinal += 1
            remaining = original_size - offset
            if profile is not None:
                next_target = profile.range_bytes(offset, max_bytes * .95, correction=correction)
                # Give a plausibly fitting complete member the ordinary ZIP path.
                if offset == 0 and profile.estimate() * correction <= max_bytes * .95:
                    next_target = remaining
            target = min(remaining, next_target) if next_target is not None else remaining
            attempts = 0
            # Only partial pieces need reconstruction metadata. A complete
            # original near the cap must first get a chance to fit an ordinary
            # ZIP, without paying for unnecessary multipart metadata.
            provisional_name = segment_member_name(original_name, ordinal, Path(original_name).suffix + '.bytes')
            placeholder = zipfile.ZipInfo(provisional_name)
            placeholder.file_size = original_size
            placeholder.compress_size = 0
            proposed = _Piece(provisional_name, original_name, source, original_size,
                              original_size, '0' * 64, original_size, original_hash,
                              work / 'unused', placeholder, False, False)
            metadata_budget = _planned_size(base, [proposed], index_name) - _info_weight(placeholder)
            piece_limit = max_bytes - metadata_budget - max(0, len(provisional_name.encode('utf-8')) * 2)
            if piece_limit < 256:
                raise ValueError('Archive byte limit is too small for required member provenance')
            while True:
                end = _line_end(source, offset, target, original_size) if original_size else 0
                count = end - offset
                whole_attempt = offset == 0 and count == original_size and not force_piece_budget
                limit = max_bytes if whole_attempt else piece_limit
                marker = 'refined' if force_piece_budget else 'candidate'
                candidate = work / f'{marker}-{number:04d}-{ordinal:06d}-{attempts:02d}.zip'
                try:
                    compressed = compress_range(candidate, original_name, source, offset, count, limit)
                    actual = compressed.size_bytes
                    if actual > count or (remaining and actual < 1):
                        raise RuntimeError('Range compressor returned an invalid source byte count')
                    end = offset + actual
                    starts, ends = _line_boundaries(source, offset, end, original_size)
                    whole = offset == 0 and actual == original_size
                    extension = Path(original_name).suffix
                    if not whole and not (starts and ends and extension.lower() in ('.jsonl', '.md', '.txt')):
                        extension += '.bytes'
                    name = original_name if whole else unique_segment_member_name(
                        original_name, ordinal, extension, used_names)
                    info = _read_info(candidate, original_name)
                    info.filename = name
                    info.orig_filename = name
                    piece = _Piece(name, original_name, source, offset, actual, compressed.sha256,
                                   original_size, original_hash, candidate, info, starts, ends)
                    planned = _planned_size(base, [piece], index_name)
                    if planned > max_bytes and not (whole and whole_attempt):
                        raise RangeTooLarge(actual, planned, max_bytes)
                except RangeTooLarge as overflow:
                    if overflow.cleanup_error is not None:
                        raise
                    candidate.unlink(missing_ok=True)
                    attempts += 1
                    if count <= 1:
                        raise ValueError('Archive byte limit cannot hold member metadata and one source byte') from overflow
                    consumed = overflow.input_bytes or min(count, max_bytes // 2)
                    target = max(1, min(count - 1,
                        int(consumed * piece_limit / max(piece_limit + 1, overflow.output_bytes) * .94)))
                    if attempts >= 3:
                        target = min(target, max(1, max_bytes // 2))
                    continue
                encoded.append(piece)
                candidates.add(candidate)
                used_names.add(name)
                offset = end
                # Carry an observed ratio forward instead of repeatedly feeding
                # the whole remaining source into another oversized candidate.
                if actual:
                    overhead = candidate.stat().st_size - info.compress_size
                    ratio = max(info.compress_size / actual, 0.000001)
                    if profile is not None:
                        predicted = profile.estimate(piece.offset, actual)
                        correction = max(.5, min(4.0, info.compress_size / max(1, predicted)))
                    next_target = max(1, int(max(1, piece_limit - overhead) / ratio * .985))
                break
        return encoded

    for number, (original_name, source) in enumerate(members.items(), 1):
        pieces.extend(encode_member(original_name, source, number))
    whole = all(piece.offset == 0 and piece.size == piece.original_size for piece in pieces)
    plain_size = 22 + sum(_info_weight(piece.info) for piece in pieces)
    if len(pieces) > zipfile.ZIP_FILECOUNT_LIMIT:
        plain_size += 76

    def release(group: list[_Piece]) -> None:
        if not consume_candidates:
            return
        for piece in group:
            if piece.archive in candidates:
                piece.archive.unlink()
                candidates.remove(piece.archive)

    if whole and plain_size <= max_bytes:
        path = work / Path(basename).name
        require_space(work, plain_size, stage='assemble single verified archive')
        with _stage(reporter, 'Assemble archive', total=len(pieces), unit='files',
                    detail='Reuse compressed members; ordinary ZIP with original filenames') as meter:
            with zipfile.ZipFile(path, 'x', allowZip64=True) as archive:
                for number, piece in enumerate(pieces, 1):
                    _copy_compressed(archive, piece)
                    if meter is not None:
                        meter.update(number, detail=piece.name)
        if path.stat().st_size > max_bytes:
            raise RuntimeError('Final archive exceeded the byte cap; nothing published')
        release(pieces)
        part = BoundedPart(path, {}, _pieces=pieces)
        _verify_standalone(part, checksums, method, reporter)
        return [part]
    # A complete member just below the byte cap might fit by itself but not
    # with the per-part index. Resegment only that rare boundary offender, after
    # proving that all complete members cannot share one ordinary archive.
    refined = []
    for number, piece in enumerate(pieces, 1):
        if _planned_size(base, [piece], index_name) > max_bytes:
            piece.archive.unlink()
            candidates.remove(piece.archive)
            refined.extend(encode_member(piece.original, piece.source, len(members) + number,
                                         force_piece_budget=True))
        else:
            refined.append(piece)
    pieces = refined
    packing = {}
    groups = _pack_pieces(pieces, base, index_name, max_bytes, diagnostics=packing)
    paths = archive_destinations(work / Path(basename).name, len(groups))
    parts: list[BoundedPart] = []
    with _stage(reporter, 'Assemble independent ZIP parts', total=len(groups), unit='files',
                detail='Copy compressed streams once; retire each owned candidate after use') as meter:
        for number, (path, group) in enumerate(zip(paths, groups), 1):
            require_space(work, _planned_size(base, group, index_name),
                          stage=f'assemble archive part {number} of {len(groups)}')
            metadata = _metadata(base, group, number, len(groups))
            _assemble(path, group, index_name, metadata)
            if path.stat().st_size > max_bytes:
                raise RuntimeError('Final ZIP size exceeded the cap; nothing published')
            release(group)
            parts.append(BoundedPart(path, {}, metadata, group, packing=packing))
            if meter is not None:
                meter.update(number, detail=f'{path.name}: {path.stat().st_size:,} bytes')
    _verify_parts(parts, checksums, index_name, max_bytes, method, reporter)
    return parts
