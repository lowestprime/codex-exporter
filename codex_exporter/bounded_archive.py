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

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

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
                name = f'segment-{number:04d}-{ordinal:06d}{extension}'
                while name in used_names:
                    name = '_' + name
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
                  reporter=None, publisher=None) -> tuple[list[Path], list[str]]:
    """Publish only a fully verified set, rolling back our own files on failure."""
    from .file_access import publish_file
    validate_max_bytes(max_bytes)
    publisher = publisher or publish_file
    destinations = archive_destinations(destination, len(parts))
    for part, target in zip(parts, destinations):
        if part.size_bytes > max_bytes:
            raise RuntimeError('Refusing to publish an archive over its byte limit')
        if target.exists():
            raise FileExistsError(f'Refusing to replace an existing ZIP: {target}')
    published: list[tuple[Path, tuple]] = []
    strategies: list[str] = []
    try:
        for part, target in zip(parts, destinations):
            strategies.append(publisher(part.path, target, reporter=reporter))
            info = target.stat()
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            published.append((target, signature))
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
