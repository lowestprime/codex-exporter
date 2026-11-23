"""Measured export progress and a bounded, interruptible 7-Zip runner.

All display output goes to stderr.  Counts describe the current stage only:
7-Zip percentages remain percentages; native ``M`` frames expose processed bytes
at 1 MiB resolution. Input-feed fallback is labeled separately. ETA estimates
use recent measured work and become unavailable during unmeasured finalization
or stalls. The default renderer uses a terminal bar, or sparse redirected logs.
"""
from __future__ import annotations

import codecs
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable, Mapping, Sequence, TextIO
import unicodedata

PROGRESS_STYLES = ('auto', 'rich', 'plain', 'json', 'off')
SEVEN_ZIP_PROGRESS_ARGS = ('-bsp1', '-bso1', '-bse1', '-bb1', '-sccUTF-8')
_ANSI = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])')
_PERCENT = re.compile(r'^\s*(\d{1,3}(?:\.\d+)?)%(?:\s+(.*))?$')
_PROCESSED_MIB = re.compile(r'^\s*(\d{1,20})M(?:\s+(.*))?$')
_RATE_WINDOW_SECONDS = 30.0
_RATE_SAMPLE_SECONDS = 0.2
_STALE_PROGRESS_SECONDS = 10.0
_MEMBER = re.compile(r'^\s*[+UT-]\s+(.+)$')
_FRAMES = re.compile(r'[\r\n\b]+')
_SPINNERS = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')


def _clean(text: object, limit: int = 1024) -> str:
    """Do not let source names or archiver output issue terminal commands."""
    value = _ANSI.sub('', str(text))
    value = ''.join(' ' if ch in '\r\n\t' else ch for ch in value
                    if ch in '\r\n\t' or unicodedata.category(ch) not in ('Cc', 'Cf', 'Cs'))
    return ' '.join(value.split())[:limit]


def _cells(text: str) -> int:
    return sum(0 if unicodedata.combining(ch) else
               2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def _clip(text: str, width: int, unicode: bool) -> str:
    if _cells(text) <= width:
        return text
    suffix = '…' if unicode else '...'
    if width <= len(suffix):
        return suffix[:max(0, width)]
    remaining = width - _cells(suffix)
    result: list[str] = []
    for ch in text:
        size = _cells(ch)
        if size > remaining:
            break
        result.append(ch)
        remaining -= size
    return ''.join(result) + suffix


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f'{minutes}m {seconds:02}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02}m {seconds:02}s'


def _byte_scale(value: float) -> tuple[float, str]:
    for exponent, name in reversed(tuple(enumerate(('B', 'KiB', 'MiB', 'GiB', 'TiB')))):
        scale = 1024.0 ** exponent
        if value >= scale or exponent == 0:
            return scale, name
    return 1.0, 'B'


def _amount(value: float, unit: str) -> str:
    if unit == 'bytes':
        scale, label = _byte_scale(value)
        return f'{value / scale:,.1f} {label}' if scale > 1 else f'{value:,.0f} B'
    return f'{value:,.0f} {unit}'


def _finite_count(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{name} must be a finite, nonnegative number')
    return result


def _windows_vt(stream: TextIO) -> bool:
    if os.name != 'nt':
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and
                    kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


class ProgressReporter:
    """A lightweight terminal dashboard, sparse log, or JSON event stream.

    ``auto`` selects the terminal display only for a capable interactive stream.
    ``rich`` requests that same display; redirection and TERM=dumb still fall
    back to plain logs.  NO_COLOR disables colors, and unsupported console
    encodings use ASCII.  Rendering is throttled to five updates per second;
    redirected plain logs emit at most one intermediate update every five
    seconds.  Stage starts, completion, and errors always emit immediately.
    """

    def __init__(self, *, style: str = 'auto', enabled: bool = True,
                 stream: TextIO | None = None, interval: float = 0.2,
                 plain_interval: float = 5.0, clock: Callable[[], float] = time.monotonic,
                 width: int | None = None):
        if style not in PROGRESS_STYLES:
            raise ValueError(f'progress style must be one of {", ".join(PROGRESS_STYLES)}')
        self.stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self.interval = _finite_count(interval, 'interval')
        self.plain_interval = max(self.interval, _finite_count(plain_interval, 'plain_interval'))
        self._width = width
        self.started = clock()
        self._lock = threading.RLock()
        self._stages: list[StageProgress] = []
        self._live_rows = 0
        self._live_stage: StageProgress | None = None
        self._finished = False
        self._detail = ''
        try:
            tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        except (OSError, ValueError):
            tty = False
        terminal = tty and os.environ.get('TERM', '').lower() != 'dumb'
        if not enabled or style == 'off':
            self.style = 'off'
        elif style == 'json':
            self.style = 'json'
        elif style in ('auto', 'rich') and terminal and _windows_vt(self.stream):
            self.style = 'rich'
        else:
            self.style = 'plain'
        self.color = self.style == 'rich' and 'NO_COLOR' not in os.environ
        self.unicode = self.style == 'rich'
        try:
            '⠋✓✕↻━░…'.encode(getattr(self.stream, 'encoding', None) or 'utf-8')
        except (UnicodeEncodeError, LookupError):
            self.unicode = False

    def __enter__(self) -> ProgressReporter:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.finish(success=exc_type is None)

    def stage(self, name: str, *, total: float | None = None, unit: str = 'bytes',
              detail: str = '', progress_basis: str = 'measured_work',
              progress_resolution: float = 0,
              retry_exceptions: tuple[type[Exception], ...] = ()) -> StageProgress:
        if not isinstance(retry_exceptions, tuple) or any(
                not isinstance(kind, type) or not issubclass(kind, Exception)
                for kind in retry_exceptions):
            raise TypeError('retry_exceptions must be a tuple of Exception subclasses')
        with self._lock:
            task = StageProgress(self, name, total=total, unit=unit, detail=detail,
                                 index=len(self._stages) + 1, retry_exceptions=retry_exceptions,
                                 progress_basis=progress_basis,
                                 progress_resolution=progress_resolution)
            self._stages.append(task)
            self._emit(task, 'stage_start', force=True)
        task._start_heartbeat()
        return task

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except UnicodeEncodeError:
            # A legacy Windows code page must not turn successful export into failure.
            try:
                encoding = getattr(self.stream, 'encoding', None) or 'ascii'
                self.stream.write(text.encode(encoding, 'replace').decode(encoding))
                self.stream.flush()
            except (OSError, ValueError, LookupError, UnicodeError):
                self.style = 'off'
        except (OSError, ValueError):
            # Closed stderr or a consumer leaving a pipe is not an export failure.
            self.style = 'off'

    def _paint(self, text: str, color: str) -> str:
        return f'\x1b[{color}m{text}\x1b[0m' if self.color else text

    def _rows(self, task: StageProgress, now: float) -> list[str]:
        stats = task._stats(now)
        done, total, unit = task.completed, task.total, task.unit
        complete = task.status == 'complete'
        failed = task.status == 'failed'
        retry = task.status == 'retry'
        if self.style == 'rich':
            icon = ('✕' if failed else '✓' if complete else '↻' if retry else
                    _SPINNERS[int(stats['elapsed_seconds'] * 5) % len(_SPINNERS)])
            if not self.unicode:
                icon = ('X' if failed else 'OK' if complete else 'RETRY' if retry else
                        '|/-\\'[int(stats['elapsed_seconds'] * 5) % 4])
        else:
            icon = ('[failed]' if failed else '[done]' if complete else '[retry]' if retry else
                    '[start]' if not task._emitted else '[working]')
        header = f'{icon} {task.index:02d} {task.name}'
        metrics = []
        percent = stats['percent']
        if percent is not None:
            scope = 'input ' if task.progress_basis == 'stdin_bytes_sent' else ''
            metrics.append(f'{scope}{percent:.1f}%')
        if unit != 'percent':
            if total is not None:
                if unit == 'bytes':
                    scale, label = _byte_scale(max(done, total))
                    precision = 1 if scale > 1 else 0
                    metrics.append(f'{done / scale:,.{precision}f}/{total / scale:,.{precision}f} {label}')
                else:
                    metrics.append(f'{done:,.0f}/{total:,.0f} {unit}')
            elif done:
                metrics.append(_amount(done, unit))
            if stats['rate_per_second'] is not None:
                metrics.append(_amount(stats['rate_per_second'], unit) + '/s')
        if stats['eta_seconds'] is not None:
            label = 'input ETA ~' if stats['eta_scope'] == 'input_feed' else 'ETA ~'
            metrics.append(label + _duration(stats['eta_seconds']))
        elif task.status == 'running':
            state = stats['eta_status']
            metrics.append({'warming_up': 'ETA estimating',
                            'unknown_total': 'ETA unavailable',
                            'waiting_for_progress': 'ETA waiting for progress',
                            'finalizing': 'finalizing; ETA unavailable'}[state])
        metrics.append('elapsed ' + _duration(stats['elapsed_seconds']))
        if self.style != 'rich':
            return [' | '.join([header, *metrics, *([task.detail] if task.detail else [])])]
        columns = self._width or shutil.get_terminal_size(fallback=(120, 24)).columns
        # Keep two fixed-width rows: details cannot crowd out the actual counters.
        width = max(12, columns - 1)
        header = _clip(header, min(width, max(36, width // 2)), self.unicode)
        available = width - _cells(header) - 3
        if task.detail and available > 4:
            detail = task.detail
            if _cells(detail) > available:
                # Preserve both the file prefix and extension for long export names.
                right = max(1, (available - 3) // 2)
                suffix = ''
                for ch in reversed(detail):
                    if _cells(ch + suffix) > right:
                        break
                    suffix = ch + suffix
                detail = _clip(detail, available - _cells(suffix), self.unicode) + suffix
            header += ' | ' + detail
        body = ' | '.join(metrics)
        if percent is not None:
            bar_width = min(16, max(0, width - 4 - _cells(body) - 1))
            if bar_width >= 6:
                filled = min(bar_width, max(0, int(bar_width * percent / 100)))
                fill, empty = ('━', '░') if self.unicode else ('#', '-')
                body = fill * filled + empty * (bar_width - filled) + ' ' + body
        color = '31' if failed else '32' if complete else '33' if retry else '36'
        return [self._paint(_clip(header, width, self.unicode), color),
                self._paint('    ' + _clip(body, width - 4, self.unicode), color)]

    def _draw(self, rows: list[str], *, final: bool) -> None:
        if self._live_rows > 1:
            self._write(f'\r\x1b[{self._live_rows - 1}A')
        for index, row in enumerate(rows):
            self._write('\r\x1b[2K' + row)
            if index < len(rows) - 1:
                self._write('\n')
        self._live_rows = 0 if final else len(rows)
        if final:
            self._write('\n')

    def _emit(self, task: StageProgress, event: str, *, force: bool = False) -> None:
        if self.style == 'off':
            return
        if not force and any(other.status == 'running' for other in self._stages[task.index:]):
            return  # A nested stage owns the live panel until it finishes.
        now = self._clock()
        interval = self.plain_interval if self.style == 'plain' else self.interval
        if not force and now - task._last_emit < interval:
            return
        if self.style == 'json':
            payload = {'event': event, 'stage': task.name, 'stage_index': task.index,
                       'status': task.status, 'unit': task.unit, 'completed': task.completed,
                       'total': task.total, 'detail': task.detail,
                       'observations': dict(task.observations),
                       'export_elapsed_seconds': max(0.0, now - self.started), **task._stats(now)}
            self._write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + '\n')
        elif self.style == 'rich':
            if self._live_rows and self._live_stage is not task:
                self._write('\n')
                self._live_rows = 0
            self._draw(self._rows(task, now), final=task.status != 'running')
            self._live_stage = task
        else:
            self._write(self._rows(task, now)[0] + '\n')
        task._last_emit = now
        task._emitted = True

    def set_detail(self, detail: str) -> None:
        """Save final-summary context without finishing before resource cleanup."""
        with self._lock:
            if not self._finished:
                self._detail = _clean(detail)

    def finish(self, *, detail: str | None = None, success: bool = True) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            detail = self._detail if detail is None else _clean(detail)
            if self.style == 'off':
                return
            now = self._clock()
            elapsed = max(0.0, now - self.started)
            completed = sum(task.status == 'complete' for task in self._stages)
            failed = sum(task.status == 'failed' for task in self._stages)
            retried = sum(task.status == 'retry' for task in self._stages)
            settled = completed + retried
            success = success and not failed and settled == len(self._stages)
            status = 'complete' if success else 'failed'
            if self.style == 'json':
                self._write(json.dumps({'event': 'export_complete' if success else 'export_failed',
                                        'status': status, 'elapsed_seconds': elapsed,
                                        'stages_completed': completed, 'stages_failed': failed,
                                        'stages_retried': retried, 'stages_settled': settled,
                                        'stages_total': len(self._stages), 'detail': _clean(detail)},
                                       ensure_ascii=True, allow_nan=False) + '\n')
                return
            if self._live_rows:
                self._write('\n')
                self._live_rows = 0
            icon = ('✓' if success else '✕') if self.unicode else ('[done]' if success else '[failed]')
            line = f'{icon} Export {status} | {settled}/{len(self._stages)} stages'
            if retried:
                line += f' | {retried} retried attempt' + ('s' if retried != 1 else '')
            line += f' | total {_duration(elapsed)}'
            if detail:
                line += ' | ' + _clean(detail)
            self._write(self._paint(line, '32' if success else '31') + '\n')


class StageProgress:
    """Measured counters for one stage, including explicitly retryable attempts.

    Retry exceptions are never suppressed. The caller must recover or finish
    the overall export with success=False; only known planning control flow
    should opt into this status. Other exceptions remain permanent failures.
    """

    def __init__(self, reporter: ProgressReporter, name: str, *, total: float | None,
                 unit: str, detail: str, index: int,
                 retry_exceptions: tuple[type[Exception], ...] = (),
                 progress_basis: str = 'measured_work', progress_resolution: float = 0):
        self.reporter = reporter
        self.name = _clean(name, 120)
        self.index = index
        self._retry_exceptions = retry_exceptions
        self.unit = _clean(unit, 32)
        self.total = _finite_count(total, 'total') if total is not None else None
        self.completed = 0.0
        self.detail = _clean(detail)
        self.status = 'running'
        self.progress_basis = _clean(progress_basis, 64)
        self.progress_resolution = _finite_count(progress_resolution, 'progress_resolution')
        self.phase = 'working'
        self.observations: dict[str, float | str | bool | None] = {}
        self.started = reporter._clock()
        self.ended: float | None = None
        self._samples: deque[tuple[float, float]] = deque([(self.started, 0.0)], maxlen=256)
        self._last_advance = self.started
        self._last_emit = -math.inf
        self._emitted = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def _start_heartbeat(self) -> None:
        if self.reporter.style == 'off':
            return

        def beat() -> None:
            while not self._heartbeat_stop.wait(0.2):
                self.heartbeat()

        self._heartbeat_thread = threading.Thread(target=beat, name='codex-stage-progress', daemon=True)
        try:
            self._heartbeat_thread.start()
        except RuntimeError:
            # Progress still updates through measured callbacks if a thread cannot start.
            self._heartbeat_thread = None

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)

    def __enter__(self) -> StageProgress:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.complete()
        elif isinstance(exc_value, self._retry_exceptions):
            self.retry(detail=str(exc_value) or exc_type.__name__)
        else:
            self.fail(detail=str(exc_value) if exc_value else exc_type.__name__)

    def _stats(self, now: float) -> dict:
        current = self.ended if self.ended is not None else now
        elapsed = max(0.0, current - self.started)
        work_percent = 100 * self.completed / self.total if self.total else None
        # Use a recent window, not an entire-job average that remains optimistic
        # when a later, less compressible region becomes much slower.
        reference = self._samples[0]
        for sample in self._samples:
            if sample[0] <= current - _RATE_WINDOW_SECONDS:
                reference = sample
            else:
                break
        span = current - reference[0]
        delta = self.completed - reference[1]
        rate = delta / span if span >= 0.1 and delta > 0 else None
        stale = current - self._last_advance >= _STALE_PROGRESS_SECONDS
        phase = self.phase
        eta = None
        if self.status != 'running':
            eta_status = self.status
            phase = self.status
        elif phase in ('finalizing', 'checking') or (self.total is not None and self.completed >= self.total):
            eta_status, phase = 'finalizing', 'finalizing'
        elif self.total is None:
            eta_status = 'unknown_total'
        elif stale:
            eta_status = 'waiting_for_progress'
        elif not rate or elapsed < 0.1:
            eta_status = 'warming_up'
        else:
            eta_status = 'estimated'
            eta = max(0.0, (self.total - self.completed) / rate)
        if stale and self.status == 'running':
            rate = None
        # Finishing all counted input is not proof that the encoder has flushed,
        # the ZIP has closed or the caller's validation has passed. Keep factual
        # counts/work_percent, but reserve a full stage bar for observed success.
        percent = None if self.status == 'running' and eta_status == 'finalizing' else work_percent
        return {'elapsed_seconds': elapsed, 'percent': percent, 'work_percent': work_percent,
                'rate_per_second': rate if self.unit != 'percent' else None, 'eta_seconds': eta,
                'eta_status': eta_status,
                'eta_scope': 'input_feed' if self.progress_basis == 'stdin_bytes_sent' else 'stage_work',
                'progress_basis': self.progress_basis,
                'progress_resolution': self.progress_resolution, 'phase': phase}

    def update(self, completed: float | None = None, *, total: float | None = None,
               detail: str | None = None, phase: str | None = None,
               progress_basis: str | None = None, progress_resolution: float | None = None,
               observations: Mapping[str, float | str | bool | None] | None = None) -> None:
        with self.reporter._lock:
            if self.status != 'running':
                return
            now = self.reporter._clock()
            basis_changed = progress_basis is not None and progress_basis != self.progress_basis
            if progress_basis is not None:
                self.progress_basis = _clean(progress_basis, 64)
            if progress_resolution is not None:
                self.progress_resolution = _finite_count(progress_resolution, 'progress_resolution')
            if completed is not None:
                value = _finite_count(completed, 'completed')
                if value != self.completed or basis_changed:
                    if value < self.completed or basis_changed:
                        self._samples.clear()
                        self._samples.append((now, value))
                    else:
                        if now - self._samples[-1][0] >= _RATE_SAMPLE_SECONDS:
                            self._samples.append((now, value))
                        while len(self._samples) > 2 and self._samples[1][0] < now - _RATE_WINDOW_SECONDS:
                            self._samples.popleft()
                    if value > self.completed:
                        self._last_advance = now
                    self.completed = value
            if total is not None:
                self.total = _finite_count(total, 'total')
            if detail is not None:
                self.detail = _clean(detail)
            if phase is not None:
                self.phase = _clean(phase, 32)
            if observations is not None:
                if len(observations) > 16:
                    raise ValueError('Progress observations must remain bounded')
                for key, value in observations.items():
                    key = _clean(key, 64)
                    if key not in self.observations and len(self.observations) >= 16:
                        raise ValueError('Progress observations must remain bounded')
                    if isinstance(value, str):
                        value = _clean(value, 256)
                    elif value is not None and not isinstance(value, bool):
                        value = _finite_count(value, key)
                    self.observations[key] = value
            self.reporter._emit(self, 'stage_progress')

    def advance(self, amount: float, *, detail: str | None = None) -> None:
        with self.reporter._lock:
            self.update(self.completed + _finite_count(amount, 'amount'), detail=detail)

    def heartbeat(self) -> None:
        self.update()

    def complete(self, completed: float | None = None, *, detail: str | None = None) -> None:
        with self.reporter._lock:
            if self.status != 'running':
                return
            if completed is not None:
                self.completed = _finite_count(completed, 'completed')
            if detail is not None:
                self.detail = _clean(detail)
            self.status = 'complete'
            self.ended = self.reporter._clock()
            self._heartbeat_stop.set()
            self.reporter._emit(self, 'stage_complete', force=True)
        self._stop_heartbeat()

    def retry(self, *, detail: str | None = None) -> None:
        """End a known retryable attempt without declaring the export successful."""
        with self.reporter._lock:
            if self.status != 'running':
                return
            if detail is not None:
                self.detail = _clean(detail)
            self.status = 'retry'
            self.ended = self.reporter._clock()
            self._heartbeat_stop.set()
            self.reporter._emit(self, 'stage_retry', force=True)
        self._stop_heartbeat()

    def fail(self, *, detail: str | None = None) -> None:
        with self.reporter._lock:
            if self.status != 'running':
                return
            if detail is not None:
                self.detail = _clean(detail)
            self.status = 'failed'
            self.ended = self.reporter._clock()
            self._heartbeat_stop.set()
            self.reporter._emit(self, 'stage_failed', force=True)
        self._stop_heartbeat()


class SevenZipProgressParser:
    """Consume 7-Zip's backspace/CR/newline frames, including split UTF-8 reads.

    The caller supplies decoded text.  A file log line identifies the file being
    processed; it is not treated as proof that that file has finished. Percent
    frames remain percentages. For stdin, native ``M`` frames report processed
    input at 1 MiB resolution and can use the caller's known range denominator.
    """

    def __init__(self, progress: StageProgress | None):
        if progress is not None and progress.unit not in ('percent', 'bytes'):
            raise ValueError("7-Zip progress stages must use unit='percent' or 'bytes'")
        self.progress = progress
        self._pending = ''
        self._member = ''
        self._last_update: tuple[float, str, str] | None = None
        self.measured_basis: str | None = None

    def _record(self, text: str, *, complete: bool) -> None:
        if self.progress is None:
            return
        text = _clean(text, 16384)
        match = _PERCENT.fullmatch(text)
        mib_match = _PROCESSED_MIB.fullmatch(text) if self.progress.unit == 'bytes' else None
        if match or mib_match:
            measured = match or mib_match
            assert measured is not None
            if match:
                amount = float(match.group(1))
                if amount > 100:
                    return
                # Percent callbacks describe encoder work, not byte throughput.
                # Byte stages use native M frames; do not invent finer byte counts
                # by converting an integer percentage back into input bytes.
                if self.progress.unit != 'percent':
                    return
                total, basis, resolution = 100, '7zip_percent', 1
            else:
                # Official 7-Zip and 7-Zip-Zstandard print Completed >> 20 as M
                # when stdin's total is unknown. This is a lower-bound sample,
                # not compressed output bytes and not bytes merely sent to stdin.
                amount = int(measured.group(1)) * 1048576
                if self.progress.total is not None and amount > self.progress.total:
                    return
                total, basis, resolution = self.progress.total, '7zip_processed_bytes', 1048576
            detail = '7-Zip: ' + (self._member or 'compressing members')
            suffix = measured.group(2) or ''
            inline_member = re.match(r'(?:\d+\s+)?[+UT-]\s+(.+)', suffix)
            if inline_member:
                detail = '7-Zip: ' + inline_member.group(1)
            value = (amount, detail, basis)
            self.measured_basis = basis
            if value != self._last_update:
                self.progress.update(amount, total=total, detail=detail, phase='compressing',
                                     progress_basis=basis, progress_resolution=resolution)
                self._last_update = value
            return
        if not complete:
            return
        member = _MEMBER.fullmatch(text)
        if member:
            self._member = member.group(1)
            self.progress.update(detail='7-Zip: ' + self._member)
        elif text.startswith('Scanning the drive:'):
            self.progress.update(detail='7-Zip: scanning inputs')
        elif text.startswith('Creating archive:') or text.startswith('Updating archive:'):
            self.progress.update(detail='7-Zip: preparing archive')
        elif text.startswith('Add new data to archive:'):
            self.progress.update(detail='7-Zip: compressing members')
        elif text.startswith('Files read from disk:'):
            self.progress.update(detail='7-Zip: finalizing archive', phase='finalizing')

    def feed(self, text: str, *, final: bool = False) -> None:
        # Bound even a faulty/malicious archiver that never sends delimiters.
        frames = _FRAMES.split(self._pending + text)
        self._pending = frames.pop()[-16384:]
        for frame in frames:
            if frame.strip():
                self._record(frame, complete=True)
        if self._pending.strip():
            self._record(self._pending, complete=final)
        if final:
            self._pending = ''


class _Tail:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.parts: deque[str] = deque()
        self.size = 0

    def append(self, text: str) -> None:
        self.parts.append(text)
        self.size += len(text)
        while self.parts and self.size > self.limit:
            excess = self.size - self.limit
            first = self.parts[0]
            if len(first) <= excess:
                self.parts.popleft()
                self.size -= len(first)
            else:
                self.parts[0] = first[excess:]
                self.size -= excess

    def text(self) -> str:
        # Normalize paired Windows line endings before translating lone progress CRs.
        text = _ANSI.sub('', ''.join(self.parts)).replace('\r\n', '\n')
        return ''.join('\n' if ch in '\r\b' else ch for ch in text
                       if ch in '\n\r\t\b' or unicodedata.category(ch) not in ('Cc', 'Cf', 'Cs'))


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait(timeout=3)


def run_seven_zip(command: Sequence[str | os.PathLike[str]], *,
                  cwd: str | Path | None = None, progress: StageProgress | None = None,
                  env: Mapping[str, str] | None = None, output_limit: int = 65536,
                  heartbeat_interval: float = 0.2, timeout: float | None = None,
                  cancel_event: threading.Event | None = None,
                  input_chunks: Iterable[bytes] | None = None, input_size: int | None = None,
                  on_tick: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
    """Run 7-Zip without deadlocking pipes or retaining unbounded diagnostic logs.

    Include SEVEN_ZIP_PROGRESS_ARGS in the command and omit ``-bd``.  Both output
    streams are drained together so errors cannot fill an unread stderr pipe.
    ``stdout`` in the returned CompletedProcess is the bounded combined log;
    ``stderr`` is empty.  Nonzero exits are returned for the caller to handle.
    Ctrl+C, explicit cancellation, and timeouts terminate and reap the child.
    Optional input chunks are streamed through a separate writer, so an input
    range never needs a duplicate raw file. With ``input_size`` and a byte stage,
    native MiB counters become measured range progress. A silent variant falls
    back to separately labeled input-feed progress, never encoder-completion
    claims. The monitor callback runs alongside pipe draining and may stop input
    or raise to cancel an oversized candidate.
    A successful exit supplies the observed completion signal even when 7-Zip
    did not print a final 100% frame.  Archive verification remains a later stage.
    """
    if input_size is not None:
        input_size = _finite_count(input_size, 'input_size')
        if input_chunks is None:
            raise ValueError('input_size requires streamed input_chunks')
    parser = SevenZipProgressParser(progress)
    interval = max(0.02, _finite_count(heartbeat_interval, 'heartbeat_interval'))
    if timeout is not None:
        timeout = _finite_count(timeout, 'timeout')
    tail = _Tail(output_limit)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    chunks: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=16)
    stop = threading.Event()
    input_lock = threading.Lock()
    input_state = {'sent': 0, 'exhausted': False, 'error': None}
    args = [os.fspath(arg) for arg in command]
    process = subprocess.Popen(args, cwd=cwd, env=env,
                               stdin=subprocess.PIPE if input_chunks is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    assert process.stdout is not None

    def send(value: bytes | BaseException | None) -> bool:
        while not stop.is_set():
            try:
                chunks.put(value, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def read_pipe() -> None:
        try:
            while not stop.is_set():
                chunk = process.stdout.read(8192)
                if not chunk or not send(chunk):
                    break
        except OSError as error:
            send(error)
        finally:
            send(None)

    def write_pipe() -> None:
        assert process.stdin is not None and input_chunks is not None
        try:
            for chunk in input_chunks:
                if stop.is_set():
                    break
                pending = memoryview(chunk)
                while pending and not stop.is_set():
                    written = process.stdin.write(pending)
                    if not written:
                        raise BrokenPipeError('7-Zip closed its input stream')
                    pending = pending[written:]
                    with input_lock:
                        input_state['sent'] += written
            if not stop.is_set():
                with input_lock:
                    input_state['exhausted'] = True
        except BrokenPipeError:
            # Let the process exit and preserve its actionable diagnostic log.
            # The caller also checks the number of bytes successfully supplied.
            pass
        except BaseException as error:
            with input_lock:
                input_state['error'] = error
            send(error)
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def input_telemetry() -> None:
        if progress is None or input_chunks is None:
            return
        with input_lock:
            sent, exhausted = input_state['sent'], input_state['exhausted']
        observations = {'input_bytes_sent': sent, 'input_total_bytes': input_size,
                        'input_complete': exhausted}
        # Only the main thread emits telemetry; input writes are separately
        # counted under a small lock. Native encoder measurements take priority.
        if progress.unit == 'bytes' and input_size is not None:
            total = sent if exhausted else input_size
            if parser.measured_basis is None:
                progress.update(sent, total=total, progress_basis='stdin_bytes_sent',
                                phase='finalizing' if exhausted else 'feeding',
                                progress_resolution=1, observations=observations)
            else:
                progress.update(total=total, observations=observations)
        else:
            progress.update(observations=observations)

    reader: threading.Thread | None = None
    writer: threading.Thread | None = None
    started = time.monotonic()
    eof = False
    try:
        # Child ownership starts at Popen, including a failure to start either
        # helper thread. Unstarted threads must never be joined during cleanup.
        reader = threading.Thread(target=read_pipe, name='codex-7zip-output', daemon=True)
        writer = (threading.Thread(target=write_pipe, name='codex-7zip-input', daemon=True)
                  if input_chunks is not None else None)
        reader.start()
        if writer is not None:
            writer.start()
        while not eof or process.poll() is None:
            input_telemetry()
            if on_tick is not None:
                on_tick()
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt('Archive compression cancelled')
            if timeout is not None and time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(args, timeout, output=tail.text())
            if eof:
                try:
                    process.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    value = chunks.get(timeout=interval)
                except queue.Empty:
                    value = b''
                if value is None:
                    eof = True
                elif isinstance(value, BaseException):
                    raise value
                elif value:
                    text = decoder.decode(value)
                    tail.append(text)
                    input_telemetry()
                    parser.feed(text)
            if progress is not None:
                progress.heartbeat()
        text = decoder.decode(b'', final=True)
        tail.append(text)
        parser.feed(text, final=True)
        returncode = process.wait()
        if writer is not None:
            writer.join(timeout=3)
            with input_lock:
                writer_error = input_state['error']
            if writer_error is not None:
                raise writer_error
            with input_lock:
                input_exhausted = input_state['exhausted']
            if returncode == 0 and not input_exhausted:
                raise RuntimeError('7-Zip exited before consuming the complete input stream')
            input_telemetry()
        if progress is not None:
            if returncode == 0:
                detail = '7-Zip completed; checking candidate; full archive verification follows'
                if progress.unit == 'bytes' and input_chunks is not None:
                    with input_lock:
                        sent = input_state['sent']
                    progress.update(sent, total=sent, phase='checking', detail=detail)
                else:
                    progress.update(100, total=100, phase='checking', detail=detail)
            else:
                progress.update(detail=f'7-Zip exited with code {returncode}')
        return subprocess.CompletedProcess(args, returncode, stdout=tail.text(), stderr='')
    finally:
        stop.set()
        try:
            _stop_process(process)
        finally:
            if writer is not None and writer.ident is not None:
                writer.join(timeout=3)
            # The writer normally closes stdin; a startup failure has no writer
            # to do so. Close the owned pipe on that path as well.
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if reader is not None and reader.ident is not None:
                reader.join(timeout=1)
            process.stdout.close()
