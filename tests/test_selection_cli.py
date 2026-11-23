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
    md = Path(value['file']).read_text(encoding='utf-8')
    assert b'Historical-only prompt' not in native and 'Historical-only prompt' not in md
    assert b'Answer-1' not in native and 'Answer-1' not in md
    for text in ('Precise current anchor', 'Latest steering', 'tool-2', 'exact output-2', 'Answer-2', 'Answer-3'):
        assert text.encode() in native and text in md
    assert native.startswith(before.splitlines(keepends=True)[0])
    assert b'\r\n' in native
    assert value['source_copy_sha256'] == hashlib.sha256(native).hexdigest()
    manifest = json.loads(Path(value['manifest']).read_text(encoding='utf-8'))
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
    md = Path(value['file']).read_text(encoding='utf-8')
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
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    result = run(source, tmp_path, '--turn', '1')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    md = Path(value['file']).read_text(encoding='utf-8')
    native = Path(value['source_copy']).read_text(encoding='utf-8')
    assert 'preserved-tool' in md and 'preserved final' in md
    assert 'future unrelated' not in md and 'future unrelated' not in native
    assert 'environment_context' in md and 'environment_context' in native


def test_ambiguous_text_needs_explicit_occurrence(source, tmp_path):
    data = source.read_text(encoding='utf-8').replace('Latest steering', 'Precise current anchor 🧠')
    source.write_text(data, encoding='utf-8')
    ambiguous = run(source, tmp_path, '--since-text', 'Precise current anchor')
    assert ambiguous.returncode != 0 and 'matches 2 user turns' in ambiguous.stderr
    selected = run(source, tmp_path, '--since-text', 'Precise current anchor', '--since-occurrence', '2')
    assert selected.returncode == 0, selected.stderr
    value = json.loads(selected.stdout)
    native = Path(value['source_copy']).read_text(encoding='utf-8')
    assert 'Answer-3' in native and 'Answer-2' not in native


def test_media_only_user_turn_is_present_in_markdown_and_exact_native(source, tmp_path):
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '', 'audio': {'url': 'synthetic-audio-reference'}}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Recorded response to media'}},
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    result = run(source, tmp_path, '--turn', '1')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert '[audio sha256:' in Path(value['file']).read_text(encoding='utf-8')
    assert Path(value['source_copy']).read_bytes() == source.read_bytes()


@pytest.mark.parametrize('args,include,exclude,gap_included', [
    (('--mode','thread','--no-fast'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','answer-3','command-1','command-2','command-3'), (), True),
    (('--mode','thread','--fast'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','answer-3','command-1','command-2','command-3'), (), True),
    (('--mode','response','--response','2'), ('answer-2','command-2','output-2'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-3','command-1','command-3'), False),
    (('--mode','turn','--response','2'), ('Prompt-2','answer-2','command-2','output-2'), ('Prompt-1','Prompt-3','answer-1','answer-3','command-1','command-3'), True),
    (('--mode','last-response'), ('answer-3','command-3','output-3'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','command-1','command-2'), False),
    (('--mode','last-assistant'), ('answer-3',), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','command-1','command-2','command-3'), False),
    (('--mode','last-substantial','--min-chars','64'), ('answer-2',), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-3','command-1','command-2','command-3'), False),
    (('--mode','message','--message','3'), ('Prompt-2',), ('Prompt-1','Prompt-3','answer-1','answer-2','answer-3','command-1','command-2','command-3'), False),
    (('--mode','range','--from-message','3','--to-message','4'), ('Prompt-2','answer-2'), ('Prompt-1','Prompt-3','answer-1','answer-3','command-1','command-2','command-3'), False),
    (('--mode','chat'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','answer-3'), ('command-1','command-2','command-3','output-1','output-2','output-3'), False),
    (('--mode','chat-actions'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','answer-3','command-1','command-2','command-3'), (), True),
    (('--mode','actions'), ('command-1','command-2','command-3','output-1','output-2','output-3'), ('Prompt-1','Prompt-2','Prompt-3','answer-1','answer-2','answer-3'), False),
    (('--since-turn','2','--no-fast'), ('Prompt-2','Prompt-3','answer-2','answer-3','command-2','command-3'), ('Prompt-1','answer-1','command-1'), True),
    (('--since-turn','2','--fast'), ('Prompt-2','Prompt-3','answer-2','answer-3','command-2','command-3'), ('Prompt-1','answer-1','command-1'), True),
    (('--last','1','--mode','thread','--no-fast'), ('Prompt-3','answer-3','command-3'), ('Prompt-1','Prompt-2','answer-1','answer-2','command-1','command-2'), False),
    (('--last','1','--mode','thread','--fast'), ('Prompt-3','answer-3','command-3'), ('Prompt-1','Prompt-2','answer-1','answer-2','command-1','command-2'), False),
])
def test_every_mode_keeps_matching_markdown_native_bytes_and_source_gap_audit(tmp_path, args, include, exclude, gap_included):
    source = tmp_path / f'rollout-{SID}.jsonl'
    gap = b'\x00' * 29 + b'\r\n'
    rows = [(json.dumps({'type':'session_meta','payload':{'id':SID}})+'\r\n').encode()]
    for n in (1,2,3):
        records = [
            {'type':'turn_context','payload':{'turn_id':f'turn-{n}'}},
            {'type':'event_msg','payload':{'type':'user_message','message':f'Prompt-{n}'}},
            {'type':'response_item','payload':{'type':'function_call','call_id':f'call-{n}','name':'exec_command','arguments':json.dumps({'cmd':f'echo command-{n}'})}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':f'call-{n}','output':f'output-{n}'}},
            {'type':'event_msg','payload':{'type':'agent_message','message':f'answer-{n}' + (' a detailed synthetic response' * 4 if n==2 else '')}},
        ]
        for i, record in enumerate(records):
            rows.append((json.dumps(record)+'\r\n').encode())
            if n == 2 and i == 1:
                rows.append(gap)
    before = b''.join(rows)
    source.write_bytes(before)
    result = run(source, tmp_path, *args, '--name','Synthetic mode matrix')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    native = Path(value['source_copy']).read_bytes()
    markdown = Path(value['file']).read_text(encoding='utf-8')
    for text in include:
        assert text in markdown and text.encode() in native
    for text in exclude:
        assert text not in markdown and text.encode() not in native
    assert (gap in native) is gap_included
    assert value['source_copy_sha256'] == hashlib.sha256(native).hexdigest()
    assert value['source_sha256'] == hashlib.sha256(native).hexdigest()
    selection = value['source_snapshot'].get('selection')
    if selection is not None:
        assert len(selection['source_gaps']) == 1
        audit = selection['source_gaps'][0]
        assert audit['source_snapshot_sha256'] == hashlib.sha256(before).hexdigest()
        assert audit['sha256'] == hashlib.sha256(gap).hexdigest()
        assert audit['included_in_excerpt'] is gap_included
        assert 'NUL-only source gap' in result.stderr
    else:
        assert native == before
        assert value.get('parse_error_count',1) == 1
    assert source.read_bytes() == before


def test_response_listing_uses_human_anchor_ids_after_late_completed_mirror(tmp_path):
    source = tmp_path / f'rollout-{SID}.jsonl'
    rows = [
        {'type':'event_msg','payload':{'type':'user_message','item_id':'user-one','message':'First human prompt'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'First answer'}},
        {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'UserMessage','id':'user-one','content':[{'type':'input_text','text':'First human prompt'}]}}},
        {'type':'event_msg','payload':{'type':'user_message','item_id':'user-two','message':'Second human prompt'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Second answer'}},
    ]
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    listed = run(source,tmp_path,'--list-responses')
    assert listed.returncode == 0, listed.stderr
    value = json.loads(listed.stdout)
    assert [row['index'] for row in value['responses']] == [1,2]
    assert [row['preview'] for row in value['responses']] == ['First answer','Second answer']
    assert value['turn_index_policy'] == 'human_user_turns_v2'
    exported = run(source,tmp_path,'--mode','response','--response','2')
    assert exported.returncode == 0, exported.stderr
    native = Path(json.loads(exported.stdout)['source_copy']).read_text()
    assert 'Second answer' in native and 'First answer' not in native


@pytest.mark.parametrize('renderer', ['--fast','--no-fast'])
def test_configuration_update_is_known_metadata_and_native_bytes_remain(renderer, tmp_path):
    source = tmp_path / f'rollout-{SID}.jsonl'
    rows = [
        {'type':'event_msg','payload':{'type':'user_message','message':'Configuration request'}},
        {'type':'response_item','payload':{'type':'configuration_update','reasoning':{'effort':'xhigh'},'metadata':{'harness_authored_configuration':True}}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Configuration response'}},
    ]
    before = ''.join(json.dumps(row)+'\n' for row in rows).encode()
    source.write_bytes(before)
    exported = run(source,tmp_path,'--mode','thread',renderer,'--strict-events')
    assert exported.returncode == 0, exported.stderr
    result = json.loads(exported.stdout)
    assert Path(result['source_copy']).read_bytes() == before
    manifest = json.loads(Path(result['manifest']).read_text())
    assert 'configuration_metadata' in json.dumps(manifest)


@pytest.mark.parametrize('low,high', [('0','1'),('1','0'),('0','0')])
def test_explicit_zero_range_endpoint_is_an_error_not_a_default(source,tmp_path,low,high):
    result = run(source,tmp_path,'--mode','range','--from-message',low,'--to-message',high)
    assert result.returncode != 0
    assert 'Invalid message range' in result.stderr
    assert not list((tmp_path/'out').glob('*.md'))


@pytest.mark.parametrize('mode,extra', [('turn',('--response','2')),('response',('--response','2')),('last-response',())])
def test_last_scope_with_turn_or_response_mode_uses_human_anchors_despite_late_mirrors(tmp_path,mode,extra):
    source=tmp_path/f'rollout-{SID}.jsonl'
    rows=[]
    for number in (1,2,3):
        text=f'Human anchor {number}'
        identity=f'user-{number}'
        rows.extend([
            {'type':'event_msg','payload':{'type':'user_message','item_id':identity,'message':text}},
            {'type':'event_msg','payload':{'type':'agent_message','message':f'Answer-{number}'}},
            {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'UserMessage','id':identity,'content':[{'type':'input_text','text':text}]}}},
        ])
    raw=[(json.dumps(row)+'\n').encode() for row in rows]
    source.write_bytes(b''.join(raw))
    exported=run(source,tmp_path,'--last','2','--mode',mode,*extra,'--name','Scoped result')
    assert exported.returncode==0,exported.stderr
    result=json.loads(exported.stdout)
    native=Path(result['source_copy']).read_bytes()
    markdown=Path(result['file']).read_text(encoding='utf-8')
    assert b'Answer-3' in native and 'Answer-3' in markdown
    assert b'Answer-2' not in native and 'Answer-2' not in markdown
    if mode=='turn':
        assert native==b''.join(raw[6:])
    else:
        assert b'Human anchor' not in native


def test_pending_turn_without_response_reports_clear_error_and_no_empty_export(tmp_path):
    source=tmp_path/f'rollout-{SID}.jsonl'
    source.write_text(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'Pending request'}})+'\n')
    result=run(source,tmp_path,'--mode','last-response')
    assert result.returncode==2
    assert 'No records matching --mode last-response' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not list((tmp_path/'out').glob('*.md'))


@pytest.mark.parametrize('mode_args', [
    ('--mode','last-assistant'), ('--mode','last-substantial','--min-chars','1'),
    ('--mode','message','--message','2'), ('--mode','range','--from-message','2','--to-message','2'),
    ('--mode','chat'),
])
def test_strict_message_selection_does_not_fail_only_because_known_actions_are_hidden(tmp_path,mode_args):
    source=tmp_path/f'rollout-{SID}.jsonl'
    rows=[
        {'type':'event_msg','payload':{'type':'user_message','message':'Synthetic request'}},
        {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'FileChange','changes':{'synthetic.txt':{'type':'add','content':'saved'}}}}},
        {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'CommandExecution','command':['echo','synthetic'],'aggregated_output':'recorded output'}}},
        {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'FunctionCallOutput','name':'synthetic','output':'recorded output'}}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Synthetic response'}},
    ]
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result=run(source,tmp_path,*mode_args,'--strict-events')
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    native=Path(value['source_copy']).read_text()
    assert 'Synthetic response' in native
    assert all(kind not in native for kind in ('FileChange','CommandExecution','FunctionCallOutput'))


@pytest.mark.parametrize('kind',['FutureToolCall','UnknownCommandExecution'])
def test_strict_message_view_still_rejects_unknown_future_tool_like_schema(tmp_path,kind):
    source=tmp_path/f'rollout-{SID}.jsonl'
    rows=[
        {'type':'event_msg','payload':{'type':'item_completed','item':{'type':kind,'command':'echo synthetic','output':'unknown body'}}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Recorded response'}},
    ]
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result=run(source,tmp_path,'--mode','last-assistant','--strict-events')
    assert result.returncode != 0
    assert 'Unknown event schemas' in result.stderr
    assert not list((tmp_path/'out').glob('*.md'))
