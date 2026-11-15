"""Validated presentation preferences shared by configuration, help and CLI."""
from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    'sort': '', 'sort_order': 'auto', 'pager': 'auto', 'page_size': 0,
    'timestamps': True, 'timestamp_timezone': 'local',
}


def validate_preferences(values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unknown display preference: ' + ', '.join(sorted(unknown)))
    result = {**DEFAULTS, **values}
    if not isinstance(result['sort'], str):
        raise ValueError('sort must be a comma-separated field specification')
    if result['sort_order'] not in ('auto', 'asc', 'desc'):
        raise ValueError('sort_order must be auto, asc or desc')
    if result['pager'] not in ('auto', 'always', 'never'):
        raise ValueError('pager must be auto, always or never')
    if type(result['page_size']) is not int or result['page_size'] < 0:
        raise ValueError('page_size must be zero (terminal height) or a positive integer')
    if type(result['timestamps']) is not bool:
        raise ValueError('timestamps must be true or false')
    if not isinstance(result['timestamp_timezone'], str) or not result['timestamp_timezone'].strip():
        raise ValueError('timestamp_timezone must be local, UTC, source, an offset or an IANA zone')
    from .timestamps import TimestampFormatter
    TimestampFormatter(result['timestamps'], result['timestamp_timezone'])
    return result


def effective_preferences(config: dict[str, Any]) -> dict[str, Any]:
    values = config.get('display_defaults') or {}
    if not isinstance(values, dict):
        raise ValueError('display_defaults must be a JSON object')
    return validate_preferences(values)


def update_preferences(config: dict[str, Any], assignments: list[str]) -> dict[str, Any]:
    values = dict(config.get('display_defaults') or {})
    for assignment in assignments:
        key, separator, value = assignment.partition('=')
        if not separator or key not in DEFAULTS:
            raise ValueError('--set-default accepts KEY=VALUE for: ' + ', '.join(DEFAULTS))
        if key == 'timestamps':
            if value.casefold() not in ('true', 'false', 'yes', 'no', 'on', 'off', '1', '0'):
                raise ValueError('timestamps expects a boolean: true/false, yes/no, on/off or 1/0')
            values[key] = value.casefold() in ('true', 'yes', 'on', '1')
        elif key == 'page_size':
            try:
                values[key] = int(value)
            except ValueError as exc:
                raise ValueError('page_size expects an integer') from exc
        else:
            values[key] = value
    validate_preferences(values)
    config['display_defaults'] = values
    return values
