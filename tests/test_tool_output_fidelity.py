"""Recorded aggregate output, independent streams, and completion metadata survive."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_exporter import cli

ROOT=Path(__file__).resolve().parents[1]
OPTIONS=dict(seq=1, include_actions=True, tool_outputs='full', max_tool_chars=40,
             strip=False, max_line_chars=20000, max_repeated_lines=25)


def command_event(**values):
    return {'type':'event_msg','payload':{'type':'item_completed','item':{
        'type':'CommandExecution','id':'synthetic-call',**values}}}


def classify(obj,**options):
    return cli.classify_record(obj,**{**OPTIONS,**options})


def test_native_aggregate_output_is_rendered_when_no_other_output_surface_exists():
    record=classify(command_event(command=['echo','synthetic'],aggregated_output='UNIQUE AGGREGATE OUTPUT',exit_code=7,status='failed'))
    assert record is not None
    assert 'UNIQUE AGGREGATE OUTPUT' in record.text
    assert 'Exit code 7' in record.text
    assert 'Status: failed' in record.text


def test_aggregate_output_only_record_without_command_is_not_lost():
    record=classify(command_event(aggregated_output='Aggregate-only recorded result',exit_code=0,status='completed'))
    assert record is not None and 'Aggregate-only recorded result' in record.text
    assert 'exit code 0' in record.text.casefold()
    assert 'Status: completed' in record.text


def test_both_independent_stdout_and_stderr_survive_with_labels():
    record=classify({'type':'response_item','payload':{'type':'function_call_output','call_id':'streams',
        'stdout':'STDOUT SENTINEL','stderr':'STDERR SENTINEL','exit_code':3}})
    assert record is not None
    assert 'stdout:\nSTDOUT SENTINEL' in record.text
    assert 'stderr:\nSTDERR SENTINEL' in record.text
    assert 'Exit code 3' in record.text


def test_identical_text_on_two_distinct_streams_is_not_deduplicated():
    record=classify(command_event(command=['echo'],stdout='SAME STREAM TEXT',stderr='SAME STREAM TEXT'))
    assert record.text.count('SAME STREAM TEXT')==2


@pytest.mark.parametrize('mode',['none','summary','tail','full'])
def test_aggregate_output_respects_existing_tool_output_policy(mode):
    body='AGGREGATE-FIRST\n'+'middle recorded line\n'*8+'AGGREGATE-LAST'
    record=classify(command_event(command=['synthetic'],aggregated_output=body,exit_code=4,status='failed'),tool_outputs=mode)
    assert record is not None and 'Exit code 4' in record.text and 'Status: failed' in record.text
    if mode=='none':
        assert 'AGGREGATE-FIRST' not in record.text and 'AGGREGATE-LAST' not in record.text
    elif mode=='summary':
        assert 'tool output omitted:' in record.text and 'AGGREGATE-FIRST' in record.text
        assert 'AGGREGATE-LAST' not in record.text
    elif mode=='tail':
        assert 'EXPORTER TOOL OUTPUT TRUNCATED' in record.text and 'AGGREGATE-LAST' in record.text
        assert 'AGGREGATE-FIRST' not in record.text
    else:
        assert body in record.text


@pytest.mark.parametrize('renderer',['--fast','--no-fast'])
def test_cli_preserves_aggregate_output_completion_metadata_and_exact_native_bytes(tmp_path,renderer):
    source=tmp_path/'source.jsonl'
    rows=[
        {'type':'event_msg','payload':{'type':'user_message','message':'Synthetic request'}},
        command_event(command=['echo','synthetic'],aggregated_output='Native aggregate-only sentinel 🧠',exit_code=5,status='failed'),
        {'type':'event_msg','payload':{'type':'agent_message','message':'Synthetic response'}},
    ]
    original=b''.join((json.dumps(row,ensure_ascii=False)+'\r\n').encode() for row in rows)
    source.write_bytes(original)
    result=subprocess.run([sys.executable,str(ROOT/'Export-CodexThread.py'),'--jsonl',str(source),
        '--mode','thread',renderer,'--no-archive','--no-progress','--out-dir',str(tmp_path/'out'),
        '--no-remember-out-dir','--tokenizer','regex','--json'],capture_output=True,text=True,timeout=60,
        env={**os.environ,'CODEX_HOME':str(tmp_path/'home'),'CODEX_EXPORT_CONFIG':str(tmp_path/'config.json')})
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    markdown=Path(value['file']).read_text(encoding='utf-8')
    assert 'Native aggregate-only sentinel 🧠' in markdown
    assert 'Exit code 5' in markdown and 'Status: failed' in markdown
    assert Path(value['source_copy']).read_bytes()==original
    assert value['source_copy_sha256']==hashlib.sha256(original).hexdigest()
    assert source.read_bytes()==original


def output_parts(**values):
    return cli.extract_output_and_meta(values,strip=False,max_line_chars=20000,max_repeated_lines=25)


@pytest.mark.parametrize('key',['aggregated_output','aggregatedOutput'])
def test_aggregate_keeps_literal_json_and_console_transport_lookalikes(key):
    body='{"arbitrary_console_key": "literal result", "nested": [1, 2]}'
    text,_=output_parts(**{key:body})
    assert text==body
    headers='Chunk ID: literal printed text\nProcess exited with code 99\nOutput:\nActual console body'
    text,meta=output_parts(**{key:headers,'exit_code':2})
    assert text==headers and meta['exit_code']==2


@pytest.mark.parametrize('name',['stdout','stderr'])
def test_single_stream_is_literal_and_does_not_decode_json_or_strip_headers(name):
    for body in ['{"console_key": "retained"}', 'Output:\nprinted output',
                 'Process exited with code 91\nthis is printed console text']:
        text,meta=output_parts(**{name:body})
        assert text==body and 'exit_code' not in meta


@pytest.mark.parametrize('aggregate',[None,'',' \n ',[],{},42])
def test_empty_or_malformed_aggregate_falls_back_to_populated_alternative(aggregate):
    text,_=output_parts(aggregated_output=aggregate,output='Fallback body')
    assert text=='Fallback body'


def test_aggregate_priority_does_not_concatenate_alternative_output_representations():
    text,_=output_parts(aggregated_output='Canonical aggregate',output='Alternative output',
                       result='Alternative result',content='Alternative content')
    assert text=='Canonical aggregate'
    text,_=output_parts(output='Chosen output',result='Alternative result',
                       stdout='stdout alternative',stderr='stderr alternative',content='Alternative content')
    assert text=='Chosen output'


def test_truncated_aggregate_preserves_complete_additional_stdout_and_stderr():
    aggregate='STDOUT prefix\n[aggregate source truncated]\nSTDERR suffix'
    stdout='STDOUT prefix\nadditional stdout bytes that are not in the aggregate'
    stderr='additional stderr bytes that are not in the aggregate\nSTDERR suffix'
    text,_=output_parts(aggregated_output=aggregate,stdout=stdout,stderr=stderr)
    assert text==f'{aggregate}\n\nRecorded full stdout:\n{stdout}\n\nRecorded full stderr:\n{stderr}'


def test_streams_wholly_contained_in_aggregate_do_not_duplicate_it():
    aggregate='first stream\nsecond stream'
    text,_=output_parts(aggregated_output=aggregate,stdout='first stream',stderr='second stream')
    assert text==aggregate


def test_identical_additional_streams_both_remain_when_absent_from_aggregate():
    text,_=output_parts(aggregated_output='Aggregate only',stdout='Independent identical text',
                       stderr='Independent identical text')
    assert text.count('Independent identical text')==2
    assert 'Recorded full stdout:' in text and 'Recorded full stderr:' in text


@pytest.mark.parametrize('exit_key',['exit_code','exitCode'])
def test_root_completion_overrides_nested_metadata_envelope_and_transport_hints(exit_key):
    envelope=json.dumps({'output':'Process exited with code 99\nOutput:\nConsole body',
                         'metadata':{'exitCode':88,'status':'completed'}})
    text,meta=output_parts(output=envelope,metadata={'exitCode':77,'status':'running'},
                           **{exit_key:6,'status':'failed'})
    assert text=='Console body' and meta['exit_code']==6 and meta['status']=='failed'
    record=classify(command_event(command=['run'],output=envelope,
                    metadata={'exitCode':77,'status':'completed'},**{exit_key:6,'status':'failed'}))
    assert 'Exit code 6' in record.text and 'Status: failed' in record.text
    assert 'Success' not in record.text


def test_explicit_null_completion_does_not_resurrect_stale_nested_aliases():
    text,meta=output_parts(output=json.dumps({'output':'body','metadata':{'exitCode':5,'status':'failed'}}),
                          metadata={'exitCode':4,'status':'completed'},exit_code=None,status=None)
    assert text=='body' and meta['exit_code'] is None and meta['status'] is None
    assert cli.tool_completion_lines(meta)==[]


@pytest.mark.parametrize('status',['failed','running','in_progress','interrupted','cancelled'])
def test_zero_exit_with_non_success_status_does_not_claim_success(status):
    record=classify(command_event(command=['synthetic'],aggregated_output='body',exit_code=0,status=status))
    assert 'Exit code 0' in record.text and f'Status: {status}' in record.text
    assert 'Success' not in record.text


def test_program_code_is_not_mistaken_for_a_root_exit_code():
    _,meta=output_parts(code='print(42)',output='42')
    assert 'exit_code' not in meta


def test_completion_only_record_survives_none_output_policy():
    record=classify(command_event(aggregated_output='body hidden by policy',exit_code=7,status='failed'),tool_outputs='none')
    assert record is not None and 'body hidden by policy' not in record.text
    assert record.text=='Exit code 7\n\nStatus: failed'


def test_patch_branch_keeps_failure_metadata_despite_success_looking_output():
    patch='*** Begin Patch\n*** Add File: synthetic.txt\n+example\n*** End Patch'
    record=classify(command_event(command=patch,aggregated_output='Success-looking console line',exit_code=2,status='failed'))
    assert 'Exit code 2' in record.text and 'Status: failed' in record.text
    assert 'Success-looking console line' in record.text
    assert '\n\nSuccess\n' not in record.text and 'Success (exit code 0)' not in record.text
