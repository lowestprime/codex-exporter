"""Progress must stay truthful, script-safe, bounded, and cancellable."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

from codex_exporter.progress import (ProgressReporter, SEVEN_ZIP_PROGRESS_ARGS,
                                    SevenZipProgressParser, run_seven_zip)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Terminal(io.StringIO):
    def __init__(self, encoding='utf-8'):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding

    def isatty(self):
        return True


@pytest.fixture
def capable_terminal(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.delenv('NO_COLOR', raising=False)
    # Test rendering independent of whether the test runner owns a Windows console.
    monkeypatch.setattr('codex_exporter.progress._windows_vt', lambda stream: True)


def events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_measured_counts_rates_eta_and_no_invented_completion(capsys):
    stream, clock = io.StringIO(), Clock()
    progress = ProgressReporter(style='json', stream=stream, clock=clock)
    with progress.stage('Verify snapshot', total=1000, unit='bytes') as task:
        clock.advance(2)
        task.update(250)
        observed = events(stream)[-1]
        assert observed['percent'] == 25
        assert observed['rate_per_second'] == 125
        assert observed['eta_seconds'] == 6
    final = events(stream)[-1]
    assert final['event'] == 'stage_complete'
    assert final['completed'] == 250  # completion does not invent unread bytes
    assert final['percent'] == 25
    assert final['eta_seconds'] is None
    progress.finish()
    assert events(stream)[-1]['elapsed_seconds'] == 2
    assert capsys.readouterr().out == ''


def test_unknown_work_never_claims_a_percentage_or_eta():
    stream, clock = io.StringIO(), Clock()
    progress = ProgressReporter(style='json', stream=stream, clock=clock)
    with progress.stage('Prepare archive', unit='files') as task:
        clock.advance(10)
        task.advance(3)
        task.heartbeat()
    for event in events(stream):
        assert event['total'] is None
        assert event['percent'] is None
        assert event['eta_seconds'] is None
    assert events(stream)[-1]['completed'] == 3


def test_auto_redirection_is_sparse_plain_and_stdout_stays_empty(capsys):
    clock = Clock()
    progress = ProgressReporter(clock=clock)
    with progress.stage('Render', total=1000, unit='records') as task:
        for count in range(1001):
            clock.advance(0.001)
            task.update(count)
    progress.finish()
    captured = capsys.readouterr()
    assert captured.out == ''
    assert len(captured.err.splitlines()) == 3
    assert '[start]' in captured.err and '[done]' in captured.err
    assert '1,000/1,000 records' in captured.err
    assert '\x1b' not in captured.err and '\r' not in captured.err


def test_throttle_keeps_latest_counts_and_emits_final_immediately():
    stream, clock = io.StringIO(), Clock()
    progress = ProgressReporter(style='json', stream=stream, clock=clock)
    with progress.stage('Copy', total=10000) as task:
        for count in range(10000):
            clock.advance(0.0001)
            task.update(count + 1)
    result = events(stream)
    assert len(result) <= 7  # start + <=5 updates + completion
    assert result[-1]['completed'] == 10000
    assert result[-1]['event'] == 'stage_complete'


def test_rich_unicode_color_and_terminal_control_sanitizing(capable_terminal):
    stream, clock = Terminal(), Clock()
    progress = ProgressReporter(style='rich', stream=stream, clock=clock, width=300)
    with progress.stage('Compress\x1b[31m archive', total=100, unit='percent') as task:
        clock.advance(1)
        task.update(42, detail='safe\x1b]52;clipboard;secret\x07\nmember\u202e')
    result = stream.getvalue()
    assert '\x1b[36m' in result and '\x1b[32m' in result
    assert '━' in result and '░' in result and '✓' in result
    assert '42.0%' in result
    assert '\x1b[31m' not in result
    assert 'clipboard' not in result and '\u202e' not in result
    assert 'safe member' in result


def test_no_color_dumb_terminal_ascii_and_off(capable_terminal, monkeypatch):
    monkeypatch.setenv('NO_COLOR', '1')
    stream = Terminal()
    with ProgressReporter(stream=stream) as progress:
        with progress.stage('Copy', total=10) as task:
            task.complete(10)
    result = stream.getvalue()
    assert '\x1b[2K' in result  # control sequences are independent from colors
    assert '\x1b[36m' not in result and '\x1b[32m' not in result
    monkeypatch.setenv('TERM', 'dumb')
    stream = Terminal()
    with ProgressReporter(style='rich', stream=stream) as progress:
        with progress.stage('Copy', total=10) as task:
            task.complete(10)
    assert '\x1b' not in stream.getvalue() and '\r' not in stream.getvalue()
    monkeypatch.setenv('TERM', 'xterm')
    stream = Terminal('ascii')
    with ProgressReporter(stream=stream) as progress:
        with progress.stage('Copy', total=10) as task:
            task.complete(10)
    stream.getvalue().encode('ascii')
    assert '#' in stream.getvalue()
    stream = io.StringIO()
    with ProgressReporter(style='off', stream=stream) as progress:
        with progress.stage('Copy', total=10) as task:
            task.complete(10)
    assert stream.getvalue() == ''


def test_progress_failures_are_visible_and_do_not_hide_the_error():
    stream = io.StringIO()
    with pytest.raises(OSError, match='input changed'):
        with ProgressReporter(style='json', stream=stream) as progress:
            with progress.stage('Verify'):
                raise OSError('input changed')
    result = events(stream)
    assert result[-2]['event'] == 'stage_failed'
    assert result[-2]['detail'] == 'input changed'
    assert result[-1]['event'] == 'export_failed'
    assert result[-1]['stages_failed'] == 1


def test_closed_progress_stream_does_not_fail_export():
    stream = io.StringIO()
    stream.close()
    with ProgressReporter(style='json', stream=stream) as progress:
        with progress.stage('Copy', total=10) as task:
            task.complete(10)
    assert progress.style == 'off'


def test_parser_handles_backspace_cr_split_frames_and_members():
    stream = io.StringIO()
    progress = ProgressReporter(style='json', stream=stream, interval=0)
    with progress.stage('Compress', unit='percent') as task:
        parser = SevenZipProgressParser(task)
        for text in ['Scanning the drive:\n', '+ sour', 'ce.jsonl\n  1', '2%',
                     '\b\b\b\b    \b\b\b\b', ' 42% 1\r',
                     '+ render.md\n', ' 73% 2 + render.md', '\r1000%\n']:
            parser.feed(text)
        parser.feed('', final=True)
    result = events(stream)
    assert {12, 42, 73}.issubset({row['completed'] for row in result})
    assert result[-1]['completed'] == 73
    assert result[-1]['detail'] == '7-Zip: render.md'
    assert all(row['rate_per_second'] is None for row in result)
    assert any(row['detail'] == '7-Zip: source.jsonl' for row in result)
    assert result[0]['percent'] is None


def test_runner_drains_both_pipes_bounds_errors_and_decodes_unicode():
    stream = io.StringIO()
    progress = ProgressReporter(style='json', stream=stream, interval=0)
    code = (
        "import os,sys\n"
        "os.write(2,b'e'*2000000)\n"
        "os.write(1,b'\\n+ source-')\n"
        "v='\u03b1.jsonl\\n 25%\\b\\b\\b\\b    \\b\\b\\b\\b 85% 2\\r'.encode('utf-8')\n"
        "os.write(1,v[:1]);os.write(1,v[1:])\n"
        "os.write(2,b'\\nFINAL DIAGNOSTIC\\n')\n"
    )
    with progress.stage('Compress', unit='percent') as task:
        result = run_seven_zip([sys.executable, '-c', code], progress=task,
                              output_limit=4096, timeout=10)
    assert result.returncode == 0
    assert len(result.stdout) <= 4096
    assert result.stdout.endswith('FINAL DIAGNOSTIC\n')
    assert result.stderr == ''
    assert {25, 85, 100}.issubset({row['completed'] for row in events(stream)})
    assert any('source-\u03b1.jsonl' in row['detail'] for row in events(stream))


def test_runner_failure_returns_diagnostic_without_fabricated_100_percent():
    stream = io.StringIO()
    progress = ProgressReporter(style='json', stream=stream, interval=0)
    task = progress.stage('Compress', unit='percent')
    result = run_seven_zip([sys.executable, '-c',
                           "import sys;print(' 7%\\rERROR: cannot open input\\x1b[31m');sys.exit(7)"],
                          progress=task, output_limit=1024, timeout=10)
    assert result.returncode == 7
    assert 'cannot open input' in result.stdout
    assert '\x1b' not in result.stdout
    assert task.completed == 7
    assert not any(row['completed'] == 100 for row in events(stream))
    task.fail(detail='7-Zip failed')


def test_silent_runner_emits_elapsed_heartbeat_without_guessing_work():
    stream = io.StringIO()
    progress = ProgressReporter(style='json', stream=stream, interval=0.02)
    with progress.stage('Compress', unit='percent') as task:
        result = run_seven_zip([sys.executable, '-c', 'import time;time.sleep(0.3)'],
                              progress=task, heartbeat_interval=0.05, timeout=10)
    heartbeat = [row for row in events(stream) if row['event'] == 'stage_progress' and row['total'] is None]
    assert heartbeat and any(row['elapsed_seconds'] >= 0.05 for row in heartbeat)
    assert all(row['completed'] == 0 and row['percent'] is None for row in heartbeat)
    assert result.returncode == 0
    assert events(stream)[-1]['completed'] == 100


@pytest.mark.parametrize('cancel', [True, False])
def test_cancellation_and_timeout_reap_child(monkeypatch, cancel):
    created = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr('codex_exporter.progress.subprocess.Popen', popen)
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    if cancel:
        timer.start()
    try:
        with pytest.raises(KeyboardInterrupt if cancel else subprocess.TimeoutExpired):
            run_seven_zip([sys.executable, '-c', 'import time;time.sleep(30)'],
                          cancel_event=event if cancel else None,
                          timeout=10 if cancel else 0.15, heartbeat_interval=0.02)
    finally:
        timer.cancel()
    assert len(created) == 1
    assert created[0].poll() is not None
    assert created[0].stdout.closed


def test_progress_denominators_reject_invalid_values():
    progress = ProgressReporter(enabled=False)
    for value in [float('nan'), float('inf'), -1]:
        with pytest.raises(ValueError):
            progress.stage('Read', total=value)
    with progress.stage('Read', total=10) as task:
        with pytest.raises(ValueError):
            task.update(-1)
        task.update(12)
        assert task.completed == 12  # actual counts are never clamped to hide an overrun


def test_real_seven_zip_progress_and_lzma_archive(tmp_path):
    executable = os.environ.get('CODEX_EXPORT_TEST_7Z') or shutil.which('7zz') or shutil.which('7z')
    if not executable:
        pytest.skip('optional integration test requires 7-Zip')
    payload = ('measured export progress \u03b1\n' * 20000).encode()
    for name in ('thread.md', 'thread.manifest.json', 'thread.jsonl'):
        (tmp_path / name).write_bytes(payload)
    stream = io.StringIO()
    progress = ProgressReporter(style='json', stream=stream, interval=0)
    with progress.stage('Compress', unit='percent') as task:
        result = run_seven_zip([executable, 'a', '-tzip', '-mm=LZMA', '-mx=9', '-md=8m',
                               '-mmt=2', '-y', *SEVEN_ZIP_PROGRESS_ARGS, 'thread.zip',
                               'thread.md', 'thread.manifest.json', 'thread.jsonl'],
                              cwd=tmp_path, progress=task, timeout=30)
    assert result.returncode == 0, result.stdout
    assert events(stream)[-1]['completed'] == 100
    with zipfile.ZipFile(tmp_path / 'thread.zip') as archive:
        assert len(archive.infolist()) == 3
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_LZMA}
        assert all(archive.read(info) == payload for info in archive.infolist())


def test_active_stage_heartbeat_runs_during_blocking_work_and_stops():
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream, interval=0.01)
    with reporter.stage('Count tokens', unit='records') as task:
        threading.Event().wait(0.3)
    result = events(stream)
    heartbeat = [row for row in result if row['event'] == 'stage_progress']
    assert heartbeat
    assert all(row['completed'] == 0 and row['percent'] is None for row in heartbeat)
    assert task._heartbeat_thread is not None and not task._heartbeat_thread.is_alive()


def test_shared_reporter_tracks_snapshot_and_renderer_without_duplicate_stages(tmp_path):
    from codex_exporter.modern import stream_export
    from codex_exporter.snapshot import snapshot_source
    source = tmp_path / 'source.jsonl'
    raw = (json.dumps({'type': 'event_msg', 'payload': {
        'type': 'agent_message', 'message': 'Keep this captured content.'}}) + '\n').encode()
    source.write_bytes(raw)
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream)
    with snapshot_source(source, reporter=reporter) as snapshot:
        result = stream_export(snapshot.path, tmp_path / 'thread.md', session_id='session',
                               title='Measured export', progress=False, snapshot=snapshot,
                               reporter=reporter)
    reporter.finish()
    observed = events(stream)
    started = [row for row in observed if row['event'] == 'stage_start']
    completed = [row for row in observed if row['event'] == 'stage_complete']
    assert len(started) == len(completed) == 6
    assert [row['stage'] for row in started] == [row['stage'] for row in completed]
    by_stage = {row['stage']: row for row in completed}
    for name in ['Capture snapshot', 'Verify captured prefix', 'Read and render records']:
        assert by_stage[name]['completed'] == len(raw)
        assert by_stage[name]['total'] == len(raw)
    assert by_stage['Finalize snapshot boundary']['percent'] is None
    assert by_stage['Publish rendered files']['completed'] == 2
    assert '1 source lines; 1 rendered records' in by_stage['Read and render records']['detail']
    assert observed[-1]['stages_total'] == 6
    assert Path(result['file']).is_file()


def test_compressed_snapshot_progress_counts_physical_bytes_and_complete_frames(tmp_path):
    zstandard = pytest.importorskip('zstandard')
    from codex_exporter.snapshot import snapshot_source
    raw = b'{"type":"session_meta","payload":{}}\n'
    compressor = zstandard.ZstdCompressor()
    physical = compressor.compress(raw) + compressor.compress(raw)
    source = tmp_path / 'source.jsonl.zst'
    source.write_bytes(physical)
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream)
    with snapshot_source(source, reporter=reporter) as snapshot:
        assert snapshot.path.read_bytes() == raw + raw
    completed = [row for row in events(stream) if row['event'] == 'stage_complete']
    decode = next(row for row in completed if row['stage'] == 'Decode compressed snapshot')
    assert decode['completed'] == decode['total'] == len(physical)
    assert decode['detail'] == f'{len(raw) * 2:,} decoded bytes; 2 complete frames'


def test_rich_two_rows_preserve_member_suffix_and_redraw_without_wrapping(capable_terminal):
    stream, clock = Terminal(), Clock()
    reporter = ProgressReporter(style='rich', stream=stream, clock=clock, width=80)
    with reporter.stage('Verify archive', total=1000) as task:
        clock.advance(1)
        task.update(250, detail='thread_' + 'x' * 180 + '.manifest.json')
        clock.advance(1)
        task.update(500)
    raw = stream.getvalue()
    assert '\x1b[1A' in raw
    assert '.manifest.json' in raw
    assert '250/1,000 B' in raw and 'ETA ~3.0s' in raw
    import re
    for chunk in raw.split('\r'):
        for row in chunk.splitlines():
            clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', row)
            assert len(clean) <= 79
