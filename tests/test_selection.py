"""Synthetic boundary, ambiguity, raw-byte and source-provenance regressions."""
from __future__ import annotations

from dataclasses import asdict
from datetime import timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_exporter.selection import (
    SelectionError, excerpt_records, excerpt_turns, index_turns,
    parse_since_time, resolve_since, turn_rows,
)
from codex_exporter.snapshot import snapshot_source


def event(text, *, timestamp='', turn_id='', item_id=''):
    payload = {'type': 'user_message', 'message': text}
    if turn_id:
        payload['turn_id'] = turn_id
    if item_id:
        payload['item_id'] = item_id
    return {'type': 'event_msg', 'timestamp': timestamp, 'payload': payload}


def answer(text):
    return {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': text}}


def item(text, *, kind='message', role='user', item_id=''):
    body = {'type': kind, 'content': [{'type': 'input_text', 'text': text}]}
    if role:
        body['role'] = role
    if item_id:
        body['id'] = item_id
    if kind == 'UserMessage':
        return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': body}}
    return {'type': 'response_item', 'payload': body}


def write_rollout(path: Path, rows, *, newline=b'\n', eof_newline=True):
    lines = [json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + newline for row in rows]
    if lines and not eof_newline:
        lines[-1] = lines[-1][:-len(newline)]
    path.write_bytes(b''.join(lines))
    return lines


def meta():
    return {'type': 'session_meta', 'payload': {'id': 'synthetic-session', 'cwd': '/synthetic/project'}}


def context(turn_id):
    return {'type': 'turn_context', 'payload': {'turn_id': turn_id, 'cwd': '/synthetic/project'}}


def test_modern_and_legacy_duplicates_are_one_turn_but_repeated_steering_is_new(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [
        meta(),
        item('# AGENTS.md instructions for /synthetic\n<INSTRUCTIONS>Policy</INSTRUCTIONS>'),
        item('<environment_context>Project configuration</environment_context>'),
        item('System policy', role='system'),
        context('turn-first'),
        item('Inspect the new result', item_id='message-1'),
        event('Inspect the new result', item_id='message-1'),
        answer('First answer'),
        context('turn-second'),
        event('Continue', item_id='message-2'),
        item('Continue', kind='UserMessage', role='', item_id='message-2'),
        event('Continue', item_id='steering-1'),
        answer('Final answer'),
    ])
    index = index_turns(path)
    assert len(index.turns) == 3
    assert [turn.line for turn in index.turns] == [6, 10, 12]
    assert index.turns[0].duplicate_lines == (7,)
    assert index.turns[1].duplicate_lines == (11,)
    assert index.turns[0].turn_id == 'turn-first'
    assert index.turns[1].turn_id == 'turn-second'
    assert index.injected_context_lines == (2, 3)
    assert resolve_since(index, 'id:steering-1').number == 3


def test_content_dedupe_is_cross_schema_only_and_does_not_cross_assistant_activity(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('Repeat'), item('Repeat'), event('Repeat'), answer('Done'), item('Repeat')])
    index = index_turns(path)
    assert [turn.line for turn in index.turns] == [1, 3, 5]
    assert index.turns[0].duplicate_lines == (2,)


def test_exact_ids_dedupe_late_completed_user_surface(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [item('Prompt', item_id='unique-message'), answer('Reply'),
                        item('Prompt', kind='UserMessage', role='', item_id='unique-message')])
    index = index_turns(path)
    assert len(index.turns) == 1
    assert index.turns[0].duplicate_lines == (3,)


def test_selectors_require_explicit_resolution_of_ambiguous_text_and_ids(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('Inspect shared keyword α', turn_id='same-active-turn', item_id='msg-one'),
                        answer('One'), event('Inspect SHARED keyword β', turn_id='same-active-turn', item_id='msg-two'),
                        answer('Two'), event('123', turn_id='distinct-turn')])
    index = index_turns(path)
    with pytest.raises(SelectionError, match='matches 2 user turns') as caught:
        resolve_since(index, 'shared\n   KEYWORD')
    assert [turn.number for turn in caught.value.candidates] == [1, 2]
    assert 'turn:1' in str(caught.value) and 'turn:2' in str(caught.value)
    assert resolve_since(index, 'text:shared keyword', occurrence=2).number == 2
    with pytest.raises(SelectionError, match='matches 2 user turns'):
        resolve_since(index, 'id:same-active-turn')
    assert resolve_since(index, 'id:msg-two').number == 2
    assert resolve_since(index, 'id:distinct').number == 3
    assert resolve_since(index, 'turn:2').number == 2
    assert resolve_since(index, '123').number == 3
    with pytest.raises(SelectionError, match='No user turn'):
        resolve_since(index, 'missing words')
    with pytest.raises(SelectionError, match='exceeds 2'):
        resolve_since(index, 'shared keyword', occurrence=3)
    with pytest.raises(SelectionError, match='positive'):
        resolve_since(index, 'shared keyword', occurrence=0)


def test_full_prompt_is_searchable_without_retention_in_turn_index(tmp_path):
    path = tmp_path / 'thread.jsonl'
    text = 'A large prompt. ' * 50_000 + 'Distinctive late boundary phrase'
    write_rollout(path, [event(text), answer('Reply')])
    index = index_turns(path)
    assert len(index.turns[0].preview) <= 240
    assert len(json.dumps(asdict(index.turns[0]))) < 1500
    assert resolve_since(index, 'late boundary phrase').number == 1
    assert 'late boundary phrase' not in index.turns[0].preview


def test_time_resolution_is_inclusive_offset_aware_and_date_only_is_local(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('Earlier', timestamp='2030-01-01T17:00:00Z'), answer('One'),
                        event('Boundary', timestamp='2030-01-01T10:00:00-08:00'), answer('Two'),
                        event('Later', timestamp='2030-01-01T19:00:00+00:00')])
    index = index_turns(path)
    assert resolve_since(index, 'time:2030-01-01T18:00:00Z').number == 2
    assert resolve_since(index, 'time:2030-01-01T17:30:00Z').number == 2
    assert resolve_since(index, 'time:2030-01-01T11:00:00-08:00').number == 3
    with pytest.raises(SelectionError, match='UTC offset'):
        resolve_since(index, 'time:2030-01-01T18:00:00')
    with pytest.raises(SelectionError, match='ISO date'):
        resolve_since(index, 'time:2030-13-01')
    midnight = parse_since_time('2030-01-01')
    assert midnight.hour == midnight.minute == midnight.second == 0
    assert midnight.utcoffset() is not None
    assert parse_since_time('2030-01-01T18:00:00Z').astimezone(timezone.utc).hour == 18


def test_turn_excerpt_is_exact_raw_interval_with_minimal_context_and_provenance(tmp_path):
    path = tmp_path / 'thread.jsonl'
    lines = write_rollout(path, [
        meta(), context('old-turn'), event('Prior prompt'), answer('Prior answer'),
        context('new-turn'), event('Selected café prompt'),
        {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'call-1', 'arguments': '{"cmd":"pwd"}'}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-1', 'output': '/synthetic'}},
        answer('Selected answer'),
        event('New steering'), answer('Newest answer'),
    ], newline=b'\r\n', eof_newline=False)
    with snapshot_source(path, directory=tmp_path) as snapshot:
        original_sha = snapshot.sha256
        index = index_turns(snapshot)
        excerpt = excerpt_turns(snapshot, 2, index=index)
        expected = lines[0] + lines[4] + b''.join(lines[5:])
        assert excerpt.path.read_bytes() == expected
        assert excerpt.sha256 == hashlib.sha256(expected).hexdigest()
        assert excerpt.size_bytes == len(expected)
        assert excerpt.owns_staged_path()
        assert excerpt.original_path == path
        selected = excerpt.metadata['selection']
        assert selected['original_snapshot']['captured_sha256'] == original_sha
        assert selected['input_snapshot_sha256'] == original_sha
        assert selected['start_turn'] == 2 and selected['stop_turn'] == 3
        assert selected['selected_turn_count'] == 2
        assert selected['context_lines'] == [1, 5]
        assert selected['ranges'][-1]['source_line_start'] == 6
        assert selected['ranges'][-1]['source_line_end'] == 11
        assert selected['ranges'][-1]['excerpt_line_start'] == 3
        assert selected['ranges'][-1]['excerpt_byte_end'] == len(expected)
        assert snapshot.path.read_bytes() == b''.join(lines)
    assert not excerpt.path.exists()
    assert path.read_bytes() == b''.join(lines)


def test_bounded_turn_selection_excludes_following_user_and_can_omit_context(tmp_path):
    path = tmp_path / 'thread.jsonl'
    lines = write_rollout(path, [meta(), event('First'), answer('One'), event('Second'), answer('Two'), event('Third'), answer('Three')])
    with snapshot_source(path, directory=tmp_path) as snapshot:
        index = index_turns(snapshot)
        excerpt = excerpt_turns(snapshot, resolve_since(index, 'turn:2'), 2, index=index, include_context=False)
        assert excerpt.path.read_bytes() == b''.join(lines[3:5])
        assert excerpt.metadata['selection']['context_lines'] == []
        with pytest.raises(SelectionError, match='inclusive user-turn range'):
            excerpt_turns(snapshot, 3, 2, index=index)


def test_post_anchor_compaction_payload_is_retained_and_disclosed(tmp_path):
    path = tmp_path / 'thread.jsonl'
    lines = write_rollout(path, [meta(), event('Old prompt'), answer('Old reply'), event('New prompt'),
        {'type': 'compacted', 'message': 'Summary', 'replacement_history': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Old prompt'}]}]},
        {'type': 'event_msg', 'payload': {'type': 'thread_rolled_back', 'num_turns': 1}},
        answer('After compaction')])
    with snapshot_source(path, directory=tmp_path) as snapshot:
        excerpt = excerpt_turns(snapshot, 2)
        assert excerpt.path.read_bytes() == lines[0] + b''.join(lines[3:])
        selection = excerpt.metadata['selection']
        assert selection['embedded_history_record_count'] == 1
        assert [row['line'] for row in selection['history_events']] == [5, 6]
        assert 'may quote older content' in selection['history_policy']


def test_record_excerpt_uses_all_merged_source_lines_without_other_messages(tmp_path):
    path = tmp_path / 'thread.jsonl'
    lines = write_rollout(path, [meta(), context('t1'), event('Prompt'),
        {'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': '{"cmd":"pwd"}'}},
        answer('Unselected commentary'),
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': '/synthetic'}},
        context('t2'), event('Next prompt'), answer('Selected answer'), answer('Omitted answer')])
    selected = [SimpleNamespace(seq=4, source_seqs=(4, 6)), SimpleNamespace(seq=9, source_seqs=(9,))]
    with snapshot_source(path, directory=tmp_path) as snapshot:
        excerpt = excerpt_records(snapshot, selected)
        expected = b''.join(lines[line - 1] for line in (1, 2, 4, 6, 7, 9))
        assert excerpt.path.read_bytes() == expected
        info = excerpt.metadata['selection']
        assert info['selected_source_line_count'] == 3
        assert info['context_lines'] == [1, 2, 7]
        assert info['sha256'] == hashlib.sha256(expected).hexdigest()


def test_record_excerpt_rejects_reconstructed_and_missing_provenance(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [meta(), event('Prompt'), answer('Reply')])
    with snapshot_source(path, directory=tmp_path) as snapshot:
        with pytest.raises(SelectionError, match='live-context'):
            excerpt_records(snapshot, [SimpleNamespace(seq=-1)])
        with pytest.raises(SelectionError, match='no native source-line'):
            excerpt_records(snapshot, [SimpleNamespace(seq=0)])
        with pytest.raises(SelectionError, match='do not exist'):
            excerpt_records(snapshot, [SimpleNamespace(seq=99)])


def test_snapshot_appends_do_not_enter_excerpt_and_index_mutation_is_rejected(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [meta(), event('Prompt'), answer('Reply')])
    with snapshot_source(path, directory=tmp_path) as snapshot:
        index = index_turns(snapshot)
        with path.open('ab') as live:
            live.write((json.dumps(event('Appended live prompt')) + '\n').encode())
        excerpt = excerpt_turns(snapshot, 1, index=index)
        assert b'Appended live prompt' not in excerpt.path.read_bytes()
        with snapshot.path.open('ab') as staged:
            staged.write(b'\n')
        with pytest.raises(SelectionError, match='changed'):
            resolve_since(index, 'Prompt')
        with pytest.raises(SelectionError, match='different or changed'):
            excerpt_turns(snapshot, 1, index=index)


def test_image_only_prompts_and_late_context_are_indexed_without_blob_previews(tmp_path):
    path = tmp_path / 'thread.jsonl'
    rows = [
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,SYNTHETIC_A'}]}},
        context('image-turn'), answer('Image description'),
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,SYNTHETIC_B'}]}},
    ]
    write_rollout(path, rows)
    index = index_turns(path)
    assert len(index.turns) == 2
    assert index.turns[0].turn_id == 'image-turn'
    assert index.turns[0].text_sha256 != index.turns[1].text_sha256
    assert 'SYNTHETIC_' not in str(turn_rows(index))


def test_invalid_json_cannot_silently_change_turn_numbering(tmp_path):
    path = tmp_path / 'thread.jsonl'
    path.write_bytes(b'{invalid}\n' + json.dumps(event('Prompt')).encode() + b'\n')
    with pytest.raises(SelectionError, match='line 1 cannot be parsed'):
        index_turns(path)


def test_repairable_legacy_json_remains_byte_exact_in_excerpt(tmp_path):
    path = tmp_path / 'thread.jsonl'
    raw = b'{"type":"event_msg","payload":{"type":"user_message","message":"Open C:\\work\\item.txt"}}\n'
    path.write_bytes(raw)
    with snapshot_source(path, directory=tmp_path) as snapshot:
        index = index_turns(snapshot)
        assert index.repaired_json_lines == (1,)
        excerpt = excerpt_turns(snapshot, 1, index=index)
        assert excerpt.path.read_bytes() == raw
        assert excerpt.metadata['selection']['repaired_json_lines'] == [1]


def test_turn_listing_cannot_emit_terminal_control_sequences(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('hello\x1b[31m world\nnext\tline')])
    index = index_turns(path)
    assert '\x1b' not in turn_rows(index)[0]['preview']
    assert '\n' not in turn_rows(index)[0]['preview']


def test_context_before_and_after_prompts_never_overwrites_a_previous_turn_id(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [context('turn-one'), event('First'),
                        context('turn-two'), event('Second'),
                        event('Third'), context('turn-three'), answer('Final')])
    index = index_turns(path)
    assert [turn.turn_id for turn in index.turns] == ['turn-one', 'turn-two', 'turn-three']


def test_reused_item_id_with_different_explicit_turn_is_not_deduplicated(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('Repeat', turn_id='one', item_id='reused-id'), answer('First'),
                        event('Repeat', turn_id='two', item_id='reused-id')])
    index = index_turns(path)
    assert len(index.turns) == 2
    assert [turn.turn_id for turn in index.turns] == ['one', 'two']


def test_index_is_bounded_even_if_a_supplied_path_appends_during_progress(tmp_path):
    path = tmp_path / 'thread.jsonl'
    write_rollout(path, [event('Prompt'), answer('Reply')])
    before_size = path.stat().st_size

    class AppendOnStart:
        done = False
        updates = []

        def update(self, completed=None, **kwargs):
            self.updates.append(completed)
            if not self.done:
                self.done = True
                with path.open('ab') as source:
                    source.write((json.dumps(event('Later live prompt')) + '\n').encode())

    meter = AppendOnStart()
    with pytest.raises(SelectionError, match='changed while indexing'):
        index_turns(path, progress=meter)
    assert meter.updates[-1] == before_size


def test_native_metadata_turn_and_epoch_timestamp_are_available_for_selectors(tmp_path):
    path = tmp_path / 'thread.jsonl'
    record = item('Native attribution prompt', item_id='message-native')
    record['payload']['internal_chat_message_metadata_passthrough'] = {
        'turn_id': 'metadata-turn', 'create_time': 1893513600.125,
    }
    write_rollout(path, [record])
    index = index_turns(path)
    assert resolve_since(index, 'id:metadata-turn').number == 1
    assert index.turns[0].timestamp == '2030-01-01T16:00:00.125000+00:00'
    assert resolve_since(index, 'time:2030-01-01T16:00:00Z').number == 1


def test_client_ids_connect_native_and_legacy_user_events_across_activity(tmp_path):
    path = tmp_path / 'thread.jsonl'
    legacy = event('Client-linked prompt')
    legacy['payload']['client_id'] = 'client-message-1'
    modern = item('Client-linked prompt', kind='UserMessage', role='', item_id='native-item')
    modern['payload']['item']['client_id'] = 'client-message-1'
    write_rollout(path, [legacy, answer('An intervening reply'), modern])
    index = index_turns(path)
    assert len(index.turns) == 1
    assert index.turns[0].duplicate_lines == (3,)
    assert resolve_since(index, 'id:client-message-1').number == 1


def test_legacy_file_and_audio_only_prompts_remain_user_turns(tmp_path):
    from codex_exporter.selection import native_user_text
    path = tmp_path / 'thread.jsonl'
    file_prompt = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'file_ids': ['file-synthetic']}}
    audio_prompt = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'audio': ['data:audio/wav;base64,SYNTHETIC_AUDIO']}}
    local_prompt = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'local_audio': ['/synthetic/note.wav']}}
    write_rollout(path, [file_prompt, answer('One'), audio_prompt, answer('Two'), local_prompt])
    index = index_turns(path)
    assert len(index.turns) == 3
    assert index.turns[0].preview.startswith('[image sha256:')
    assert index.turns[1].preview.startswith('[audio sha256:')
    assert index.turns[2].preview.startswith('[audio sha256:')
    assert 'SYNTHETIC_AUDIO' not in str(turn_rows(index))
    assert native_user_text(audio_prompt).startswith('[audio sha256:')


def test_equivalent_media_envelopes_and_started_events_do_not_double_count(tmp_path):
    path = tmp_path / 'thread.jsonl'
    legacy = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'images': ['data:image/png;base64,SYNTHETIC']}}
    modern = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
        'type': 'UserMessage', 'content': [{'type': 'image', 'image_url': 'data:image/png;base64,SYNTHETIC', 'detail': 'original'}]}}}
    started = {'type': 'event_msg', 'payload': {'type': 'item_started', 'item': {'type': 'UserMessage', 'id': 'start-item'}}}
    write_rollout(path, [legacy, started, modern])
    index = index_turns(path)
    assert len(index.turns) == 1
    assert index.turns[0].duplicate_lines == (3,)


def test_native_user_event_is_authoritative_even_if_user_sends_context_markup(tmp_path):
    path = tmp_path / 'thread.jsonl'
    prompt = '# AGENTS.md instructions for /synthetic\n<INSTRUCTIONS>Actual user request</INSTRUCTIONS>'
    write_rollout(path, [event(prompt), answer('Reply')])
    index = index_turns(path)
    assert len(index.turns) == 1
    assert resolve_since(index, 'Actual user request').number == 1


def test_context_envelope_followed_by_actual_request_is_not_an_injected_only_record(tmp_path):
    from codex_exporter.selection import is_injected_context
    path = tmp_path / 'thread.jsonl'
    context_only = '# AGENTS.md instructions for /synthetic\n<INSTRUCTIONS>Policy</INSTRUCTIONS>\n<environment_context>cwd</environment_context>'
    assert is_injected_context(context_only)
    assert not is_injected_context(context_only + '\nPlease implement the actual request')
    assert not is_injected_context('<environment_context>cwd</environment_context>\nA real request')
    write_rollout(path, [item(context_only), item(context_only + '\nPlease implement the actual request')])
    index = index_turns(path)
    assert len(index.turns) == 1
    assert index.turns[0].line == 2
