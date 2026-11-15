"""Source-preserving regressions for the disk-failure and CLI reports."""
from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import sys

import pytest

from codex_exporter import cli, selection
from codex_exporter.snapshot import snapshot_source


def write_source(path: Path, rows: list[dict]) -> bytes:
    data = b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in rows)
    path.write_bytes(data)
    return data


def prompt(text: str, **kwargs) -> dict:
    return {'type': 'event_msg', 'timestamp': kwargs.pop('timestamp', '2026-11-15T01:50:00Z'),
            'payload': {'type': 'user_message', 'message': text, **kwargs}}


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / 'rollout-11111111-2222-4333-8444-555555555555.jsonl'
    write_source(path, [
        {'type': 'session_meta', 'payload': {'id': '11111111-2222-4333-8444-555555555555'}},
        prompt('Original real prompt', turn_id='real-1'),
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Original reply'}},
        prompt('Latest real prompt', turn_id='real-2'),
    ])
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'no-home'))
    return path


def invoke(monkeypatch, args):
    monkeypatch.setattr(sys, 'argv', ['codex-export', *args])
    return cli.main()


def test_bare_command_matches_help_exactly_and_has_no_progress(source, monkeypatch, capsys):
    assert invoke(monkeypatch, []) == 0
    bare = capsys.readouterr()
    with pytest.raises(SystemExit) as done:
        invoke(monkeypatch, ['--help'])
    assert done.value.code == 0
    help_result = capsys.readouterr()
    assert bare.out == help_result.out
    assert bare.out.splitlines()[0] == f'Codex Exporter {cli.__version__}'
    assert bare.err == help_result.err == ''
    for flag in ('--work-dir', '--sort', '--pager', '--timestamp-timezone', '--no-timestamps'):
        assert flag in bare.out


def test_list_turns_stages_no_snapshot_and_sorts_without_renumbering(source, tmp_path, monkeypatch, capsys):
    import codex_exporter.snapshot as snapshots
    def forbidden(*args, **kwargs):
        raise AssertionError('A plain JSONL turn listing must not allocate a disk snapshot')
    monkeypatch.setattr(snapshots, 'snapshot_source', forbidden)
    assert invoke(monkeypatch, ['--jsonl', str(source), '--list-turns', '--sort', 'turn:desc',
                               '--no-progress', '--tokenizer', 'regex', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r['turn'] for r in result['turns']] == [2, 1]
    assert [r['turn_id'] for r in result['turns']] == ['real-2', 'real-1']
    assert result['source_prefix_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not list(tmp_path.glob('.codex-*'))


def test_live_index_allows_appends_and_defers_incomplete_tail(source, monkeypatch):
    initial = source.read_bytes()
    source.write_bytes(initial + b'{"type":"event_msg","payload":')
    physical = source.read_bytes()
    original = selection._bounded_lines
    def append_during_read(stream, size):
        for n, raw in original(stream, size):
            if n == 1:
                with source.open('ab') as writer:
                    writer.write(b'{"type":"user_message","message":"late"}}\n')
            yield n, raw
    monkeypatch.setattr(selection, '_bounded_lines', append_during_read)
    index = selection.index_live_turns(source)
    assert len(index.turns) == 2
    assert index.size_bytes == len(initial)
    assert index.deferred_tail_bytes == len(physical) - len(initial)
    assert index.physical_size_bytes == len(physical)
    assert index.sha256 == hashlib.sha256(initial).hexdigest()
    assert source.read_bytes().startswith(physical)


def test_live_index_rejects_same_size_prefix_edits(source, monkeypatch):
    original = selection._bounded_lines
    def edit_after_read(stream, size):
        yield from original(stream, size)
        source.write_bytes(source.read_bytes().replace(b'Original real prompt', b'Modified real prompt'))
    monkeypatch.setattr(selection, '_bounded_lines', edit_after_read)
    with pytest.raises(selection.SelectionError, match='prefix changed'):
        selection.index_live_turns(source)


@pytest.mark.parametrize('text', [
    '<codex_internal_context source="goal"><objective>Keep processing</objective></codex_internal_context>',
    '<codex_internal_context source="user_goal">Goal remains active</codex_internal_context>',
    '<in-app-browser-context source="ambient-ui-state">Automatic UI state</in-app-browser-context>',
    '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>',
    '<external_codex_apps_selected_page_part_1_of_1>Page snapshot</external_codex_apps_selected_page_part_1_of_1>',
    '<turn_aborted>Prior response interrupted</turn_aborted>',
])
def test_complete_internal_envelopes_are_context_not_user_anchors(text, tmp_path):
    source = tmp_path / 'source.jsonl'
    original = write_source(source, [prompt('Actual request', turn_id='human'), prompt(text),
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Continue after context'}},
        prompt('Next request', turn_id='next')])
    with snapshot_source(source) as snapshot:
        index = selection.index_turns(snapshot)
        assert [r.turn_id for r in index.turns] == ['human', 'next']
        excerpt = selection.excerpt_turns(snapshot, 1, 1, index=index)
        assert excerpt.path.read_bytes() == b''.join(original.splitlines(keepends=True)[:3])
        assert b'Continue after context' in excerpt.path.read_bytes()
    record = cli.Record('message', 'user', text, source_type='user_message')
    assert record.kind == 'note' and record.text == text
    assert source.read_bytes() == original


@pytest.mark.parametrize('text', [
    '<codex_internal_context source="goal">unfinished',
    '<codex_internal_context source="goal">Context</codex_internal_context>\nPlease handle this actual request.',
    '<send_user_message_question_reply>Yes, use that approach.</send_user_message_question_reply>',
    '<heartbeat>{"instructions":"Scheduled task"}</heartbeat>',
    '/goal Read my task instructions.',
])
def test_mixed_unclosed_and_actual_steering_remain_user_turns(text, tmp_path):
    source = tmp_path / 'source.jsonl'
    write_source(source, [prompt(text)])
    assert len(selection.index_turns(source).turns) == 1
    assert cli.Record('message', 'user', text, source_type='user_message').role == 'user'


def test_packaging_disk_failure_cleans_private_render_and_preserves_old_files(source, tmp_path, monkeypatch, capsys):
    from codex_exporter import archive_pipeline
    output, work = tmp_path / 'output', tmp_path / 'work'
    output.mkdir()
    sentinel = output / 'existing.md'
    sentinel.write_text('Previously published output', encoding='utf-8')
    before = source.read_bytes()
    monkeypatch.setattr(archive_pipeline, 'find_archiver', lambda *_: Path('synthetic-7z'))
    seen = {}
    def no_space(result, original, settings, **kwargs):
        seen['render'] = Path(result['file'])
        seen['snapshot'] = Path(original)
        assert work in seen['render'].parents
        assert work in seen['snapshot'].parents
        assert kwargs['destination'].parent == output
        raise OSError(errno.ENOSPC, 'Synthetic disk exhaustion during packaging')
    monkeypatch.setattr(archive_pipeline, 'package_export', no_space)
    assert invoke(monkeypatch, ['--jsonl', str(source), '--mode', 'thread', '--no-fast',
        '--out-dir', str(output), '--work-dir', str(work), '--no-remember-out-dir',
        '--no-progress', '--tokenizer', 'regex']) == 1
    captured = capsys.readouterr()
    assert 'Traceback' not in captured.err and '--work-dir' in captured.err
    assert not seen['render'].exists() and not seen['snapshot'].exists()
    assert list(output.iterdir()) == [sentinel]
    assert list(work.iterdir()) == []
    assert source.read_bytes() == before


def test_message_json_is_one_document_and_sorted_native_numbers_remain(source, tmp_path, monkeypatch, capsys):
    assert invoke(monkeypatch, ['--jsonl', str(source), '--list', '--sort', 'index:desc',
                               '--no-progress', '--tokenizer', 'regex', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r['index'] for r in result['messages']] == [3, 2, 1]
    assert not list(tmp_path.glob('.codex-*'))


def test_repeated_cross_schema_prompts_in_distinct_native_turns_do_not_merge(tmp_path):
    path = tmp_path / 'distinct-turns.jsonl'
    write_source(path, [
        {'type': 'turn_context', 'payload': {'turn_id': 'turn-a'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'Continue'}},
        {'type': 'turn_context', 'payload': {'turn_id': 'turn-b'}},
        prompt('Continue'),
    ])
    index = selection.index_turns(path)
    assert [row.turn_id for row in index.turns] == ['turn-a', 'turn-b']
    assert [row.line for row in index.turns] == [2, 4]
