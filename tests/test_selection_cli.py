"""CLI guarantees for inclusive selection and matching source excerpts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'Export-CodexThread.py'
SID = '11111111-2222-4333-8444-555555555555'


@pytest.fixture
def source(tmp_path):
    rows = [{'type': 'session_meta', 'payload': {'id': SID, 'cwd': '/synthetic/work'}}]
    for n, text in enumerate(('Historical-only prompt', 'Precise current anchor 🧠', 'Latest steering'), 1):
        timestamp = f'2026-11-13T{n:02d}:00:00Z'
        rows += [
            {'type': 'turn_context', 'payload': {'turn_id': f'turn-{n}', 'model': 'synthetic'}},
            {'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': 'user_message', 'message': text}},
            {'type': 'response_item', 'timestamp': timestamp, 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': f'call-{n}', 'arguments': json.dumps({'cmd': f'echo tool-{n}'})}},
            {'type': 'response_item', 'timestamp': timestamp, 'payload': {'type': 'function_call_output', 'call_id': f'call-{n}', 'output': f'exact output-{n}'}},
            {'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': 'agent_message', 'message': f'Answer-{n}'}}
        ]
    path = tmp_path / f'rollout-{SID}.jsonl'
    path.write_bytes(b''.join((json.dumps(row, ensure_ascii=False) + '\r\n').encode() for row in rows))
    return path


def run(source, tmp_path, *args, input=None):
    return subprocess.run([sys.executable, str(ENTRY), '--jsonl', str(source),
        '--out-dir', str(tmp_path / 'out'), '--no-remember-out-dir', '--no-archive', '--no-progress',
        '--tokenizer', 'regex', '--json', *args], text=True, capture_output=True, input=input,
        env={**os.environ, 'CODEX_HOME': str(tmp_path / 'home'), 'CODEX_EXPORT_CONFIG': str(tmp_path / 'config.json')}, timeout=60)


@pytest.mark.parametrize('selector', [('--since', 'Precise current anchor'), ('--since-turn', '2'),
    ('--since-id', 'turn-2'), ('--since-time', '2026-11-13T02:00:00Z')])
@pytest.mark.parametrize('renderer', ['--fast', '--no-fast'])
def test_since_includes_anchor_and_all_later_tools_without_history(source, tmp_path, selector, renderer):
    before = source.read_bytes()
    result = run(source, tmp_path, *selector, renderer)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    native = Path(value['source_copy']).read_bytes()
    md = Path(value['file']).read_text()
    assert b'Historical-only prompt' not in native and 'Historical-only prompt' not in md
    assert b'Answer-1' not in native and 'Answer-1' not in md
    for text in ('Precise current anchor', 'Latest steering', 'tool-2', 'exact output-2', 'Answer-2', 'Answer-3'):
        assert text.encode() in native and text in md
    assert native.startswith(before.splitlines(keepends=True)[0])
    assert b'\r\n' in native
    assert value['source_copy_sha256'] == hashlib.sha256(native).hexdigest()
    manifest = json.loads(Path(value['manifest']).read_text())
    selection = manifest['source_snapshot']['selection']
    assert selection['start_turn'] == 2 and selection['stop_turn'] == 3
    assert selection['input_snapshot_sha256'] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before


@pytest.mark.parametrize('args,include,exclude', [
    (('--last', '1', '--mode', 'thread'), ('Latest steering', 'tool-3', 'Answer-3'), ('Historical-only prompt', 'Precise current anchor', 'Answer-2')),
    (('--turn', '2'), ('Precise current anchor', 'tool-2', 'exact output-2', 'Answer-2'), ('Historical-only prompt', 'Latest steering', 'Answer-3')),
    (('--mode', 'response', '--response', '2'), ('tool-2', 'exact output-2', 'Answer-2'), ('Historical-only prompt', 'Precise current anchor', 'Latest steering')),
    (('--mode', 'last-assistant'), ('Answer-3',), ('Latest steering', 'tool-3', 'Answer-2')),
    (('--mode', 'message', '--message', '3'), ('Precise current anchor',), ('Historical-only prompt', 'Answer-2', 'Latest steering')),
    (('--mode', 'range', '--from-message', '3', '--to-message', '4'), ('Precise current anchor', 'Answer-2'), ('tool-2', 'exact output-2', 'Answer-1')),
])
def test_all_subset_modes_copy_matching_native_lines(source, tmp_path, args, include, exclude):
    result = run(source, tmp_path, *args)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    native = Path(value['source_copy']).read_bytes()
    md = Path(value['file']).read_text()
    for text in include:
        assert text.encode() in native and text in md
    for text in exclude:
        assert text.encode() not in native and text not in md
    assert value['source_sha256'] == hashlib.sha256(native).hexdigest()


def test_list_turns_is_discoverable_without_compression(source, tmp_path):
    result = run(source, tmp_path, '--list-turns')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert [row['turn'] for row in value['turns']] == [1, 2, 3]
    assert value['turns'][1]['turn_id'] == 'turn-2'
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('args', [('--since', ''), ('--since', 'no such prompt'), ('--since-time', '2026-11-13T02:00:00'),
    ('--since', 'turn:2', '--last', '1'), ('--archive-max-bytes', '512000001')])
def test_invalid_selection_has_no_published_files(source, tmp_path, args):
    result = run(source, tmp_path, *args)
    assert result.returncode != 0
    assert not list((tmp_path / 'out').glob('*'))


def test_browser_cancel_never_starts_a_progress_stage(tmp_path):
    result = subprocess.run([sys.executable, str(ENTRY), '--browse', '--progress-style', 'json'],
        input='q\n', capture_output=True, text=True,
        env={**os.environ, 'CODEX_HOME': str(tmp_path / 'empty'), 'CODEX_EXPORT_CONFIG': str(tmp_path / 'config')}, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip()
    assert 'Initialize tokenizer' not in result.stderr and 'Resolve sources' not in result.stderr


def test_single_turn_keeps_tools_after_injected_context_and_duplicate_surface(source, tmp_path):
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'real anchor'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '<environment_context>synthetic settings</environment_context>'}]}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'arguments': '{"cmd":"echo preserved-tool"}'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'preserved final'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'future unrelated'}},
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = run(source, tmp_path, '--turn', '1')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    md = Path(value['file']).read_text()
    native = Path(value['source_copy']).read_text()
    assert 'preserved-tool' in md and 'preserved final' in md
    assert 'future unrelated' not in md and 'future unrelated' not in native
    assert 'environment_context' in md and 'environment_context' in native


def test_ambiguous_text_needs_explicit_occurrence(source, tmp_path):
    data = source.read_text().replace('Latest steering', 'Precise current anchor 🧠')
    source.write_text(data)
    ambiguous = run(source, tmp_path, '--since-text', 'Precise current anchor')
    assert ambiguous.returncode != 0 and 'matches 2 user turns' in ambiguous.stderr
    selected = run(source, tmp_path, '--since-text', 'Precise current anchor', '--since-occurrence', '2')
    assert selected.returncode == 0, selected.stderr
    value = json.loads(selected.stdout)
    native = Path(value['source_copy']).read_text()
    assert 'Answer-3' in native and 'Answer-2' not in native


def test_media_only_user_turn_is_present_in_markdown_and_exact_native(source, tmp_path):
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'audio': {'url': 'synthetic-audio-reference'}}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Recorded response to media'}},
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = run(source, tmp_path, '--turn', '1')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert '[audio sha256:' in Path(value['file']).read_text()
    assert Path(value['source_copy']).read_bytes() == source.read_bytes()
