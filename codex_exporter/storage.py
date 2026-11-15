"""Measured space checks for exporter-owned work and publication directories.

Compression has no generally reliable ratio forecast. Check exact allocations
when known, report the selected volumes, and never delete unrelated exports to
make room. A failed export is cleaned by its owning transaction context.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil

RESERVE_BYTES = 8 * 1024 * 1024


class StorageSpaceError(OSError):
    """An actionable disk-space error without an invented compression forecast."""


def work_directory_parent(output_parent: Path, configured: str | Path = '') -> Path:
    selected = Path(configured).expanduser() if configured else Path(output_parent)
    selected = selected.resolve()
    selected.mkdir(parents=True, exist_ok=True)
    if not selected.is_dir():
        raise NotADirectoryError(f'Exporter work directory is not a directory: {selected}')
    return selected


def _existing_directory(directory: Path) -> Path:
    selected = Path(directory).resolve()
    while not selected.exists() and selected != selected.parent:
        selected = selected.parent
    return selected if selected.is_dir() else selected.parent


def _free(directory: Path) -> int | None:
    try:
        return shutil.disk_usage(_existing_directory(directory)).free
    except OSError:
        return None


def same_volume(left: Path, right: Path) -> bool:
    """Compare actual mounted devices, including Windows volume identities."""
    try:
        return _existing_directory(left).stat().st_dev == _existing_directory(right).stat().st_dev
    except OSError:
        return False


def require_space(directory: Path, required_bytes: int, *, stage: str,
                  reserve_bytes: int = RESERVE_BYTES) -> dict:
    """Check a known next allocation; this is a check, not a disk reservation."""
    if (isinstance(required_bytes, bool) or not isinstance(required_bytes, int)
            or required_bytes < 0 or reserve_bytes < 0):
        raise ValueError('Space requirements must be nonnegative integer byte counts')
    directory = Path(directory).resolve()
    available = _free(directory)
    needed = required_bytes + reserve_bytes
    plan = {'directory': str(directory), 'required_bytes': required_bytes,
            'reserve_bytes': reserve_bytes, 'available_bytes': available}
    if available is not None and available < needed:
        raise StorageSpaceError(errno.ENOSPC,
            f'Insufficient free disk space for {stage}: {directory}. '
            f'Need at least {needed:,} additional bytes including {reserve_bytes:,} bytes of headroom; '
            f'{available:,} bytes are available. Free space on this volume or select '
            f'--work-dir on a larger volume; the final --out-dir also needs space for the verified ZIP set. '
            'The live source is retained.')
    return plan


def storage_failure(error: BaseException, *, stage: str, work_dir: Path,
                    output_dir: Path) -> BaseException:
    """Annotate actual ENOSPC/Windows disk-full failures without masking others."""
    if isinstance(error, StorageSpaceError):
        return error
    disk_full = (isinstance(error, OSError)
                 and (error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1))
                      or getattr(error, 'winerror', None) in (39, 112)))
    # External archivers report a failed filesystem write in their bounded log.
    text = str(error).casefold()
    if not disk_full and not any(marker in text for marker in (
            'no space left on device', 'not enough space on the disk', 'disk full',
            'there is not enough space', 'disk quota exceeded')):
        return error
    work_dir, output_dir = Path(work_dir).resolve(), Path(output_dir).resolve()
    details = []
    for label, directory in (('work', work_dir), ('output', output_dir)):
        available = _free(directory)
        free = f'{available:,} bytes free' if available is not None else 'free space unavailable'
        details.append(f'{label}: {directory} ({free})')
    return StorageSpaceError(errno.ENOSPC,
        f'Disk space ran out during {stage}; ' + '; '.join(details) + '. '
        'Free space or choose --work-dir on a larger volume and an --out-dir with room for the final archives. '
        'Incomplete files owned by this export are discarded; the live source and prior completed exports are retained. '
        f'Original error: {error}')


def private_prefix(stem: str, stage: str) -> str:
    """Keep a recognizable export prefix without exhausting path-length limits."""
    safe = ''.join(ch for ch in stem if ch not in '/\\\x00')
    prefix = safe.encode('utf-8')[:96].decode('utf-8', errors='ignore').rstrip(' .')
    return f'.{prefix or "codex-export"}.{stage}-'


from contextlib import contextmanager


@contextmanager
def error_context(*, stage: str, work_dir: Path, output_dir: Path):
    """Capture volume state at failure, before the owning transaction cleans up."""
    try:
        yield
    except BaseException as error:
        enriched = storage_failure(error, stage=stage, work_dir=work_dir, output_dir=output_dir)
        if enriched is not error:
            raise enriched from error
        raise
