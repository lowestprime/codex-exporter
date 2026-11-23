"""Compress exact file ranges without staging duplicate uncompressed segments.

7-Zip reads the selected range through standard input, using the requested ZIP
codec and every configured tuning option. The output monitor can finish a range
near the measured byte budget. A final over-budget result is retried at a smaller
input boundary; no estimated compression ratio is treated as a size guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING
import zipfile

from .progress import ProgressReporter, SEVEN_ZIP_PROGRESS_ARGS, run_seven_zip
from .storage import require_space

if TYPE_CHECKING:
    from .archive_pipeline import ArchiveSettings

CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CompressedRange:
    size_bytes: int
    sha256: str


class RangeTooLarge(RuntimeError):
    def __init__(self, input_bytes: int, output_bytes: int, limit: int):
        super().__init__(f'Compressed range exceeded its {limit:,}-byte planning budget')
        self.input_bytes = input_bytes
        self.output_bytes = output_bytes
        self.limit = limit
        self.cleanup_error: OSError | None = None


def _range_chunks(source: Path, offset: int, count: int, stop_input: threading.Event,
                  state: dict, *, prefer_lines: bool):
    with source.open('rb') as original:
        original.seek(offset)
        remaining = count
        while remaining and not stop_input.is_set():
            block = original.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise RuntimeError('Export member was truncated during range compression')
            # Stopping after an ordinary record preserves independently parseable
            # JSONL. Giant records remain explicit, exactly reconstructable bytes.
            if prefer_lines and len(block) < remaining:
                boundary = block.rfind(b'\n')
                if boundary >= 0 and boundary + 1 < len(block):
                    original.seek(boundary + 1 - len(block), os.SEEK_CUR)
                    block = block[:boundary + 1]
            yield block
            state['digest'].update(block)
            state['bytes'] += len(block)
            remaining -= len(block)


def compress_range(destination: Path, name: str, source: Path, offset: int, count: int,
                   limit: int, *, settings: ArchiveSettings, backend: str,
                   archiver: Path | None = None, reporter=None,
                   adaptive: bool = True, stage_label: str = 'Compress archive member') -> CompressedRange:
    """Create one ZIP member from a range and report exactly the bytes supplied.

    ``limit`` is an internal candidate budget; final archive metadata is accounted
    for separately by the packer. Only completed, independently verified ZIPs
    under the configured final cap can be published.
    """
    if offset < 0 or count < 0 or limit < 1:
        raise ValueError('Range offsets, counts and compression budgets must be valid')
    # The external encoder runs with destination.parent as cwd. Absolute paths
    # keep a relative request from accidentally repeating that directory.
    destination, source = Path(destination).resolve(), Path(source).resolve()
    if destination.exists():
        raise FileExistsError(f'Refusing to replace a compression candidate: {destination}')
    require_space(destination.parent, min(1024 * 1024, max(0, count)),
                  stage='start streamed archive compression')
    reporter = reporter or ProgressReporter(enabled=False)
    stop_input = threading.Event()
    state = {'bytes': 0, 'digest': hashlib.sha256()}
    cut_at = max(1, limit - max(256, min(1024 * 1024, limit // 100)))
    chunks = _range_chunks(source, offset, count, stop_input, state,
                           prefer_lines=source.suffix.lower() in ('.jsonl', '.md', '.txt'))

    def monitor() -> None:
        try:
            current = destination.stat().st_size
        except FileNotFoundError:
            return
        meter.update(observations={'output_bytes': current})
        if current > limit:
            raise RangeTooLarge(state['bytes'], current, limit)
        if adaptive and state['bytes'] and current >= cut_at:
            stop_input.set()

    try:
        python_backend = backend == 'python'
        detail = (f'{name}; streamed range at byte {offset:,}' if python_backend else
                  f'7-Zip: {name}; source offset {offset:,}')
        with reporter.stage(stage_label, total=count, unit='bytes', detail=detail,
                            progress_basis='measured_work' if python_backend else '7zip_processed_bytes',
                            retry_exceptions=(RangeTooLarge,) if adaptive else ()) as meter:
            if python_backend:
                with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=settings.level, allowZip64=True) as archive:
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    info._compresslevel = settings.level
                    with archive.open(info, 'w', force_zip64=True) as output:
                        for block in chunks:
                            output.write(block)
                            meter.update(state['bytes'] + len(block))
                            # Flush only the file buffer, never the codec: no
                            # forced flush blocks or ratio penalty are introduced.
                            archive.fp.flush()
                            monitor()
            else:
                if archiver is None:
                    raise ValueError('External range compression requires a 7-Zip executable')
                method = 'LZMA' if settings.method.upper() == 'LZMA' else 'Deflate'
                command = [str(archiver), 'a', '-tzip', f'-mx={settings.level}', f'-mm={method}',
                           f'-mmt={settings.threads}', '-y', *SEVEN_ZIP_PROGRESS_ARGS]
                if method == 'LZMA':
                    configured = int(settings.dictionary[:-1]) * (1024 ** {'k': 1, 'm': 2, 'g': 3}[settings.dictionary[-1].lower()])
                    # Stdin has unknown size. Limit encoder history to the known
                    # input range; bytes beyond the range cannot improve matches.
                    effective = max(4096, min(configured, count))
                    dictionary = settings.dictionary if effective >= configured else f'{effective}b'
                    command.extend([f'-md={dictionary}', f'-mfb={settings.word_size}'])
                command.extend([f'-si{name}', str(destination)])
                completed = run_seven_zip(command, cwd=destination.parent, progress=meter,
                                          input_chunks=chunks, input_size=count, on_tick=monitor,
                                          heartbeat_interval=0.05)
                if completed.returncode:
                    diagnostic = (completed.stdout + '\n' + completed.stderr).strip()[-1600:]
                    raise RuntimeError(f'7-Zip range compression failed ({completed.returncode}): {diagnostic}')
            # Codec flush and ZIP finalization can cross the budget even when
            # every monitor tick fit. Keep these checks in the same stage so
            # their retry/failure status is never falsely reported as complete.
            if count and not state['bytes']:
                raise RuntimeError('Archive compressor accepted no input bytes')
            if not stop_input.is_set() and state['bytes'] != count:
                raise RuntimeError('Archive compressor did not consume the complete requested range')
            size = destination.stat().st_size
            if size > limit:
                raise RangeTooLarge(state['bytes'], size, limit)
            with zipfile.ZipFile(destination) as archive:
                infos = archive.infolist()
                if len(infos) != 1 or infos[0].filename != name or infos[0].file_size != state['bytes']:
                    raise RuntimeError('Archive range compressor returned an unexpected member or byte count')
            meter.update(state['bytes'], total=state['bytes'], phase='checking',
                         progress_basis='verified_member_bytes', progress_resolution=0,
                         detail=f'{name}; {state["bytes"]:,} source bytes compressed')
            return CompressedRange(state['bytes'], state['digest'].hexdigest())
    except BaseException as primary:
        try:
            destination.unlink(missing_ok=True)
        except OSError as cleanup_error:
            # Retain the primary diagnostic, including its concrete exception
            # type. A failed removal is separately visible and makes a budget
            # overflow fatal to callers that would otherwise retry it.
            if isinstance(primary, RangeTooLarge):
                primary.cleanup_error = cleanup_error
            detail = f'Could not remove rejected candidate {destination}: {cleanup_error}'
            if hasattr(primary, 'add_note'):
                primary.add_note(detail)
            cleanup = reporter.stage('Clean rejected compression candidate',
                                     total=1, unit='files', detail=detail)
            cleanup.fail(detail=detail)
            raise primary from cleanup_error
        raise
    finally:
        chunks.close()
