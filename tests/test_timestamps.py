"""Recorded-time rendering: DST, missing metadata and source-byte fidelity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from codex_exporter.chatgpt_archive import export_chatgpt
from codex_exporter.modern import stream_export
from codex_exporter import timestamps
from codex_exporter.timestamps import (TimestampFormatter, extract_source_timestamp,
                                       parse_source_timestamp, timestamp_policy)


@pytest.mark.parametrize(('stamp', 'expected'), [
    ('2026-11-15T01:50:00Z', 'November 14, 2026 at 05:50:00 PM PST'),
    ('2026-07-15T00:50:00Z', 'July 14, 2026 at 05:50:00 PM PDT'),
    ('2026-11-01T08:30:00Z', 'November 1, 2026 at 01:30:00 AM PDT'),
    ('2026-11-01T09:30:00Z', 'November 1, 2026 at 01:30:00 AM PST'),
    ('2026-03-08T09:59:59Z', 'March 8, 2026 at 01:59:59 AM PST'),
    ('2026-03-08T10:00:00Z', 'March 8, 2026 at 03:00:00 AM PDT'),
])
def test_pacific_dst_and_inclusive_fold(stamp, expected):
    assert TimestampFormatter(timezone_name='America/Los_Angeles').format(stamp) == expected


@pytest.mark.parametrize(('value', 'expected'), [
    (0, 'January 1, 1970 at 12:00:00 AM UTC'),
    ('0', 'January 1, 1970 at 12:00:00 AM UTC'),
    (Decimal('0.125'), 'January 1, 1970 at 12:00:00 AM UTC'),
    (-1, 'December 31, 1969 at 11:59:59 PM UTC'),
    ('2026-11-14T17:50:00-08:00', 'November 15, 2026 at 01:50:00 AM UTC'),
    ('2026-11-14T17:50:00.123456-08:00', 'November 15, 2026 at 01:50:00 AM UTC'),
    ('2026-11-14T17:50:00', 'November 14, 2026 at 05:50:00 PM timezone unspecified'),
    ('2026-11-14', 'November 14, 2026 (time not recorded)'),
])
def test_parsing_preserves_actual_source_time(value, expected):
    assert TimestampFormatter(timezone_name='UTC').format(value) == expected


@pytest.mark.parametrize('value', [None, '', '  ', False, True, 'not a timestamp',
                                  '2026-02-30T17:50:00Z', float('nan'), float('inf'),
                                  Decimal('NaN'), '1e9999', {}, [], 10**400])
def test_missing_and_invalid_never_substitute_export_time(value):
    formatter = TimestampFormatter(timezone_name='UTC')
    assert formatter.format(value) == ''
    assert formatter.markdown(value) == ''


def test_source_and_explicit_offset_modes():
    stamp = '2026-11-14T17:50:00-08:00'
    assert TimestampFormatter(timezone_name='source').format(stamp).endswith('05:50:00 PM UTC-08:00')
    assert TimestampFormatter(timezone_name='+05:30').format(stamp) == 'November 15, 2026 at 07:20:00 AM UTC+05:30'
    assert TimestampFormatter(timezone_name='UTC-0800').format(stamp).endswith('05:50:00 PM UTC-08:00')
    assert timestamp_policy(timezone_name='+0000')['timezone'] == 'UTC'


def test_disabled_formatting_avoids_parsing(monkeypatch):
    formatter = TimestampFormatter(enabled=False)
    monkeypatch.setattr(timestamps, 'parse_source_timestamp', lambda value: pytest.fail('disabled format parsed timestamp'))
    assert formatter.format('2026-11-15T01:50:00Z') == ''
    assert formatter.markdown('2026-11-15T01:50:00Z') == ''
    assert formatter.metadata()['enabled'] is False


@pytest.mark.parametrize(('long_name', 'offset', 'abbreviation'), [
    ('Pacific Standard Time', -8, 'PST'),
    ('Pacific Daylight Time', -7, 'PDT'),
    ('Eastern Standard Time', -5, 'EST'),
    ('Unrecognized local display name', -8, 'Unrecognized local display name'),
    # Do not attach a familiar but incorrect abbreviation to an unexpected offset.
    ('Pacific Standard Time', -6, 'Pacific Standard Time'),
])
def test_windows_local_timezone_descriptions(monkeypatch, long_name, offset, abbreviation):
    zone = timezone(timedelta(hours=offset), long_name)
    monkeypatch.setattr(timestamps, '_localize', lambda when: when.astimezone(zone))
    assert TimestampFormatter().format('2026-11-15T01:50:00Z').endswith(abbreviation)


def test_local_zone_uses_each_events_dst_offset(monkeypatch):
    if not hasattr(time, 'tzset'):
        pytest.skip('POSIX timezone-switch test; Windows name conversion covered separately')
    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    time.tzset()
    try:
        formatter = TimestampFormatter()
        assert formatter.format('2026-11-15T01:50:00Z').endswith('05:50:00 PM PST')
        assert formatter.format('2026-07-15T00:50:00Z').endswith('05:50:00 PM PDT')
    finally:
        monkeypatch.undo()
        time.tzset()


def test_missing_iana_database_has_actionable_error(monkeypatch):
    def unavailable(key):
        raise ZoneInfoNotFoundError(key)
    monkeypatch.setattr(timestamps, 'ZoneInfo', unavailable)
    with pytest.raises(ValueError, match='pip install tzdata'):
        TimestampFormatter(timezone_name='America/Los_Angeles')
    # Local/UTC and source-offset display do not require a timezone dependency.
    assert TimestampFormatter(timezone_name='UTC').format(0).endswith('UTC')
    assert TimestampFormatter(timezone_name='source').format(0).endswith('UTC')
    TimestampFormatter(timezone_name='local')


@pytest.mark.parametrize('zone', ['+24:00', '-01:60', 'Not/A_Real_Zone', '../UTC'])
def test_invalid_zone_fails_clearly(zone):
    with pytest.raises(ValueError, match='[Tt]imestamp timezone'):
        TimestampFormatter(timezone_name=zone)


def test_native_timestamp_extraction_metadata_only():
    assert extract_source_timestamp({'timestamp': 0, 'payload': {'timestamp': 'later'}}) == '0'
    assert extract_source_timestamp({'payload': {'item': {'created_at': '2026-11-15T01:50:00Z'}}}) == '2026-11-15T01:50:00Z'
    assert extract_source_timestamp({'message': {'create_time': Decimal('3.125')}}) == '3.125'
    assert extract_source_timestamp({'timestamp': False, 'payload': {'timestamp': 'valid outer fallback'}}) == 'valid outer fallback'
    assert extract_source_timestamp({'payload': {'output': {'timestamp': 'tool result date'}}}) == ''
    assert extract_source_timestamp({'payload': {'content': [{'timestamp': 'quoted message'}]}}) == ''
    assert extract_source_timestamp({'timestamp': 'malformed native metadata'}) == 'malformed native metadata'


def _rollout(path: Path) -> bytes:
    rows = [
        {'type': 'event', 'timestamp': '2026-11-15T01:50:00Z',
         'payload': {'type': 'user_message', 'message': 'Original prompt'}},
        {'type': 'event', 'timestamp': '2026-11-15T01:50:01Z',
         'payload': {'type': 'agent_message', 'message': 'Original reply'}},
        {'type': 'event', 'timestamp': '2026-11-15T01:50:02Z',
         'payload': {'type': 'item', 'item': {'type': 'local_shell_call',
                    'arguments': '{"cmd":"echo preserved"}'}}},
        {'type': 'event', 'payload': {'type': 'agent_message', 'message': 'Timestamp absent'}},
    ]
    data = ''.join(json.dumps(row) + '\n' for row in rows).encode()
    path.write_bytes(data)
    return data


@pytest.mark.parametrize('show', [True, False])
def test_stream_timestamps_all_roles_without_changing_native_jsonl(tmp_path, show):
    source = tmp_path / 'rollout.jsonl'
    original = _rollout(source)
    result = stream_export(source, tmp_path / 'out.md', session_id='test', title='Recorded times',
                           progress=False, show_timestamps=show, timestamp_timezone='America/Los_Angeles')
    text = Path(result['file']).read_text()
    manifest = json.loads(Path(result['manifest']).read_text())
    assert source.read_bytes() == original
    assert result['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert manifest['timestamp_policy']['enabled'] is show
    assert manifest['timestamp_policy']['timezone'] == 'America/Los_Angeles'
    assert all(body in text for body in ('Original prompt', 'Original reply', 'echo preserved', 'Timestamp absent'))
    assert '_November 14, 2026 at 05:50:00 PM PST_\n\nEvent schema:' in text if show else '_November' not in text
    assert text.count('_November 14, 2026 at 05:50:') == (3 if show else 0)
    assert 'Timestamp: `' not in text


@pytest.mark.parametrize('show', [True, False])
def test_chatgpt_uses_message_time_not_conversation_time(tmp_path, show):
    path = tmp_path / 'conversations.json'
    stamp = datetime(2026, 11, 15, 1, 50, tzinfo=timezone.utc).timestamp()
    conv = {'id': 'sample', 'title': 'Recorded times', 'create_time': 0, 'mapping': {
        'a': {'parent': None, 'children': ['b'], 'message': {
            'author': {'role': 'user'}, 'create_time': stamp,
            'content': {'parts': ['Original prompt']}}},
        'b': {'parent': 'a', 'children': ['c'], 'message': {
            'author': {'role': 'assistant'}, 'create_time': stamp + 1,
            'content': {'parts': ['Original reply']}}},
        'c': {'parent': 'b', 'children': [], 'message': {
            'author': {'role': 'assistant'}, 'content': {'parts': ['No message date']}}},
    }}
    path.write_text(json.dumps([conv]))
    original = path.read_bytes()
    results = export_chatgpt(path, tmp_path / 'out', conversation_ids=[], title_match='',
                            show_timestamps=show, timestamp_timezone='America/Los_Angeles')
    result = results[0]
    text = Path(result['file']).read_text()
    manifest = json.loads(Path(result['manifest']).read_text())
    assert path.read_bytes() == original
    assert text.count('_November 14, 2026 at 05:50:') == (2 if show else 0)
    assert '## Node 1: user\n\n_November 14' in text if show else '_November' not in text
    assert '_January 1, 1970' not in text
    assert '- Timestamp (UTC):' not in text
    assert '- Created (UTC): `1970-01-01T00:00:00+00:00`' in text
    assert manifest['timestamp_policy']['enabled'] is show


@pytest.mark.parametrize('renderer', ['stream', 'chatgpt'])
def test_invalid_zone_rejected_before_output_creation(tmp_path, renderer):
    output = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='timestamp timezone'):
        if renderer == 'stream':
            stream_export(tmp_path / 'unused.jsonl', output / 'out.md',
                          session_id='test', title='Test', timestamp_timezone='Not/A_Real_Zone')
        else:
            export_chatgpt(tmp_path / 'unused.json', output, conversation_ids=[], title_match='',
                           timestamp_timezone='Not/A_Real_Zone')
    assert not output.exists()


def test_native_message_metadata_time_matches_selector_and_renderer(tmp_path):
    from codex_exporter.selection import index_turns
    row = {'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Prompt'}],
        'internal_chat_message_metadata_passthrough': {'turn_id': 'native', 'create_time': 0},
    }}
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps(row) + '\n')
    source_value = extract_source_timestamp(row)
    assert source_value == '1970-01-01T00:00:00+00:00'
    assert index_turns(path).turns[0].timestamp == source_value
    assert TimestampFormatter(timezone_name='UTC').format(source_value) == 'January 1, 1970 at 12:00:00 AM UTC'
