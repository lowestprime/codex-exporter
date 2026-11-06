"""Verified, bounded snapshots of append-only Codex rollouts.

Capture a fixed prefix through one open handle, verify it, then stop consulting
app-owned files. This is an append-only consistency check, not an OS atomic
snapshot or protection against a hostile writer changing bytes between checks.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, BinaryIO, Iterator

CHUNK_BYTES = 4 * 1024 * 1024


@dataclass
class _Digest:
    digest: Any = field(default_factory=hashlib.sha256)
    complete_digest: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    complete_size: int = 0

    def update(self, data: bytes) -> None:
        last_newline = data.rfind(b'\n')
        if last_newline >= 0:
            self.digest.update(data[:last_newline + 1])
            self.complete_digest = self.digest.copy()
            self.complete_size = self.size + last_newline + 1
            self.digest.update(data[last_newline + 1:])
        else:
            self.digest.update(data)
        self.size += len(data)


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    original_path: Path
    sha256: str
    size_bytes: int
    metadata: dict[str, Any]
    _owned_directory: Path = field(repr=False)

    def owns_staged_path(self) -> bool:
        """Only this private staging file is eligible for source-copy promotion."""
        return (self.path.parent == self._owned_directory
                and self.path.resolve() != self.original_path.resolve()
                and self.path.is_file()
                and not self.path.is_symlink())


def _progress(label: str, done: int, total: int, begin: float) -> None:
    elapsed = max(time.monotonic() - begin, .001)
    print(f'\rCodex {label}: {100 * done / total if total else 100:5.1f}%  '
          f'{done / 1048576:,.1f}/{total / 1048576:,.1f} MiB  '
          f'{done / elapsed / 1048576:,.1f} MiB/s',
          end='\n' if done >= total else '', file=sys.stderr, flush=True)


def _copy_prefix(source: BinaryIO, target: BinaryIO, size: int, *, progress: bool = False) -> _Digest:
    """Never chase a moving EOF, including while the caller keeps appending."""
    state = _Digest()
    begin = last = time.monotonic()
    while state.size < size:
        data = source.read(min(CHUNK_BYTES, size - state.size))
        if not data:
            raise RuntimeError('Codex source was truncated during snapshot capture; retry the export')
        target.write(data)
        state.update(data)
        now = time.monotonic()
        if progress and now - last >= 1:
            _progress('snapshot', state.size, size, begin)
            last = now
    if progress:
        _progress('snapshot', state.size, size, begin)
    return state


def _verify_prefix(source: BinaryIO, size: int, expected: str, *, progress: bool = False) -> None:
    source.seek(0)
    digest = hashlib.sha256()
    done = 0
    begin = last = time.monotonic()
    while done < size:
        data = source.read(min(CHUNK_BYTES, size - done))
        if not data:
            raise RuntimeError('Codex source was truncated during snapshot verification; retry the export')
        digest.update(data)
        done += len(data)
        now = time.monotonic()
        if progress and now - last >= 1:
            _progress('verify', done, size, begin)
            last = now
    if digest.hexdigest() != expected:
        raise RuntimeError('Codex source prefix was rewritten during snapshot capture; retry the export')
    if progress:
        _progress('verify', done, size, begin)


def _decode_zstd(source: Path, target: Path) -> _Digest:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError('Compressed Codex rollouts require the optional zstandard dependency') from exc
    state = _Digest()
    decoder = None
    frames = 0
    with source.open('rb') as physical, target.open('xb') as output:
        # Small compressed chunks bound output bursts. Unlike stream_reader,
        # decompressobj exposes EOF so incomplete frames cannot look successful.
        while data := physical.read(1024):
            while data:
                if decoder is None:
                    decoder = zstd.ZstdDecompressor().decompressobj()
                decoded = decoder.decompress(data)
                output.write(decoded)
                state.update(decoded)
                if decoder.eof:
                    frames += 1
                    data = decoder.unused_data
                    decoder = None
                else:
                    data = b''
        if decoder is not None or not frames:
            raise RuntimeError('Incomplete Zstandard frame in captured Codex rollout; retry after compression completes')
        output.flush()
        os.fsync(output.fileno())
    return state


def _finish_records(path: Path, state: _Digest) -> tuple[str, int, int, str | None]:
    """Keep valid EOF JSON, defer an invalid unterminated tail with an audit hash."""
    if state.size == state.complete_size:
        return state.digest.hexdigest(), state.size, 0, None
    with path.open('r+b') as stream:
        stream.seek(state.complete_size)
        tail = stream.read()  # At most one JSONL record; never the whole thread by design.
        valid = False
        try:
            text = tail.decode('utf-8')
            if not text.strip():
                valid = True
            else:
                json.loads(text)
                valid = True
        except UnicodeError:
            pass
        except ValueError:
            # Keep the renderer's narrowly repairable complete EOF records too;
            # otherwise adding a newline would unexpectedly change coverage.
            from .cli import parse_jsonl_line
            repaired, _ = parse_jsonl_line(text)
            valid = repaired is not None
        if valid:
            return state.digest.hexdigest(), state.size, 0, None
        if not state.complete_size:
            raise RuntimeError('No complete JSONL record is available at the snapshot boundary; retry after the writer finishes a record')
        deferred_sha = hashlib.sha256(tail).hexdigest()
        stream.truncate(state.complete_size)
        stream.flush()
        os.fsync(stream.fileno())
    return (state.complete_digest.hexdigest(), state.complete_size,
            state.size - state.complete_size, deferred_sha)


@contextmanager
def snapshot_source(source: Path, *, directory: Path | None = None,
                    progress: bool = False) -> Iterator[SourceSnapshot]:
    """Freeze a verified complete-record prefix; clean staging on every exit.

    The cutoff is the open handle's first fstat size. Later appends are left for
    the next export. Captured bytes are compared to a second bounded read from
    that same handle; observed truncation, replacement and rewrites fail before
    any export is rendered. Changes after this context yields cannot affect it.
    """
    source = source.expanduser().resolve()
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.codex-snapshot-', dir=directory) as temp:
        staging = Path(temp)
        captured = staging / 'snapshot.jsonl'
        compressed = source.name.lower().endswith('.jsonl.zst')
        physical = staging / 'source.jsonl.zst' if compressed else captured
        started = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        with source.open('rb', buffering=0) as original:
            before = os.fstat(original.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Codex rollout must be a regular file')
            if shutil.disk_usage(staging).free < before.st_size + 8 * 1024 * 1024:
                raise OSError('Insufficient free disk space to capture the Codex snapshot')
            with physical.open('xb') as output:
                state = _copy_prefix(original, output, before.st_size, progress=progress)
                output.flush()
                os.fsync(output.fileno())
            physical_sha = state.digest.hexdigest()
            _verify_prefix(original, before.st_size, physical_sha, progress=progress)
            after = os.fstat(original.fileno())
            try:
                current = source.stat()
            except FileNotFoundError as exc:
                raise RuntimeError('Codex source was removed or replaced during snapshot capture; retry the export') from exc
            if ((before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                raise RuntimeError('Codex source was replaced during snapshot capture; retry the export')
            if after.st_size < before.st_size or current.st_size < before.st_size:
                raise RuntimeError('Codex source was truncated during snapshot capture; retry the export')
        if compressed:
            state = _decode_zstd(physical, captured)
            physical.unlink()
        sha, included_bytes, deferred, deferred_sha = _finish_records(captured, state)
        metadata: dict[str, Any] = {
            'policy': 'verified_prefix',
            'original_source': str(source),
            'source_encoding': 'zstandard' if compressed else 'plain',
            'capture_started_at': started,
            'capture_completed_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
            'source_size_bytes_at_open': before.st_size,
            'source_size_bytes_after_capture': after.st_size,
            'source_mtime_ns_at_open': before.st_mtime_ns,
            'source_mtime_ns_after_capture': after.st_mtime_ns,
            'physical_prefix_sha256': physical_sha,
            'decoded_prefix_bytes': state.size,
            'captured_size_bytes': included_bytes,
            'captured_sha256': sha,
            'deferred_tail_bytes': deferred,
            'deferred_tail_sha256': deferred_sha,
            'deferred_tail_reason': 'invalid_or_incomplete_unterminated_json' if deferred else None,
            'source_changed_during_capture': (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns),
            'appended_bytes_during_capture': max(0, after.st_size - before.st_size),
            'prefix_verified': True,
            'consistency': 'append_only_prefix_verified_by_two_reads; not_an_atomic_filesystem_snapshot',
        }
        if deferred:
            print(f'NOTE: Snapshot deferred {deferred:,} trailing bytes from an unfinished or invalid final JSONL record; '
                  'the original is unchanged and the next export can capture its completion.', file=sys.stderr)
        yield SourceSnapshot(captured, source, sha, included_bytes, metadata, staging)
