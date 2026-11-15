"""Render recorded event times without inventing a missing date or timezone.

Local conversion asks the operating system for the offset at the event instant;
never reuse today's fixed offset for historical records. IANA zones use Python's
zoneinfo database. This module only formats Markdown and export metadata: native
JSONL bytes and source timestamp values remain unchanged.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_MONTHS = ('', 'January', 'February', 'March', 'April', 'May', 'June',
           'July', 'August', 'September', 'October', 'November', 'December')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
_DATE_ONLY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_FIXED_OFFSET = re.compile(r'^(?:UTC)?([+-])(\d{2}):?(\d{2})$', re.I)
_TIMESTAMP_KEYS = ('timestamp', 'time', 'created_at', 'createdAt', 'create_time')
_WRAPPER_KEYS = ('payload', 'item', 'message', 'event', 'record',
                 'internal_chat_message_metadata_passthrough')
# Windows may expose descriptive names rather than abbreviations. Match the
# expected offset too; unknown/localized descriptions are retained, not guessed.
_WINDOWS_NAMES = {
    'Pacific Standard Time': ('PST', -8 * 60),
    'Pacific Daylight Time': ('PDT', -7 * 60),
    'Mountain Standard Time': ('MST', -7 * 60),
    'Mountain Daylight Time': ('MDT', -6 * 60),
    'Central Standard Time': ('CST', -6 * 60),
    'Central Daylight Time': ('CDT', -5 * 60),
    'Eastern Standard Time': ('EST', -5 * 60),
    'Eastern Daylight Time': ('EDT', -4 * 60),
    'Alaskan Standard Time': ('AKST', -9 * 60),
    'Alaskan Daylight Time': ('AKDT', -8 * 60),
    'Hawaiian Standard Time': ('HST', -10 * 60),
    'Atlantic Standard Time': ('AST', -4 * 60),
    'Atlantic Daylight Time': ('ADT', -3 * 60),
}


def extract_source_timestamp(obj: dict[str, Any]) -> str:
    """Find event metadata, never dates embedded in tool output or message text.

    A recorded outer event timestamp takes precedence. Known event wrappers may
    carry the timestamp instead; inspect them breadth-first to a fixed depth.
    Preserve malformed strings for source provenance; the formatter handles them
    separately. In particular, Unix epoch zero is valid and is not discarded.
    """
    pending = deque([(obj, 0, False)])
    seen: set[int] = set()
    while pending:
        record, depth, native_metadata = pending.popleft()
        if not isinstance(record, dict) or id(record) in seen:
            continue
        seen.add(id(record))
        for key in _TIMESTAMP_KEYS:
            value = record.get(key)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                if native_metadata and key == 'create_time':
                    # Preserve the selector's established ISO representation of
                    # native metadata epoch values; raw JSONL remains unchanged.
                    parsed = parse_source_timestamp(value)
                    if parsed is not None:
                        return parsed.isoformat()
                return str(value)
        if depth < 3:
            pending.extend((record[key], depth + 1, key == 'internal_chat_message_metadata_passthrough')
                           for key in _WRAPPER_KEYS
                           if isinstance(record.get(key), dict))
    return ''


def parse_source_timestamp(value: Any) -> datetime | None:
    """Parse ISO 8601 or Unix seconds; leave offset-free times explicitly naive.

    Numeric values always mean seconds, never guessed milliseconds. Invalid,
    non-finite and out-of-range values produce None. Epoch arithmetic supports
    pre-1970 timestamps even on platforms with restricted C localtime ranges.
    """
    if isinstance(value, datetime):
        return value
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if not _NUMBER.fullmatch(value):
            try:
                # Python 3.10 fromisoformat does not accept a trailing Z.
                return datetime.fromisoformat(value[:-1] + '+00:00'
                                               if value[-1:] in {'Z', 'z'} else value)
            except ValueError:
                return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        return _EPOCH + timedelta(seconds=seconds)
    except (ValueError, TypeError, OverflowError):
        return None


def _offset_label(offset: timedelta | None) -> str:
    if offset is None:
        return 'timezone unspecified'
    seconds = int(offset.total_seconds())
    if not seconds:
        return 'UTC'
    sign = '+' if seconds >= 0 else '-'
    hours, remainder = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'UTC{sign}{hours:02d}:{minutes:02d}' + (f':{seconds:02d}' if seconds else '')


def _zone_label(when: datetime) -> str:
    offset = when.utcoffset()
    label = when.tzname() or _offset_label(offset)
    known = _WINDOWS_NAMES.get(label)
    if known and offset == timedelta(minutes=known[1]):
        return known[0]
    # Numeric tzdata names (+0545, -03) are more legible with an explicit UTC
    # prefix, and are never confused with a guessed regional abbreviation.
    if re.fullmatch(r'[+-]\d{2}(?:\d{2})?(?:\d{2})?', label):
        return _offset_label(offset)
    return label


def _localize(when: datetime) -> datetime:
    """Use the event's instant, not the current local DST/UTC offset."""
    return when.astimezone()


class TimestampFormatter:
    """Resolve a display timezone once, then cheaply format many event records."""

    def __init__(self, enabled: bool = True, timezone_name: str = 'local') -> None:
        self.enabled = bool(enabled)
        self.timezone_name = str(timezone_name or 'local').strip() or 'local'
        self._zone: tzinfo | None = None
        normalized = self.timezone_name.casefold()
        if normalized in {'local', 'source'}:
            self.timezone_name = normalized
        elif normalized in {'utc', 'z'}:
            self.timezone_name = 'UTC'
            self._zone = timezone.utc
        else:
            match = _FIXED_OFFSET.fullmatch(self.timezone_name)
            if match:
                sign, hours, minutes = match.groups()
                if int(hours) > 23 or int(minutes) > 59:
                    raise ValueError('Timestamp timezone offset must be between -23:59 and +23:59')
                offset = timedelta(hours=int(hours), minutes=int(minutes))
                if sign == '-':
                    offset = -offset
                self.timezone_name = _offset_label(offset)
                self._zone = timezone(offset)
            else:
                try:
                    self._zone = ZoneInfo(self.timezone_name)
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    raise ValueError(
                        f'Unknown or unavailable timestamp timezone {self.timezone_name!r}. '
                        'Use local, UTC, source, an offset such as +05:30, or an IANA name '
                        'such as America/Los_Angeles. IANA zones on Windows may require '
                        'installing the timezone database: python -m pip install tzdata.'
                    ) from exc

    def format(self, value: Any) -> str:
        if not self.enabled:
            return ''
        when = parse_source_timestamp(value)
        if when is None:
            return ''
        if isinstance(value, str) and _DATE_ONLY.fullmatch(value.strip()):
            return f'{_MONTHS[when.month]} {when.day}, {when.year:04d} (time not recorded)'
        if when.utcoffset() is None:
            label = 'timezone unspecified'
        else:
            try:
                if self.timezone_name == 'local':
                    when = _localize(when)
                elif self._zone is not None:
                    when = when.astimezone(self._zone)
                label = _zone_label(when)
            except (OverflowError, OSError, ValueError):
                # A platform may be unable to convert an extreme historical date.
                # Keep the recorded instant/offset rather than substitute now.
                label = _zone_label(when) + ' (requested timezone conversion unavailable)'
        hour = when.hour % 12 or 12
        meridiem = 'AM' if when.hour < 12 else 'PM'
        return (f'{_MONTHS[when.month]} {when.day}, {when.year:04d} at '
                f'{hour:02d}:{when.minute:02d}:{when.second:02d} {meridiem} {label}')

    def markdown(self, value: Any) -> str:
        label = self.format(value)
        return f'_{label}_\n\n' if label else ''

    def metadata(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'timezone': self.timezone_name,
            'format': 'Month D, YYYY at hh:mm:ss AM/PM zone',
            'source': 'recorded event timestamp; never export creation time',
            'missing_or_invalid': 'omit timestamp',
            'naive_time': 'preserve recorded wall time; timezone unspecified',
            'precision': 'whole seconds for display; original source values retained',
        }


def timestamp_policy(enabled: bool = True, timezone_name: str = 'local') -> dict[str, Any]:
    """Validate a display policy and return its serializable manifest form."""
    return TimestampFormatter(enabled, timezone_name).metadata()
