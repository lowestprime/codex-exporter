"""Large metadata histories keep ordered JSON values without repeated list scans."""
from __future__ import annotations

import json

from codex_exporter import cli


def test_metadata_values_keep_first_seen_order_normalization_and_plain_json():
    meta = {'_models_seen': ['initial-model'], 'ordinary': {'retained': True}}
    for value in (' initial-model ', 'model-b', 'model-a', 'model-b', '', '  '):
        cli.add_unique_meta(meta, '_models_seen', value)
    for value in ('later', 'earlier', 'later'):
        cli.add_unique_meta(meta, '_updated_candidates', value)
    expected = {'_models_seen': ['initial-model', 'model-b', 'model-a'],
                '_updated_candidates': ['later', 'earlier'], 'ordinary': {'retained': True}}
    assert meta == expected
    assert json.loads(json.dumps(meta)) == expected
    assert json.loads(json.dumps(meta, indent=2, sort_keys=True)) == expected
    assert set(meta) == set(expected)


def test_existing_bucket_is_indexed_once_and_never_linearly_searched_again():
    class ExistingValues(list):
        def __contains__(self, value):
            raise AssertionError('The metadata collector must not scan the growing value list')

    initial = ExistingValues(['existing'])
    meta = {'_updated_candidates': initial}
    cli.add_unique_meta(meta, '_updated_candidates', 'existing')
    active = meta['_updated_candidates']
    for number in range(25_000):
        cli.add_unique_meta(meta, '_updated_candidates', f'timestamp-{number}')
        assert meta['_updated_candidates'] is active
    cli.add_unique_meta(meta, '_updated_candidates', 'timestamp-0')
    assert len(active) == 25_001
    assert active[:3] == ['existing', 'timestamp-0', 'timestamp-1']
    assert active[-1] == 'timestamp-24999'
    assert initial == ['existing']


def test_parser_returns_plain_lists_and_manifest_contains_no_transient_cache(tmp_path):
    source = tmp_path / 'source.jsonl'
    rows = [
        {'timestamp': '2030-01-02T00:00:00Z', 'type': 'session_meta',
         'payload': {'id': 'synthetic', 'created_at': '2030-01-01T00:00:00Z', 'title': 'Synthetic'}},
        {'timestamp': '2030-01-01T00:00:00Z', 'type': 'turn_context',
         'payload': {'model': 'synthetic-model'}},
        {'timestamp': '2030-01-02T00:00:00Z', 'type': 'response_item',
         'payload': {'type': 'configuration_update', 'reasoning': {'effort': 'xhigh'}}},
        {'timestamp': '2030-01-03T00:00:00Z', 'type': 'event_msg',
         'payload': {'type': 'user_message', 'message': 'Synthetic prompt'}},
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    records, meta = cli.parse_session(source, include_actions=True, tool_outputs='full', max_tool_chars=20000,
                                      strip=False, max_line_chars=20000, max_repeated_lines=25)
    assert meta['_updated_candidates'] == ['2030-01-02T00:00:00Z', '2030-01-01T00:00:00Z', '2030-01-03T00:00:00Z']
    for key in ('_updated_candidates', '_created_candidates', '_models_seen', '_reasoning_efforts_seen', '_candidate_titles'):
        assert type(meta[key]) is list
    serialized = json.dumps(meta, sort_keys=True)
    assert '"_seen"' not in serialized and 'UniqueMetadata' not in serialized
    assert json.loads(serialized)['_models_seen'] == ['synthetic-model']
    manifest = cli.build_manifest(source=source, output=None, session_id='synthetic', title='Synthetic',
                                  mode='thread', records=records, meta=meta, text='Synthetic prompt\n', redacted=False)
    encoded = json.dumps(manifest, sort_keys=True)
    assert '"_seen"' not in encoded and 'UniqueMetadata' not in encoded
    assert json.loads(encoded)['content_summary']['models'] == ['synthetic-model']
