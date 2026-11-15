"""Timestamp preference/selection contracts through the actual CLI renderers."""
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


def _run(tmp_path: Path, *args: str):
    return subprocess.run([sys.executable, str(ENTRY), *map(str, args)],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
        env={**os.environ, 'CODEX_HOME': str(tmp_path / 'codex'),
             'CODEX_EXPORT_CONFIG': str(tmp_path / 'config.json')})


@pytest.fixture
def source(tmp_path):
    def event(kind, text, epoch, identity):
        return {'type': 'event_msg', 'timestamp': epoch,
                'payload': {'type': kind, 'message': text, 'turn_id': identity}}
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'test-session', 'title': 'Timestamp regression'}},
        event('user_message', 'First prompt', 0, 'turn-a'),
        event('agent_message', 'First answer', 1, 'turn-a'),
        {'type': 'event', 'timestamp': 2, 'payload': {'type': 'item', 'item': {
            'type': 'local_shell_call', 'arguments': '{"cmd":"echo preserved"}'}}},
        {'type': 'event', 'timestamp': 3, 'payload': {'type': 'item', 'item': {
            'type': 'local_shell_call_output', 'output': 'preserved output'}}},
        event('user_message', 'Second prompt', 60, 'turn-b'),
        event('agent_message', 'Second answer', 61, 'turn-b'),
        event('user_message', 'Third prompt', 120, 'turn-c'),
        event('agent_message', 'Third answer', 121, 'turn-c'),
    ]
    lines = [json.dumps(row, separators=(',', ':')).encode() + b'\r\n' for row in rows]
    path = tmp_path / 'rollout.jsonl'
    path.write_bytes(b''.join(lines))
    return path, lines


def _export(tmp_path, source, *args, out_name='output', source_copy=False):
    run = _run(tmp_path, '--jsonl', source, '--no-archive', '--no-progress',
               '--tokenizer', 'regex', '--no-remember-out-dir',
               '--out-dir', tmp_path / out_name,
               '--source-copy' if source_copy else '--no-source-copy', '--json', *args)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    return result, Path(result['file']).read_text(encoding='utf-8')


@pytest.mark.parametrize(('arguments', 'body', 'time_fragment'), [
    (['--mode', 'thread'], 'First prompt', '12:00:00 AM UTC'),
    (['--mode', 'response', '--response', '2'], 'Second answer', '12:01:01 AM UTC'),
    (['--mode', 'turn', '--response', '2', '--no-ui-style'], 'Second prompt', '12:01:00 AM UTC'),
    (['--mode', 'last-response'], 'Third answer', '12:02:01 AM UTC'),
    (['--mode', 'last-assistant'], 'Third answer', '12:02:01 AM UTC'),
    (['--mode', 'last-substantial', '--min-chars', '1'], 'Third answer', '12:02:01 AM UTC'),
    (['--mode', 'message', '--message', '1'], 'First prompt', '12:00:00 AM UTC'),
    (['--mode', 'range', '--from-message', '2', '--to-message', '3'], 'First answer', '12:00:01 AM UTC'),
    (['--mode', 'chat'], 'First prompt', '12:00:00 AM UTC'),
    (['--mode', 'chat-actions'], 'preserved output', '12:00:00 AM UTC'),
    (['--mode', 'actions'], 'preserved output', 'January 1, 1970 at '),
])
def test_recorded_timestamp_in_each_classic_export_mode(tmp_path, source, arguments, body, time_fragment):
    path, lines = source
    result, text = _export(tmp_path, path, '--no-fast', '--timestamp-timezone', 'UTC', *arguments)
    assert body in text
    assert '_January 1, 1970 at ' in text
    assert time_fragment in text
    assert path.read_bytes() == b''.join(lines)
    manifest = json.loads(Path(result['manifest']).read_text())
    policy = manifest.get('timestamp_policy') or manifest['output']['timestamp_display']
    assert policy['enabled'] is True and policy['timezone'] == 'UTC'


@pytest.mark.parametrize('style', [['--plain'], ['--ui-style'], ['--no-ui-style'], ['--fence-turns']])
def test_no_timestamps_in_each_classic_presentation(tmp_path, source, style):
    path, _ = source
    _, text = _export(tmp_path, path, '--mode', 'thread', '--no-fast', '--timestamp-timezone', 'UTC',
                      '--no-timestamps', *style)
    assert 'First prompt' in text and 'Third answer' in text
    assert '_January 1, 1970 at ' not in text


def test_persisted_timestamp_preference_and_invocation_override(tmp_path, source):
    path, _ = source
    saved = _run(tmp_path, '--set-default', 'timestamps=false',
                 '--set-default', 'timestamp_timezone=UTC')
    assert saved.returncode == 0, saved.stderr
    config_before = (tmp_path / 'config.json').read_bytes()
    disabled, text = _export(tmp_path, path, '--mode', 'thread', '--no-fast', out_name='disabled')
    assert '_January 1, 1970 at ' not in text
    enabled, text = _export(tmp_path, path, '--mode', 'thread', '--no-fast', '--timestamps', out_name='enabled')
    assert '_January 1, 1970 at 12:00:00 AM UTC_' in text
    assert (tmp_path / 'config.json').read_bytes() == config_before
    for result, expected in ((disabled, False), (enabled, True)):
        manifest = json.loads(Path(result['manifest']).read_text())
        policy = manifest.get('timestamp_policy') or manifest['output']['timestamp_display']
        assert policy['enabled'] is expected and policy['timezone'] == 'UTC'


def test_fast_and_classic_excerpts_keep_same_exact_native_bytes(tmp_path, source):
    path, lines = source
    original = b''.join(lines)
    expected_excerpt = lines[0] + b''.join(lines[5:])
    for fast, timestamps_on in ((False, True), (True, True), (True, False)):
        name = f'fast-{fast}-time-{timestamps_on}'
        result, text = _export(tmp_path, path, '--mode', 'thread', '--since-turn', '2',
                              '--fast' if fast else '--no-fast',
                              '--timestamps' if timestamps_on else '--no-timestamps',
                              '--timestamp-timezone', 'UTC', out_name=name, source_copy=True)
        assert 'First prompt' not in text and 'First answer' not in text
        assert 'Second prompt' in text and 'Third answer' in text
        assert ('_January 1, 1970 at 12:01:00 AM UTC_' in text) is timestamps_on
        assert Path(result['source_copy']).read_bytes() == expected_excerpt
        assert result['source_copy_sha256'] == hashlib.sha256(expected_excerpt).hexdigest()
        assert path.read_bytes() == original


def test_read_only_listing_keeps_epoch_zero_and_sorted_original_numbers(tmp_path, source):
    path, lines = source
    before = path.stat()
    out = tmp_path / 'no-output'
    run = _run(tmp_path, '--jsonl', path, '--list-turns', '--sort', 'timestamp:desc',
               '--no-progress', '--json', '--tokenizer', 'regex', '--no-remember-out-dir', '--out-dir', out)
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert [r['turn'] for r in report['turns']] == [3, 2, 1]
    assert [r['timestamp'] for r in report['turns']] == ['120', '60', '0']
    assert report['source_prefix_sha256'] == hashlib.sha256(b''.join(lines)).hexdigest()
    assert not out.exists()
    assert not (tmp_path / 'config.json').exists()
    assert path.read_bytes() == b''.join(lines)
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_chatgpt_cli_honors_timestamp_preferences(tmp_path):
    source = tmp_path / 'conversations.json'
    source.write_text(json.dumps([{'id': 'chat', 'title': 'Chat', 'mapping': {'m': {
        'parent': None, 'children': [], 'message': {'create_time': 0, 'author': {'role': 'user'},
        'content': {'parts': ['Original message']}}}}}]))
    for flag, shown in (('--timestamps', True), ('--no-timestamps', False)):
        run = _run(tmp_path, '--chatgpt-export', source, '--out-dir', tmp_path / flag[2:],
                   '--timestamp-timezone', 'UTC', '--no-progress', '--json', flag)
        assert run.returncode == 0, run.stderr
        result = json.loads(run.stdout)[0]
        text = Path(result['file']).read_text()
        assert 'Original message' in text
        assert ('_January 1, 1970 at 12:00:00 AM UTC_' in text) is shown
        assert json.loads(Path(result['manifest']).read_text())['timestamp_policy']['enabled'] is shown


@pytest.mark.parametrize('fast', [False, True])
def test_native_metadata_epoch_zero_visible_in_both_renderers(tmp_path, fast):
    path = tmp_path / 'native.jsonl'
    row = {'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Epoch metadata prompt'}],
        'internal_chat_message_metadata_passthrough': {'turn_id': 'native-turn', 'create_time': 0},
    }}
    original = (json.dumps(row) + '\n').encode()
    path.write_bytes(original)
    _, text = _export(tmp_path, path, '--mode', 'thread', '--fast' if fast else '--no-fast',
                      '--timestamp-timezone', 'UTC')
    assert '_January 1, 1970 at 12:00:00 AM UTC_' in text
    assert 'Epoch metadata prompt' in text
    assert path.read_bytes() == original
