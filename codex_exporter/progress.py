"""Measured export progress and a bounded, interruptible 7-Zip runner.

All display output goes to stderr.  Counts describe the current stage only:
7-Zip percentages are never converted into invented byte throughput.  A stage
without a known denominator shows elapsed time instead of a guessed percentage.
The default renderer uses a terminal bar, or sparse plain events when redirected.
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
from typing import Callable, Mapping, Sequence, TextIO
import unicodedata

PROGRESS_STYLES = ('auto', 'rich', 'plain', 'json', 'off')
SEVEN_ZIP_PROGRESS_ARGS = ('-bsp1', '-bso1', '-bse1', '-bb1', '-sccUTF-8')
_ANSI = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])')
_PERCENT = re.compile(r'^\s*(\d{1,3}(?:\.\d+)?)%(?:\s+(.*))?$')
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
            '⠋✓✕━░…'.encode(getattr(self.stream, 'encoding', None) or 'utf-8')
        except (UnicodeEncodeError, LookupError):
            self.unicode = False

    def __enter__(self) -> ProgressReporter:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.finish(success=exc_type is None)

    def stage(self, name: str, *, total: float | None = None, unit: str = 'bytes',
              detail: str = '') -> StageProgress:
        with self._lock:
            task = StageProgress(self, name, total=total, unit=unit, detail=detail,
                                 index=len(self._stages) + 1)
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
        if self.style == 'rich':
            icon = ('✕' if failed else '✓' if complete else
                    _SPINNERS[int(stats['elapsed_seconds'] * 5) % len(_SPINNERS)])
            if not self.unicode:
                icon = 'X' if failed else 'OK' if complete else '|/-\\'[int(stats['elapsed_seconds'] * 5) % 4]
        else:
            icon = '[failed]' if failed else '[done]' if complete else '[start]' if not task._emitted else '[working]'
        header = f'{icon} {task.index:02d} {task.name}'
        metrics = []
        percent = stats['percent']
        if percent is not None:
            metrics.append(f'{percent:.1f}%')
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
        metrics.append('elapsed ' + _duration(stats['elapsed_seconds']))
        if stats['eta_seconds'] is not None:
            metrics.append('ETA ~' + _duration(stats['eta_seconds']))
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
        color = '31' if failed else '32' if complete else '36'
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

    def finish(self, *, detail: str = '', success: bool = True) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self.style == 'off':
                return
            now = self._clock()
            elapsed = max(0.0, now - self.started)
            completed = sum(task.status == 'complete' for task in self._stages)
            failed = sum(task.status == 'failed' for task in self._stages)
            success = success and not failed and completed == len(self._stages)
            status = 'complete' if success else 'failed'
            if self.style == 'json':
                self._write(json.dumps({'event': 'export_complete' if success else 'export_failed',
                                        'status': status, 'elapsed_seconds': elapsed,
                                        'stages_completed': completed, 'stages_failed': failed,
                                        'stages_total': len(self._stages), 'detail': _clean(detail)},
                                       ensure_ascii=True, allow_nan=False) + '\n')
                return
            if self._live_rows:
                self._write('\n')
                self._live_rows = 0
            icon = ('✓' if success else '✕') if self.unicode else ('[done]' if success else '[failed]')
            line = f'{icon} Export {status} | {completed}/{len(self._stages)} stages | total {_duration(elapsed)}'
            if detail:
                line += ' | ' + _clean(detail)
            self._write(self._paint(line, '32' if success else '31') + '\n')


class StageProgress:
    """Absolute or incremental counters for one stage; context exit records status."""

    def __init__(self, reporter: ProgressReporter, name: str, *, total: float | None,
                 unit: str, detail: str, index: int):
        self.reporter = reporter
        self.name = _clean(name, 120)
        self.index = index
        self.unit = _clean(unit, 32)
        self.total = _finite_count(total, 'total') if total is not None else None
        self.completed = 0.0
        self.detail = _clean(detail)
        self.status = 'running'
        self.started = reporter._clock()
        self.ended: float | None = None
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
        else:
            self.fail(detail=str(exc_value) if exc_value else exc_type.__name__)

    def _stats(self, now: float) -> dict:
        elapsed = max(0.0, (self.ended if self.ended is not None else now) - self.started)
        percent = 100 * self.completed / self.total if self.total else None
        rate = self.completed / elapsed if elapsed >= 0.1 and self.completed > 0 else None
        eta = ((self.total - self.completed) / rate
               if self.status == 'running' and self.total and rate and self.completed < self.total else None)
        return {'elapsed_seconds': elapsed, 'percent': percent,
                'rate_per_second': rate if self.unit != 'percent' else None, 'eta_seconds': eta}

    def update(self, completed: float | None = None, *, total: float | None = None,
               detail: str | None = None) -> None:
        with self.reporter._lock:
            if self.status != 'running':
                return
            if completed is not None:
                self.completed = _finite_count(completed, 'completed')
            if total is not None:
                self.total = _finite_count(total, 'total')
            if detail is not None:
                self.detail = _clean(detail)
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
    processed; it is not treated as proof that that file has finished.  Nor is
    7-Zip's percentage interpreted as bytes read or output bytes produced.
    """

    def __init__(self, progress: StageProgress | None):
        if progress is not None and progress.unit != 'percent':
            raise ValueError("7-Zip progress stages must use unit='percent'")
        self.progress = progress
        self._pending = ''
        self._member = ''
        self._last_update: tuple[float, str] | None = None

    def _record(self, text: str, *, complete: bool) -> None:
        if self.progress is None:
            return
        text = _clean(text, 16384)
        match = _PERCENT.fullmatch(text)
        if match:
            percent = float(match.group(1))
            if percent > 100:
                return
            detail = '7-Zip: ' + (self._member or 'compressing members')
            # Some 7-Zip variants include the current member after the counter.
            suffix = match.group(2) or ''
            inline_member = re.match(r'(?:\d+\s+)?[+UT-]\s+(.+)', suffix)
            if inline_member:
                detail = '7-Zip: ' + inline_member.group(1)
            value = (percent, detail)
            if value != self._last_update:
                self.progress.update(percent, total=100, detail=detail)
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
            self.progress.update(detail='7-Zip: finalizing archive')

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
        text = _ANSI.sub('', ''.join(self.parts))
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
                  cancel_event: threading.Event | None = None) -> subprocess.CompletedProcess[str]:
    """Run 7-Zip without deadlocking pipes or retaining unbounded diagnostic logs.

    Include SEVEN_ZIP_PROGRESS_ARGS in the command and omit ``-bd``.  Both output
    streams are drained together so errors cannot fill an unread stderr pipe.
    ``stdout`` in the returned CompletedProcess is the bounded combined log;
    ``stderr`` is empty.  Nonzero exits are returned for the caller to handle.
    Ctrl+C, explicit cancellation, and timeouts terminate and reap the child.
    A successful exit supplies the observed completion signal even when 7-Zip
    did not print a final 100% frame.  Archive verification remains a later stage.
    """
    parser = SevenZipProgressParser(progress)
    interval = max(0.02, _finite_count(heartbeat_interval, 'heartbeat_interval'))
    if timeout is not None:
        timeout = _finite_count(timeout, 'timeout')
    tail = _Tail(output_limit)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    chunks: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=16)
    stop = threading.Event()
    args = [os.fspath(arg) for arg in command]
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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

    reader = threading.Thread(target=read_pipe, name='codex-7zip-output', daemon=True)
    started = time.monotonic()
    reader.start()
    eof = False
    try:
        while not eof or process.poll() is None:
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
                    parser.feed(text)
            if progress is not None:
                progress.heartbeat()
        text = decoder.decode(b'', final=True)
        tail.append(text)
        parser.feed(text, final=True)
        returncode = process.wait()
        if progress is not None:
            if returncode == 0:
                progress.update(100, total=100, detail='7-Zip completed; independent archive verification follows')
            else:
                progress.update(detail=f'7-Zip exited with code {returncode}')
        return subprocess.CompletedProcess(args, returncode, stdout=tail.text(), stderr='')
    finally:
        stop.set()
        try:
            _stop_process(process)
        finally:
            reader.join(timeout=1)
            process.stdout.close()
