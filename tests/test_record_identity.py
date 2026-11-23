"""Native occurrence identity controls numbering, tool association, and provenance."""
from __future__ import annotations

from argparse import Namespace
import json

import pytest

from codex_exporter import cli


def event(kind, **values):
    return {'type':'response_item','payload':{'type':kind,**values}}


def message(role,text,identity):
    return event('message',role=role,id=identity,content=[{'type':'input_text' if role=='user' else 'output_text','text':text}])


def call(identity,command='pwd'):
    return event('function_call',name='exec_command',call_id=identity,arguments=json.dumps({'cmd':command}))


def output(identity,text):
    return event('function_call_output',call_id=identity,output=text)


def parse(tmp_path, rows, include_actions=True):
    path=tmp_path/'rollout.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    records,_=cli.parse_session(path,include_actions=include_actions,tool_outputs='full',max_tool_chars=20000,
                               strip=False,max_line_chars=20000,max_repeated_lines=25)
    return records


@pytest.mark.parametrize('with_ids',[True,False])
def test_message_numbering_does_not_depend_on_whether_tools_are_visible(tmp_path,with_ids):
    rows=[message('user','Question','u1'),message('assistant','Done','a1'),call('c1'),output('c1','/first'),message('assistant','Done','a2')]
    if not with_ids:
        for row in rows:
            row['payload'].pop('id',None)
    visible=parse(tmp_path,rows,True)
    hidden=parse(tmp_path,rows,False)
    before=[(r.role,r.text,r.source_seqs) for r in visible if r.kind=='message']
    after=[(r.role,r.text,r.source_seqs) for r in hidden if r.kind=='message']
    assert before==after==[('user','Question',(1,)),('assistant','Done',(2,)),('assistant','Done',(5,))]
    assert cli.choose_records(hidden,Namespace(mode='last-assistant'))[0].source_seqs==(5,)
    assert cli.choose_records(hidden,Namespace(mode='message',message=3))[0].source_seqs==(5,)


@pytest.mark.parametrize('same_turn',[True,False])
def test_identical_commands_and_outputs_with_different_call_ids_remain_distinct(tmp_path,same_turn):
    rows=[message('user','First','u1'),call('c1'),output('c1','same output')]
    if not same_turn:
        rows.extend([message('assistant','First answer','a1'),message('user','Second','u2')])
    rows.extend([call('c2'),output('c2','same output')])
    actions=[record for record in parse(tmp_path,rows) if record.kind=='action']
    assert len(actions)==2
    assert [record.call_id for record in actions]==['c1','c2']
    assert all('pwd' in record.text and 'same output' in record.text for record in actions)
    assert set(actions[0].source_seqs).isdisjoint(actions[1].source_seqs)
    assert sorted(number for record in actions for number in record.source_seqs)==[2,3,len(rows)-1,len(rows)]


def test_parallel_outputs_match_call_ids_even_when_completion_order_differs(tmp_path):
    rows=[call('call-A','echo A'),call('call-B','echo B'),output('call-B','RESULT-B'),output('call-A','RESULT-A')]
    actions=[record for record in parse(tmp_path,rows) if record.kind=='action']
    assert len(actions)==2
    assert [record.source_seqs for record in actions]==[(1,4),(2,3)]
    assert 'echo A' in actions[0].text and 'RESULT-A' in actions[0].text and 'RESULT-B' not in actions[0].text
    assert 'echo B' in actions[1].text and 'RESULT-B' in actions[1].text and 'RESULT-A' not in actions[1].text


@pytest.mark.parametrize('rows',[
    [call('A','echo A'),output('B','unmatched')],
    [event('function_call',name='exec_command',arguments='{"cmd":"one"}'),event('function_call',name='exec_command',arguments='{"cmd":"two"}'),event('function_call_output',output='ambiguous')],
])
def test_mismatched_or_ambiguous_outputs_stay_separate_without_false_attribution(tmp_path,rows):
    actions=[record for record in parse(tmp_path,rows) if record.kind=='action']
    assert len(actions)==len(rows)
    assert all(len(record.source_seqs)==1 for record in actions)


def test_same_occurrence_mirrors_merge_all_native_source_lines(tmp_path):
    rows=[message('assistant','Mirrored answer','shared'),
          {'type':'event_msg','payload':{'type':'agent_message','item_id':'shared','message':'Mirrored answer'}},
          call('call-one'),call('call-one'),output('call-one','actual output'),output('call-one','actual output')]
    records=parse(tmp_path,rows)
    messages=[record for record in records if record.kind=='message']
    actions=[record for record in records if record.kind=='action']
    assert len(messages)==1 and messages[0].source_seqs==(1,2)
    assert len(actions)==1 and actions[0].source_seqs==(3,4,5,6)


def test_repeated_adjacent_message_with_distinct_ids_is_preserved(tmp_path):
    rows=[message('assistant','Done','a1'),{'type':'event_msg','payload':{'type':'agent_message','item_id':'a2','message':'Done'}}]
    assert [record.source_seqs for record in parse(tmp_path,rows)]==[(1,),(2,)]


@pytest.mark.parametrize('item', [
    {'type':'FileChange','changes':{'synthetic.txt':{'type':'add','content':'saved'}}},
    {'type':'Extension','extension_name':'synthetic','result':'returned'},
    {'type':'McpToolCall','id':'mcp-call','status':'completed','result':{'value':'returned'}},
    {'type':'ImageView','path':'/synthetic/image.png'},
])
def test_known_native_actions_do_not_become_unknown_when_message_view_hides_them(tmp_path,item):
    path=tmp_path/'rollout.jsonl'
    path.write_text(json.dumps({'type':'event_msg','payload':{'type':'item_completed','item':item}})+'\n')
    records,meta=cli.parse_session(path,include_actions=False,tool_outputs='full',max_tool_chars=20000,
                                  strip=False,max_line_chars=20000,max_repeated_lines=25)
    assert records==[]
    assert not meta['_unknown_event_types']
    assert meta['_ignored_event_reasons']['action_rendering_disabled']==1


def test_hidden_actions_policy_does_not_hide_an_unknown_schema():
    unknown={'type':'event_msg','payload':{'type':'item_completed','item':{'type':'NewUnknownAction','content':'recorded'}}}
    assert cli.known_ignored_reason(unknown,False,include_actions=False)=='unrecognized_schema'
