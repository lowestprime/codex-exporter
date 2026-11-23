"""Measured buffered rendering and snapshot totals without extra source scans."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from codex_exporter import cli
from codex_exporter.progress import ProgressReporter
from codex_exporter.snapshot import snapshot_source


def _events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.mark.parametrize('ending,tail,count', [
    (b'\n', b'', 2),
    (b'\r\n', b'', 2),
    (b'\n', b'{"type":"event_msg","payload":{}}', 3),
    (b'\n', b'{"type":"unfinished', 2),
])
def test_snapshot_record_total_accounts_for_newlines_and_deferred_tail(tmp_path, ending, tail, count):
    source = tmp_path / 'source.jsonl'
    raw = ending.join((b'{"type":"session_meta","payload":{}}',
                       b'{"type":"event_msg","payload":{}}')) + ending + tail
    source.write_bytes(raw)
    with snapshot_source(source) as captured:
        assert captured.metadata['captured_line_count'] == count
        assert captured.metadata['captured_size_bytes'] == len(captured.path.read_bytes())
    assert source.read_bytes() == raw


def test_compressed_snapshot_totals_describe_decoded_records(tmp_path):
    zstd = pytest.importorskip('zstandard')
    raw = b'{"type":"session_meta"}\n{"type":"event_msg"}'
    source = tmp_path / 'source.jsonl.zst'
    source.write_bytes(zstd.ZstdCompressor().compress(raw))
    with snapshot_source(source) as captured:
        assert captured.metadata['captured_line_count'] == 2
        assert captured.path.read_bytes() == raw


@pytest.mark.parametrize('plain,ui_style', [(False, False), (True, False), (False, True)])
def test_record_render_progress_preserves_output_and_counts_unicode_characters(tmp_path, plain, ui_style):
    records = [cli.Record('message', 'user', 'Prompt 🧠', '', 'event_msg', 1),
               cli.Record('message', 'assistant', 'Answer with more text', '', 'event_msg', 2)]
    kwargs = dict(records=records, title='Measured render', source=tmp_path / 'source.jsonl',
                  mode='chat', plain=plain, ui_style=ui_style, include_meta=False,
                  source_sha='0' * 64, show_timestamps=False)
    expected = cli.render_export(**kwargs)
    output = io.StringIO()
    reporter = ProgressReporter(style='json', stream=output, interval=0)
    with reporter.stage('Render selected records', unit='characters') as progress:
        actual = cli.render_export(**kwargs, progress=progress)
    rows = _events(output)
    total = sum(len(record.text) for record in records)
    assert actual == expected
    assert rows[-1]['completed'] == rows[-1]['total'] == total
    assert any(0 < row['completed'] < total for row in rows)
    assert all(row['unit'] == 'characters' for row in rows)
    assert any(row.get('phase') == 'finalizing' for row in rows)


def test_thread_render_progress_retains_preamble_and_both_turns(tmp_path):
    records = [cli.Record('note', 'system', 'Preamble', '', 'metadata', 1),
               cli.Record('message', 'user', 'First prompt', '', 'event_msg', 2),
               cli.Record('message', 'assistant', 'First answer', '', 'event_msg', 3),
               cli.Record('message', 'user', 'Second prompt 🧠', '', 'event_msg', 4),
               cli.Record('message', 'assistant', 'Second answer', '', 'event_msg', 5)]
    kwargs = dict(records=records, title='Measured turns', source=tmp_path / 'source.jsonl',
                  mode='thread', meta={}, session_id=None, include_frontmatter=False,
                  include_map=False, source_sha='0' * 64, show_timestamps=False)
    expected = cli.render_thread_export(**kwargs)
    output = io.StringIO()
    reporter = ProgressReporter(style='json', stream=output, interval=0)
    with reporter.stage('Render selected records', unit='characters') as progress:
        actual = cli.render_thread_export(**kwargs, progress=progress)
    rows = _events(output)
    assert actual == expected
    assert rows[-1]['completed'] == rows[-1]['total'] == sum(len(record.text) for record in records)
    assert any(0 < row['completed'] < row['total'] for row in rows if row['total'])


def test_bounded_text_write_preserves_utf8_and_reports_work_before_flush(tmp_path):
    text = ('🧠 α\n' * 300000) + 'last line\n'
    target = tmp_path / 'measured.md'
    output = io.StringIO()
    reporter = ProgressReporter(style='json', stream=output, interval=0)
    with reporter.stage('Write Markdown', unit='characters') as progress:
        cli.write_rendered_text(target, text, progress=progress)
    rows = _events(output)
    assert target.read_bytes() == text.encode('utf-8')
    assert rows[-1]['completed'] == rows[-1]['total'] == len(text)
    assert any(0 < row['completed'] < len(text) for row in rows)
    assert any(row.get('phase') == 'finalizing' for row in rows)


def test_count_pass_progress_keeps_whole_document_token_boundaries():
    text = 'lines: __CODEX_EXPORT_TOTAL_LINES__\ntokens: __CODEX_EXPORT_TOKEN_COUNT__\nHello 🧠\n'
    expected = cli.finalize_count_placeholders(text)
    output = io.StringIO()
    reporter = ProgressReporter(style='json', stream=output, interval=0)
    with reporter.stage('Count rendered output', unit='characters') as progress:
        actual = cli.finalize_count_placeholders(text, progress=progress)
    rows = _events(output)
    assert actual == expected
    assert rows[-1]['completed'] > len(actual)
    assert all(row['total'] is None for row in rows)
    assert all(row['eta_seconds'] is None for row in rows if row['status'] == 'running')
    assert any('Count pass 2' in row.get('detail', '') for row in rows)
