"""Real encoder-byte telemetry, stage ETA states, and bounded recent rates."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

from codex_exporter.archive_pipeline import ArchiveSettings
from codex_exporter.archive_stream import compress_range
from codex_exporter.progress import ProgressReporter, SevenZipProgressParser, run_seven_zip


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_native_stdin_mib_frames_are_encoder_bytes_not_percent_or_output():
    stream, clock = io.StringIO(), Clock()
    reporter = ProgressReporter(style='json', stream=stream, clock=clock, interval=0)
    with reporter.stage('Compress member', unit='bytes', total=32 * 1048576) as meter:
        parser = SevenZipProgressParser(meter)
        parser.feed('+ native.jsonl\r  0M\r')
        clock.now = 2
        parser.feed('  8')
        parser.feed('M 1 + native.jsonl\b\b\b   \b\b\b')
        measured = events(stream)[-1]
        assert measured['completed'] == 8 * 1048576
        assert measured['percent'] == 25
        assert measured['progress_basis'] == '7zip_processed_bytes'
        assert measured['progress_resolution'] == 1048576
        assert measured['rate_per_second'] == 4 * 1048576
        assert measured['eta_seconds'] == 6
        assert measured['eta_status'] == 'estimated'
        parser.feed('50%\r')  # Do not invent bytes from a percent frame.
        assert meter.completed == 8 * 1048576
        parser.feed('99999M\r')  # Impossible input progress is not trusted.
        assert meter.completed == 8 * 1048576
        parser.feed('16M 2 + native.jsonl', final=True)
        assert meter.completed == 16 * 1048576


def test_native_byte_frames_do_not_pollute_ordinary_percent_stages():
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream, interval=0)
    with reporter.stage('Compress files', unit='percent') as meter:
        parser = SevenZipProgressParser(meter)
        parser.feed(' 128M\r 25%\r')
        assert meter.completed == 25
        assert meter.total == 100
        assert meter.progress_basis == '7zip_percent'
        assert all(row['rate_per_second'] is None for row in events(stream))


def test_recent_rate_adapts_to_a_slower_region_and_ignores_repeated_heartbeats():
    stream, clock = io.StringIO(), Clock()
    reporter = ProgressReporter(style='json', stream=stream, clock=clock, interval=0)
    with reporter.stage('Render', total=10000, unit='records') as meter:
        for second in range(1, 41):
            clock.now = second
            meter.update(second * 100)
        for second in range(41, 81):
            clock.now = second
            meter.update(4000 + (second - 40) * 10)
        measured = events(stream)[-1]
        assert measured['rate_per_second'] == pytest.approx(10)
        assert measured['eta_seconds'] == pytest.approx(560)
        assert len(meter._samples) <= 256
        clock.now = 95
        meter.heartbeat()
        stalled = events(stream)[-1]
        assert stalled['eta_status'] == 'waiting_for_progress'
        assert stalled['rate_per_second'] is None
        assert stalled['eta_seconds'] is None
        assert stalled['completed'] == 4400
        clock.now = 96
        meter.update(4410)
        assert events(stream)[-1]['eta_status'] == 'estimated'


def test_fast_updates_keep_a_real_recent_window_instead_of_erasing_its_anchors():
    stream, clock = io.StringIO(), Clock()
    reporter = ProgressReporter(style='json', stream=stream, clock=clock, interval=0.5)
    with reporter.stage('Scan', total=100000, unit='bytes') as meter:
        for tick in range(1, 10001):
            clock.now = tick / 100
            done = tick * 10 if tick <= 5000 else 50000 + tick - 5000
            meter.update(done)
        stats = meter._stats(clock.now)
        assert stats['rate_per_second'] == pytest.approx(100)
        assert stats['eta_seconds'] == pytest.approx(450)
        assert len(meter._samples) <= 153


def test_full_count_does_not_claim_stage_complete_before_encoder_flush():
    stream, clock = io.StringIO(), Clock()
    reporter = ProgressReporter(style='json', stream=stream, clock=clock, interval=0)
    with reporter.stage('Compress', total=100, unit='bytes') as meter:
        clock.now = 1
        meter.update(100)
        current = events(stream)[-1]
        assert current['status'] == 'running'
        assert current['completed'] == 100 and current['work_percent'] == 100
        assert current['percent'] is None
        assert current['eta_status'] == current['phase'] == 'finalizing'
        assert current['eta_seconds'] is None
        clock.now = 2
    final = events(stream)[-1]
    assert final['event'] == 'stage_complete' and final['percent'] == 100
    assert final['eta_status'] == 'complete'


def test_unknown_and_warming_eta_states_are_explicit_in_plain_output():
    stream, clock = io.StringIO(), Clock()
    reporter = ProgressReporter(style='plain', stream=stream, clock=clock, plain_interval=0)
    with reporter.stage('Metadata'):
        clock.now = 1
    with reporter.stage('Scan', total=100) as meter:
        clock.now = 2
        meter.update(10)
    text = stream.getvalue()
    assert 'ETA unavailable' in text
    assert 'ETA estimating' in text
    assert 'ETA ~9.0s' in text


def test_stdin_fallback_is_labeled_input_feed_not_encoder_completion():
    raw = b'x' * (128 * 1024)
    observed_feed = threading.Event()

    class ObservedProgress(io.StringIO):
        def write(self, text):
            written = super().write(text)
            for line in text.splitlines():
                row = json.loads(line)
                if (row['event'] == 'stage_progress' and
                        row['progress_basis'] == 'stdin_bytes_sent' and
                        0 < row['completed'] < len(raw)):
                    observed_feed.set()
            return written

    def input_chunks():
        yield raw[:8192]
        # Pipe capacity varies by OS. Keep an interior state observable before
        # releasing the rest, rather than relying on the child draining slowly.
        if not observed_feed.wait(timeout=5):
            raise AssertionError('No interior stdin fallback update observed before producer timeout')
        yield raw[8192:]

    stream = ObservedProgress()
    reporter = ProgressReporter(style='json', stream=stream, interval=0)
    program = ('import sys,time\n'
               'while sys.stdin.buffer.read(4096): time.sleep(0.01)\n'
               'time.sleep(0.25)\n')
    with reporter.stage('Silent codec', total=len(raw), unit='bytes') as meter:
        result = run_seven_zip([sys.executable, '-c', program], progress=meter,
                              input_chunks=input_chunks(),
                              input_size=len(raw), heartbeat_interval=0.02, timeout=10)
    assert result.returncode == 0
    measured = events(stream)
    feeding = [row for row in measured if row['event'] == 'stage_progress' and
               row['progress_basis'] == 'stdin_bytes_sent' and 0 < row['completed'] < len(raw)]
    assert feeding and all(row['eta_scope'] == 'input_feed' for row in feeding)
    finalizing = [row for row in measured if row['status'] == 'running' and
                  row['observations'].get('input_complete')]
    assert finalizing
    assert all(row['percent'] is None and row['eta_seconds'] is None for row in finalizing)
    assert measured[-1]['completed'] == len(raw)


def test_writer_error_is_not_lost_when_encoder_success_and_stdout_eof_race():
    def invalid_input():
        yield b'safe prefix'
        raise ValueError('Synthetic source failure')

    program = 'import sys;sys.stdin.buffer.read()'
    with pytest.raises(ValueError, match='Synthetic source failure'):
        run_seven_zip([sys.executable, '-c', program], input_chunks=invalid_input(), timeout=10)


def test_successful_early_encoder_exit_does_not_invent_input_completion():
    with pytest.raises(RuntimeError, match='complete input stream'):
        run_seven_zip([sys.executable, '-c', 'pass'], input_chunks=[b'x' * (4 * 1024 * 1024)],
                      input_size=4 * 1024 * 1024, timeout=10)


def test_progress_observations_stay_bounded_and_json_safe():
    reporter = ProgressReporter(enabled=False)
    with reporter.stage('Observe', total=10) as meter:
        meter.update(observations={'output_bytes': 5, 'input_complete': False})
        assert meter.observations == {'output_bytes': 5, 'input_complete': False}
        with pytest.raises(ValueError):
            meter.update(observations={'bad': float('nan')})
        with pytest.raises(ValueError, match='bounded'):
            meter.update(observations={str(i): i for i in range(17)})


def test_real_stdin_lzma_emits_native_byte_progress_eta_and_exact_archive(tmp_path):
    executable = os.environ.get('CODEX_EXPORT_TEST_7Z') or shutil.which('7zz') or shutil.which('7z')
    if not executable:
        pytest.skip('optional real-LZMA integration needs 7-Zip')
    # Large enough to cross several real encoder reporting intervals, with fixed
    # content and a bounded 8 MiB dictionary so the test remains modest in CI.
    raw = random.Random(20261123).randbytes(32 * 1024 * 1024)
    source, destination = tmp_path / 'source.bin', tmp_path / 'source.zip'
    source.write_bytes(raw)
    stream = io.StringIO()
    reporter = ProgressReporter(style='json', stream=stream, interval=0)
    result = compress_range(destination, 'source.bin', source, 0, len(raw), len(raw) * 2,
                            settings=ArchiveSettings(dictionary='8m', threads=2),
                            backend='7zip', archiver=Path(executable), reporter=reporter,
                            adaptive=False)
    assert result.size_bytes == len(raw)
    with zipfile.ZipFile(destination) as archive:
        assert archive.read('source.bin') == raw
        assert archive.getinfo('source.bin').compress_type == zipfile.ZIP_LZMA
    measured = events(stream)
    native = [row for row in measured if row['progress_basis'] == '7zip_processed_bytes' and
              row['event'] == 'stage_progress' and 0 < row['completed'] < len(raw)]
    assert native, measured
    assert all(row['completed'] % 1048576 == 0 for row in native)
    assert any(row['eta_seconds'] is not None and row['eta_status'] == 'estimated' for row in native)
    # Writer-return and encoder counters are independently sampled. A write can
    # still be returning when the encoder reports bytes from that pipe block.
    assert all(row['observations']['input_total_bytes'] == len(raw) for row in native)
    assert all(0 <= row['observations']['input_bytes_sent'] <= len(raw) for row in native)
    assert not any(row['percent'] == 100 for row in measured if row['status'] == 'running')
    assert measured[-1]['completed'] == measured[-1]['total'] == len(raw)
    assert measured[-1]['progress_basis'] == 'verified_member_bytes'
    assert measured[-1]['percent'] == 100
