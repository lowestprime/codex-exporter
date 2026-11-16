"""Live listings and exports agree about complete final native records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from codex_exporter import cli
from codex_exporter.selection import (SelectionError, excerpt_turns, index_live_turns,
                                      index_turns, resolve_since, turn_rows)
from codex_exporter.snapshot import snapshot_source


SID = '11111111-2222-4333-8444-555555555555'
PREFIX = (json.dumps({'type': 'session_meta', 'payload': {'id': SID}}) + '\n').encode()
WINDOWS_TAIL = (Path(__file__).parent / 'fixtures/regression/repairable-final-user.jsonl').read_bytes()
CONTROL_TAIL = (
    b'{"type":"event_msg","payload":{"type":"user_message",'
    b'"message":"Inspect\tthis","turn_id":"tail"}}'
)
VALID_TAIL = json.dumps({'type': 'event_msg', 'payload': {
    'type': 'user_message', 'message': 'Valid final prompt', 'turn_id': 'tail'}}).encode()


@pytest.mark.parametrize('tail,repaired', [
    pytest.param(VALID_TAIL, False, id='valid'),
    pytest.param(WINDOWS_TAIL, True, id='repairable-windows-escape'),
    pytest.param(CONTROL_TAIL, True, id='repairable-control-character'),
])
@pytest.mark.parametrize('prefix', [b'', PREFIX], ids=['first-record', 'after-complete-record'])
def test_complete_final_prompt_is_listed_and_selected_byte_exactly(tmp_path, tail, repaired, prefix):
    source = tmp_path / 'rollout.jsonl'
    original = prefix + tail
    source.write_bytes(original)
    live = index_live_turns(source)
    assert live.deferred_tail_bytes == 0
    assert live.size_bytes == len(original)
    assert live.sha256 == hashlib.sha256(original).hexdigest()
    expected_line = 2 if prefix else 1
    assert live.repaired_json_lines == ((expected_line,) if repaired else ())
    assert len(live.turns) == 1
    assert live.turns[0].turn_id == 'tail'
    assert live.turns[0].byte_start == len(prefix)
    assert live.turns[0].byte_end == len(original)

    with snapshot_source(source, directory=tmp_path) as snapshot:
        indexed = index_turns(snapshot)
        assert turn_rows(live) == turn_rows(indexed)
        assert indexed.sha256 == snapshot.sha256 == live.sha256
        assert snapshot.path.read_bytes() == original
        assert snapshot.metadata['deferred_tail_bytes'] == 0
        anchor = resolve_since(indexed, 'id:tail')
        excerpt = excerpt_turns(snapshot, anchor, index=indexed)
        assert excerpt.path.read_bytes() == original
        assert excerpt.metadata['selection']['repaired_json_lines'] == list(live.repaired_json_lines)
    assert source.read_bytes() == original
    assert not list(tmp_path.glob('.codex-snapshot-*'))


@pytest.mark.parametrize('tail', [
    pytest.param(b' \t\r ', id='blank'),
    pytest.param(b'\t', id='tab'),
])
def test_blank_final_bytes_are_preserved_by_listing_and_snapshot(tmp_path, tail):
    source = tmp_path / 'rollout.jsonl'
    original = PREFIX + VALID_TAIL + b'\n' + tail
    source.write_bytes(original)
    live = index_live_turns(source)
    with snapshot_source(source) as snapshot:
        indexed = index_turns(snapshot)
        assert live.sha256 == indexed.sha256 == snapshot.sha256 == hashlib.sha256(original).hexdigest()
        assert live.size_bytes == snapshot.size_bytes == len(original)
        assert live.deferred_tail_bytes == snapshot.metadata['deferred_tail_bytes'] == 0
        assert turn_rows(live) == turn_rows(indexed)
        assert live.total_lines == indexed.total_lines == 3
    assert source.read_bytes() == original


@pytest.mark.parametrize('tail', [
    pytest.param(WINDOWS_TAIL[:-1], id='repairable-escape-missing-brace'),
    pytest.param(b'{"type":"event_msg","payload":', id='unfinished-object'),
    pytest.param(b'{"message":"partial UTF-8 \xf0\x9f', id='partial-utf8'),
    pytest.param(b'{"message":"invalid UTF-8 \xff"}', id='invalid-utf8'),
    pytest.param(b'{"message":broken}', id='unrepairable-object'),
])
def test_incomplete_final_bytes_still_defer_with_matching_prefix_hash(tmp_path, tail):
    source = tmp_path / 'rollout.jsonl'
    complete = PREFIX + VALID_TAIL + b'\n'
    original = complete + tail
    source.write_bytes(original)
    live = index_live_turns(source)
    with snapshot_source(source) as snapshot:
        indexed = index_turns(snapshot)
        assert live.deferred_tail_bytes == snapshot.metadata['deferred_tail_bytes'] == len(tail)
        assert live.physical_size_bytes == len(original)
        assert live.size_bytes == snapshot.size_bytes == len(complete)
        assert snapshot.path.read_bytes() == complete
        assert snapshot.metadata['deferred_tail_sha256'] == hashlib.sha256(tail).hexdigest()
        assert live.sha256 == indexed.sha256 == snapshot.sha256 == hashlib.sha256(complete).hexdigest()
        assert turn_rows(live) == turn_rows(indexed)
        assert live.total_lines == indexed.total_lines == 2
    assert source.read_bytes() == original


@pytest.mark.parametrize('tail', [b'42', b'null', b'"text"', b'[]'])
def test_complete_non_object_json_is_not_silently_discarded_by_listing(tmp_path, tail):
    source = tmp_path / 'rollout.jsonl'
    original = PREFIX + tail
    source.write_bytes(original)
    with pytest.raises(SelectionError, match='line 2 is not an object'):
        index_live_turns(source)
    with snapshot_source(source) as snapshot:
        assert snapshot.path.read_bytes() == original
        assert snapshot.metadata['deferred_tail_bytes'] == 0
        with pytest.raises(SelectionError, match='line 2 is not an object'):
            index_turns(snapshot)
    assert source.read_bytes() == original


@pytest.mark.parametrize('renderer', ['--fast', '--no-fast'])
def test_cli_lists_then_exports_same_repairable_final_turn(tmp_path, monkeypatch, capsys, renderer):
    source = tmp_path / f'rollout-{SID}.jsonl'
    original = PREFIX + WINDOWS_TAIL
    source.write_bytes(original)
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'unused-home')
    common = ['codex-export', '--jsonl', str(source), '--no-progress', '--tokenizer', 'regex', '--json']
    monkeypatch.setattr(sys, 'argv', [*common, '--list-turns'])
    assert cli.main() == 0
    listing = json.loads(capsys.readouterr().out)
    assert [row['turn_id'] for row in listing['turns']] == ['tail']
    assert listing['source_prefix_sha256'] == hashlib.sha256(original).hexdigest()

    monkeypatch.setattr(sys, 'argv', [*common, '--since-id', listing['turns'][0]['turn_id'],
        renderer, '--no-archive', '--out-dir', str(tmp_path / 'export'), '--no-remember-out-dir'])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result['source_copy']).read_bytes() == original
    assert result['source_copy_sha256'] == listing['source_prefix_sha256']
    assert result['source_snapshot']['selection']['repaired_json_lines'] == [2]
    assert r'Inspect C:\Users\ExampleUser\Notes' in Path(result['file']).read_text(encoding='utf-8')
    assert source.read_bytes() == original
