"""Known native/legacy actions stay known when a message view skips their bodies."""
from __future__ import annotations

import json

import pytest

from codex_exporter import cli


NATIVE_ITEMS = {
    'CommandExecution': {'command': ['echo', 'synthetic'], 'aggregated_output': 'recorded output'},
    'FunctionCallOutput': {'name': 'synthetic', 'output': 'recorded output'},
    'DynamicToolCall': {'tool': 'synthetic', 'arguments': {'cmd': 'echo synthetic'}},
    'FileChange': {'changes': {'synthetic.txt': {'type': 'add', 'content': 'saved'}}},
    'Extension': {'extension_name': 'synthetic', 'result': 'returned'},
    'McpToolCall': {'server': 'synthetic', 'tool': 'synthetic', 'arguments': {}, 'result': {'value': 'returned'}},
    'ImageView': {'path': '/synthetic/image.png'},
}
RESPONSE_TYPES = (
    'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output',
    'local_shell_call', 'local_shell_call_output', 'mcp_tool_call_output',
    'web_search_call', 'tool_search_call', 'tool_search_output', 'image_generation_call',
)
LEGACY_EVENTS = (
    'exec_command_begin', 'exec_command_end', 'patch_apply_begin', 'patch_apply_updated',
    'patch_apply_end', 'mcp_tool_call_begin', 'mcp_tool_call_end',
    'dynamic_tool_call_request', 'dynamic_tool_call_response', 'view_image_tool_call',
)
OPTIONS = dict(seq=1, tool_outputs='full', max_tool_chars=20000,
               strip=False, max_line_chars=20000, max_repeated_lines=25)


def native_item(kind, *, lifecycle='item_completed'):
    return {'type':'event_msg', 'payload':{'type':lifecycle, 'item':{'type':kind, 'id':'synthetic', **NATIVE_ITEMS[kind]}}}


def parse(tmp_path, rows, *, actions):
    path=tmp_path/'source.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    options={key:value for key,value in OPTIONS.items() if key != 'seq'}
    return cli.parse_session(path, include_actions=actions, **options)


@pytest.mark.parametrize('kind', NATIVE_ITEMS)
def test_each_native_completed_action_is_renderable_and_intentionally_hidden(kind):
    obj=native_item(kind)
    shown=cli.classify_record(obj, include_actions=True, **OPTIONS)
    assert shown is not None and shown.kind in {'action','note'}
    assert cli.classify_record(obj, include_actions=False, **OPTIONS) is None
    assert cli.known_ignored_reason(obj, False, include_actions=False)=='action_rendering_disabled'


@pytest.mark.parametrize('kind', NATIVE_ITEMS)
@pytest.mark.parametrize('lifecycle', ['item_started','item_updated','item_completed'])
def test_known_native_action_lifecycle_discriminators_remain_known_without_rendering(kind,lifecycle):
    obj=native_item(kind,lifecycle=lifecycle)
    assert cli.known_ignored_reason(obj, False, include_actions=False)=='action_rendering_disabled'


@pytest.mark.parametrize('kind', RESPONSE_TYPES)
@pytest.mark.parametrize('envelope', ['response_item','event_item','response_item_item','direct'])
def test_known_response_action_discriminators_cover_direct_and_historical_nested_envelopes(kind,envelope):
    # The generic action extractor already supports these fields; this matrix
    # checks schema recognition rather than inventing specialized body rendering.
    item={'type':kind,'arguments':'{"cmd":"echo synthetic"}','output':'recorded output'}
    if envelope=='response_item':
        obj={'type':'response_item','payload':item}
    elif envelope=='direct':
        obj=item
    else:
        obj={'type':'event' if envelope=='event_item' else 'response_item',
             'payload':{'type':'item','item':item}}
    assert cli.classify_record(obj, include_actions=True, **OPTIONS) is not None
    assert cli.classify_record(obj, include_actions=False, **OPTIONS) is None
    assert cli.known_ignored_reason(obj, False, include_actions=False)=='action_rendering_disabled'


@pytest.mark.parametrize('kind', LEGACY_EVENTS)
def test_known_legacy_action_events_do_not_turn_into_unknown_schemas_when_hidden(kind):
    obj={'type':'event_msg','payload':{'type':kind,'command':'echo synthetic','output':'recorded output'}}
    assert cli.classify_record(obj, include_actions=True, **OPTIONS) is not None
    assert cli.classify_record(obj, include_actions=False, **OPTIONS) is None
    assert cli.known_ignored_reason(obj, False, include_actions=False)=='action_rendering_disabled'


def test_registry_and_synthetic_discriminator_matrix_stay_in_sync():
    assert cli.KNOWN_NATIVE_ACTION_ITEMS == {value.casefold() for value in NATIVE_ITEMS}
    assert cli.KNOWN_RESPONSE_ACTION_TYPES == set(RESPONSE_TYPES)
    assert cli.KNOWN_LEGACY_ACTION_EVENTS == set(LEGACY_EVENTS)


@pytest.mark.parametrize('kind',['FutureToolCall','UnknownCommandExecution','FileChangeV2','ToolResultV99'])
def test_unknown_future_tool_like_discriminators_are_not_whitelisted(kind):
    obj={'type':'event_msg','payload':{'type':'item_completed','item':{'type':kind,'command':'echo retained','output':'recorded'}}}
    assert cli.known_ignored_reason(obj, False, include_actions=False)=='unrecognized_schema'


def test_body_is_never_decoded_or_traversed_for_disabled_action_recognition(monkeypatch):
    class ForbiddenBody(dict):
        def __iter__(self):
            raise AssertionError('Body traversed')
        def items(self):
            raise AssertionError('Body traversed')
        def __str__(self):
            raise AssertionError('Body stringified')
    def forbidden(*args,**kwargs):
        raise AssertionError('Disabled action body rendered')
    monkeypatch.setattr(cli,'extract_action_record',forbidden)
    monkeypatch.setattr(cli,'extract_text',forbidden)
    monkeypatch.setattr(cli,'maybe_json',forbidden)
    for kind in NATIVE_ITEMS:
        obj={'type':'event_msg','payload':{'type':'item_completed','item':{
            'type':kind,'arguments':ForbiddenBody(), 'output':ForbiddenBody(), 'content':ForbiddenBody()}}}
        assert cli.classify_record(obj, include_actions=False, **OPTIONS) is None
        assert cli.known_ignored_reason(obj,False,include_actions=False)=='action_rendering_disabled'


def test_real_failure_schema_keys_are_counted_as_disabled_not_unknown(tmp_path):
    rows=[native_item('CommandExecution'),native_item('FunctionCallOutput')]
    records,meta=parse(tmp_path,rows,actions=False)
    assert records==[] and not meta['_unknown_event_types']
    assert dict(meta['_ignored_event_types'])=={
        'event_msg/item_completed/CommandExecution':1,
        'event_msg/item_completed/FunctionCallOutput':1,
    }
    assert meta['_ignored_event_reasons']['action_rendering_disabled']==2
    records,shown=parse(tmp_path,rows,actions=True)
    assert records and not shown['_unknown_event_types']


def test_actual_scale_failure_counts_remain_visible_in_audit_without_retaining_tool_bodies(tmp_path):
    rows=[native_item('CommandExecution')]*46_757+[native_item('FunctionCallOutput')]
    records,meta=parse(tmp_path,rows,actions=False)
    assert records==[] and not meta['_unknown_event_types']
    assert meta['_ignored_event_types']['event_msg/item_completed/CommandExecution']==46_757
    assert meta['_ignored_event_types']['event_msg/item_completed/FunctionCallOutput']==1
    assert meta['_ignored_event_reasons']['action_rendering_disabled']==46_758
