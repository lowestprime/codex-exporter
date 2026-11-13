"""Inclusive user-turn selection and byte-exact native JSONL excerpts.

The index retains offsets, short previews and hashes, not full conversation text.
An excerpt is a chronological selection from a verified snapshot: it never reads
again from the application's live rollout. Compaction/rollback events inside the
selection remain intact and are disclosed, rather than rewritten as new history.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable
import unicodedata

from .progress import ProgressReporter
from .snapshot import CHUNK_BYTES, SourceSnapshot

PREVIEW_CHARS = 240


class SelectionError(ValueError):
    """A selector is invalid, missing or ambiguous; no excerpt was published."""

    def __init__(self, message: str, *, candidates: Iterable['TurnAnchor'] = ()):
        self.candidates = tuple(candidates)
        if self.candidates:
            rows = [f"  turn:{turn.number} | {turn.timestamp or 'no timestamp'} | "
                    f"id:{_preview(turn.turn_id, 80) or '(unavailable)'} | {turn.preview}"
                    for turn in self.candidates[:20]]
            message += '\n' + '\n'.join(rows)
            if len(self.candidates) > 20:
                message += f'\n  ... {len(self.candidates) - 20:,} additional matches; use --list-turns.'
        super().__init__(message)


@dataclass(frozen=True)
class SourceRange:
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    kind: str = 'selected'


@dataclass(frozen=True)
class TurnAnchor:
    number: int
    line: int
    byte_start: int
    byte_end: int
    timestamp: str
    turn_id: str
    item_ids: tuple[str, ...]
    preview: str
    text_sha256: str
    source_kind: str
    duplicate_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class HistoryEvent:
    line: int
    kind: str
    embedded_history: bool


@dataclass(frozen=True)
class TurnIndex:
    path: Path
    size_bytes: int
    total_lines: int
    sha256: str
    turns: tuple[TurnAnchor, ...]
    session_meta: SourceRange | None
    turn_contexts: tuple[SourceRange, ...]
    history_events: tuple[HistoryEvent, ...]
    injected_context_lines: tuple[int, ...]
    repaired_json_lines: tuple[int, ...]
    signature: tuple[int, ...]


def _signature(path: Path) -> tuple[int, ...]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _stage(progress: Any, name: str, total: int):
    if hasattr(progress, 'stage'):
        return progress.stage(name, total=total)
    if hasattr(progress, 'update'):
        progress.update(0, total=total, detail=name)
        return nullcontext(progress)
    return ProgressReporter(enabled=bool(progress)).stage(name, total=total)


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    safe = ''.join(' ' if char.isspace() else char for char in text
                   if char.isspace() or unicodedata.category(char) not in ('Cc', 'Cf', 'Cs'))
    safe = ' '.join(safe.split())
    return safe if len(safe) <= limit else safe[:max(0, limit - 1)] + '…'


def _normalize(text: str) -> str:
    return ' '.join(text.split()).casefold()


def _bounded_lines(stream, size: int):
    remaining = size
    line = 0
    while remaining:
        raw = stream.readline(remaining)
        if not raw:
            raise SelectionError('Snapshot was truncated while reading selected source boundaries.')
        remaining -= len(raw)
        line += 1
        yield line, raw


def _parse(raw: bytes, line: int) -> tuple[dict[str, Any] | None, bool]:
    if not raw.strip():
        return None, False
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SelectionError(f'JSONL line {line:,} is not an object; cannot index exact user turns.')
        return value, False
    except (UnicodeError, json.JSONDecodeError):
        # Match the renderer's narrow legacy escape repair, while copying the
        # original bytes into excerpts. Import only on a malformed source line.
        from .cli import parse_jsonl_line
        try:
            value, info = parse_jsonl_line(raw.decode('utf-8-sig' if line == 1 else 'utf-8'))
        except UnicodeError as exc:
            raise SelectionError(f'JSONL line {line:,} is not valid UTF-8; exact turn selection is unavailable.') from exc
        if value is None:
            raise SelectionError(f'JSONL line {line:,} cannot be parsed; exact turn selection cannot safely skip it.')
        return value, info.get('status') != 'exact'


def _parts(value: Any, depth: int = 0) -> list[str]:
    if value is None or depth > 32:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part for child in value for part in _parts(child, depth + 1)]
    if not isinstance(value, dict):
        return []
    kind = str(value.get('type') or '').casefold()
    if ('image' in kind or 'audio' in kind
            or any(key in value for key in ('image_url', 'audio_url', 'b64_json', 'base64_image'))):
        # Match equivalent native/legacy media envelopes without retaining data
        # URLs in the index. Detail hints do not create a new user message.
        media = 'audio' if 'audio' in kind or 'audio_url' in value else 'image'
        identity = {key: value[key] for key in ('file_id', 'image_url', 'audio_url', 'path', 'b64_json', 'base64_image', 'data') if key in value}
        digest = hashlib.sha256()
        for encoded in json.JSONEncoder(sort_keys=True, ensure_ascii=False).iterencode(identity or value):
            digest.update(encoded.encode('utf-8'))
        return [f'[{media} sha256:{digest.hexdigest()}]']
    if kind in ('skill', 'mention'):
        return [f"[{kind}: {value.get('name') or value.get('path') or 'unspecified'}]"]
    for key in ('text', 'input_text', 'output_text', 'content', 'message', 'transcript'):
        if key in value:
            return _parts(value[key], depth + 1)
    return []


def _types(obj: dict[str, Any]):
    payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
    item = payload.get('item') if isinstance(payload.get('item'), dict) else {}
    return str(obj.get('type') or '').casefold(), payload, str(payload.get('type') or '').casefold(), item, str(item.get('type') or '').casefold()


def _timestamp(*objects: dict[str, Any]) -> str:
    for obj in objects:
        for key in ('timestamp', 'time', 'created_at', 'createdAt'):
            if obj.get(key):
                return str(obj[key])
    return ''


def is_injected_context(text: str) -> bool:
    """Recognize complete context envelopes, not a request following a block.

    Apply this only to model-context message surfaces. Explicit native user
    events are authoritative even when the user literally sends an AGENTS block.
    """
    remaining = text.strip()
    tags = ('environment_context', 'permissions instructions', 'collaboration_mode',
            'user_instructions', 'environments_instructions')
    for _ in range(64):
        agents = re.match(r'^#\s*agents\.md instructions(?:[ \t]+for[^\r\n]*|[ \t]*)(?:\r?\n|$)',
                          remaining, re.IGNORECASE)
        if agents:
            opening = re.search(r'<instructions>', remaining[agents.end():], re.IGNORECASE)
            if opening is None or remaining[agents.end():agents.end() + opening.start()].strip():
                return False
            body_start = agents.end() + opening.end()
            close = re.search(r'</instructions>', remaining[body_start:], re.IGNORECASE)
        else:
            opening = re.match('<(' + '|'.join(re.escape(tag) for tag in tags) + ')>',
                               remaining, re.IGNORECASE)
            if opening is None:
                return False
            body_start = opening.end()
            close = re.search('</' + re.escape(opening.group(1)) + '>', remaining[body_start:], re.IGNORECASE)
        if close is None:
            return False
        remaining = remaining[body_start + close.end():].strip()
        if not remaining:
            return True
    return False


def _user_signal(obj: dict[str, Any]) -> tuple[str, str, str, tuple[str, ...], str] | None:
    top, payload, ptype, item, itype = _types(obj)
    origin: dict[str, Any] | None = None
    kind = ''
    if top == 'event_msg' and ptype == 'item_completed' and itype == 'usermessage':
        origin, kind = item, 'item_completed/UserMessage'
    elif ptype == 'user_message':
        origin, kind = payload, 'event/user_message'
    elif item and str(item.get('role') or '').casefold() == 'user' and itype in ('message', 'user_message'):
        origin, kind = item, 'response_item/message'
    elif str(payload.get('role') or '').casefold() == 'user':
        origin, kind = payload, 'response_item/message' if top == 'response_item' else 'generic/user'
    elif str(obj.get('role') or '').casefold() == 'user':
        origin, kind = obj, 'generic/user'
    if origin is None:
        return None
    content = next((origin[key] for key in ('message', 'text', 'content') if origin.get(key) is not None), None)
    parts = _parts(content)
    # Legacy user-message events keep attachments beside their text field.
    attachments = (('images', 'input_image', 'image_url'),
                   ('file_ids', 'input_image', 'file_id'),
                   ('local_images', 'local_image', 'path'),
                   ('audio', 'input_audio', 'audio_url'),
                   ('audio_references', 'input_audio', None),
                   ('local_audio', 'local_audio', 'path'))
    for key, media_type, reference_key in attachments:
        if origin.get(key):
            values = origin[key] if isinstance(origin[key], list) else [origin[key]]
            for media_value in values:
                body = ({reference_key: media_value} if reference_key else
                        media_value if isinstance(media_value, dict) else {'audio_url': media_value})
                marker = _parts({'type': media_type, **body})
                # An older audio_references list can repeat inline audio URLs.
                for part in marker:
                    if part not in parts:
                        parts.append(part)
    text = '\n'.join(parts).strip()
    if not text:
        return None
    ids = tuple(dict.fromkeys(str(value) for entry in (origin, payload, obj)
                              for key in ('id', 'item_id', 'message_id', 'event_id', 'client_id')
                              if (value := entry.get(key))))
    native_meta = origin.get('internal_chat_message_metadata_passthrough')
    native_meta = native_meta if isinstance(native_meta, dict) else {}
    turn_id = next((str(entry['turn_id']) for entry in (origin, payload, obj, native_meta) if entry.get('turn_id')), '')
    timestamp = _timestamp(obj, payload, origin)
    if not timestamp and type(native_meta.get('create_time')) in (int, float):
        try:
            timestamp = datetime.fromtimestamp(native_meta['create_time'], timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return text, kind, turn_id, ids, timestamp


def native_user_text(obj: dict[str, Any]) -> str:
    """Full user text plus hashed media markers; raw media remains in JSONL.

    Renderers can use this as a fallback for otherwise invisible image/audio-only
    user messages. It does not transcribe audio or describe image contents.
    """
    signal = _user_signal(obj)
    return signal[0] if signal is not None else ''


def _history(obj: dict[str, Any], line: int) -> HistoryEvent | None:
    top, payload, ptype, item, itype = _types(obj)
    replacement = any('replacement_history' in value for value in (obj, payload, item))
    if replacement or top == 'compacted' or 'contextcompaction' in (ptype, itype) or ptype in ('thread_rolled_back', 'context_compacted'):
        return HistoryEvent(line, 'replacement_history' if replacement else ptype or itype or top,
                            replacement or top == 'compacted' or 'contextcompaction' in (ptype, itype))
    return None


def _substantive(obj: dict[str, Any]) -> bool:
    top, payload, ptype, item, itype = _types(obj)
    if top in ('session_meta', 'turn_context', 'token_usage_record', 'compacted', 'world_state'):
        return False
    if ptype == 'item_started' and itype == 'usermessage':
        return False
    if ptype in ('token_count', 'task_started', 'task_complete', 'turn_started', 'turn_complete'):
        return False
    if str(payload.get('role') or item.get('role') or obj.get('role') or '').casefold() in ('system', 'developer'):
        return False
    return top in ('response_item', 'event_msg') or bool(item) or bool(payload.get('role') or obj.get('role'))


def index_turns(source: Path | SourceSnapshot, *, progress: Any = None) -> TurnIndex:
    """Index actual user messages with one bounded-memory pass over a snapshot.

    Memory scales with turn count and the largest individual JSONL record, not
    total conversation text. Duplicate surface records are joined only by an
    identical stable item ID, or identical cross-schema prompt text with no
    intervening assistant/tool activity. Repeated steering prompts stay distinct.
    """
    path = source.path if isinstance(source, SourceSnapshot) else Path(source)
    before = _signature(path)
    turns: list[TurnAnchor] = []
    contexts: list[SourceRange] = []
    histories: list[HistoryEvent] = []
    injected: list[int] = []
    repairs: list[int] = []
    metadata = None
    digest = hashlib.sha256()
    ids_seen: dict[str, int] = {}
    active_kinds: set[str] = set()
    activity = False
    pending_turn_id = ''
    offset = total_lines = reported = 0
    with _stage(progress, 'Index user turns', before[2]) as task, path.open('rb') as stream:
        for line, raw in _bounded_lines(stream, before[2]):
            total_lines = line
            start, offset = offset, offset + len(raw)
            digest.update(raw)
            obj, repaired = _parse(raw, line)
            if repaired:
                repairs.append(line)
            if obj is None:
                continue
            top, payload, ptype, item, _ = _types(obj)
            ref = SourceRange(line, line, start, offset, 'context')
            if top == 'session_meta' and metadata is None:
                metadata = ref
            if top == 'turn_context':
                contexts.append(ref)
                pending_turn_id = str(payload.get('turn_id') or payload.get('id') or '')
                if (turns and not activity and pending_turn_id
                        and (not turns[-1].turn_id or turns[-1].turn_id == pending_turn_id)):
                    turns[-1] = replace(turns[-1], turn_id=pending_turn_id)
                    pending_turn_id = ''
            elif ptype in ('task_started', 'turn_started'):
                pending_turn_id = str(payload.get('turn_id') or '')
            history = _history(obj, line)
            if history:
                histories.append(history)
            signal = _user_signal(obj)
            if signal is not None:
                text, kind, explicit_turn_id, item_ids, timestamp = signal
                if kind in ('response_item/message', 'generic/user') and is_injected_context(text):
                    injected.append(line)
                    continue
                text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                duplicate = next((ids_seen[item_id] for item_id in item_ids
                                  if item_id in ids_seen and turns[ids_seen[item_id]].text_sha256 == text_hash
                                  and (not explicit_turn_id or not turns[ids_seen[item_id]].turn_id
                                       or explicit_turn_id == turns[ids_seen[item_id]].turn_id)), None)
                if (duplicate is None and turns and not activity and kind not in active_kinds
                        and turns[-1].text_sha256 == text_hash
                        and (not explicit_turn_id or not turns[-1].turn_id or explicit_turn_id == turns[-1].turn_id)):
                    duplicate = len(turns) - 1
                if duplicate is not None:
                    previous = turns[duplicate]
                    turns[duplicate] = replace(previous,
                        item_ids=tuple(dict.fromkeys((*previous.item_ids, *item_ids))),
                        turn_id=explicit_turn_id or previous.turn_id,
                        timestamp=previous.timestamp or timestamp,
                        duplicate_lines=(*previous.duplicate_lines, line))
                    if duplicate == len(turns) - 1:
                        active_kinds.add(kind)
                else:
                    turns.append(TurnAnchor(len(turns) + 1, line, start, offset, timestamp,
                                            explicit_turn_id or pending_turn_id, item_ids,
                                            _preview(text), text_hash, kind))
                    active_kinds = {kind}
                    activity = False
                    pending_turn_id = ''
                    duplicate = len(turns) - 1
                for item_id in item_ids:
                    ids_seen[item_id] = duplicate
            elif _substantive(obj):
                activity = True
                pending_turn_id = ''
            if offset - reported >= CHUNK_BYTES:
                task.update(offset, detail=f'{len(turns):,} user turns; {line:,} source lines')
                reported = offset
        task.update(offset, detail=f'{len(turns):,} user turns; {total_lines:,} source lines')
    sha = digest.hexdigest()
    if _signature(path) != before:
        raise SelectionError('Snapshot changed while indexing user turns; no selection was produced.')
    if isinstance(source, SourceSnapshot) and (source.size_bytes != offset or source.sha256 != sha):
        raise SelectionError('Snapshot checksum does not match its capture metadata; no selection was produced.')
    return TurnIndex(path, offset, total_lines, sha, tuple(turns), metadata, tuple(contexts),
                     tuple(histories), tuple(injected), tuple(repairs), before)


def turn_rows(index: TurnIndex) -> list[dict[str, Any]]:
    """Stable, JSON-serializable rows for --list-turns and browser selection."""
    return [{'turn': turn.number, 'timestamp': turn.timestamp, 'turn_id': turn.turn_id,
             'item_ids': list(turn.item_ids), 'line': turn.line, 'byte_start': turn.byte_start,
             'preview': turn.preview, 'text_sha256': turn.text_sha256,
             'duplicate_lines': list(turn.duplicate_lines)} for turn in index.turns]


def _anchor_text(index: TurnIndex, turn: TurnAnchor, stream) -> str:
    stream.seek(turn.byte_start)
    obj, _ = _parse(stream.read(turn.byte_end - turn.byte_start), turn.line)
    signal = _user_signal(obj) if obj is not None else None
    if signal is None or hashlib.sha256(signal[0].encode('utf-8')).hexdigest() != turn.text_sha256:
        raise SelectionError('Indexed prompt bytes changed; rebuild the snapshot before selecting a turn.')
    return signal[0]


def parse_since_time(value: str) -> datetime:
    """Clock timestamps require an offset; a date means local midnight."""
    value = value.strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return datetime.combine(date.fromisoformat(value), time.min).astimezone()
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith(('Z', 'z')) else value)
    except ValueError as exc:
        raise SelectionError('Use an ISO date (YYYY-MM-DD) or timestamp with a UTC offset, such as 2026-01-01T09:30:00-08:00.') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SelectionError('A --since time with a clock must include Z or an explicit UTC offset; a date alone means local midnight.')
    return parsed


def _record_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith(('Z', 'z')) else value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def resolve_since(index: TurnIndex, selector: str, *, occurrence: int | None = None) -> TurnAnchor:
    """Resolve turn:N, id:ID, text:WORDS, time:ISO, or an unprefixed text query.

    Text matches the complete prompt, with Unicode case folding and normalized
    whitespace. An unprefixed integer is text, not an implicit turn number.
    Multiple matches require an explicit 1-based occurrence or another selector.
    """
    if _signature(index.path) != index.signature:
        raise SelectionError('Snapshot changed after indexing; rebuild the index before selecting a turn.')
    if occurrence is not None and (type(occurrence) is not int or occurrence < 1):
        raise SelectionError('--since-occurrence must be a positive 1-based integer.')
    selector = str(selector).strip()
    if not selector:
        raise SelectionError('The --since selector must not be empty.')
    prefix, separator, body = selector.partition(':')
    kind = prefix.casefold() if separator and prefix.casefold() in ('turn', 'id', 'text', 'time') else 'text'
    query = body.strip() if separator and kind == prefix.casefold() else selector
    if not query:
        raise SelectionError(f'The {kind}: selector requires a value.')
    if kind == 'turn':
        if not query.isdecimal() or not 1 <= int(query) <= len(index.turns):
            raise SelectionError(f'User turn must be between 1 and {len(index.turns):,}.')
        matches = [index.turns[int(query) - 1]]
    elif kind == 'id':
        matches = [turn for turn in index.turns if query in (turn.turn_id, *turn.item_ids)]
        if not matches:
            matches = [turn for turn in index.turns if any(value.startswith(query) for value in (turn.turn_id, *turn.item_ids) if value)]
    elif kind == 'time':
        threshold = parse_since_time(query).astimezone(timezone.utc)
        matches = [turn for turn in index.turns if (stamp := _record_time(turn.timestamp)) is not None and stamp >= threshold][:1]
    else:
        needle = _normalize(query)
        with index.path.open('rb') as stream:
            matches = [turn for turn in index.turns if needle in _normalize(_anchor_text(index, turn, stream))]
    if not matches:
        raise SelectionError(f'No user turn matches {_preview(selector, 160)!r}; use --list-turns to inspect available anchors.')
    if occurrence is not None:
        if occurrence > len(matches):
            raise SelectionError(f'Occurrence {occurrence:,} exceeds {len(matches):,} matching user turns.', candidates=matches)
        return matches[occurrence - 1]
    if len(matches) > 1:
        raise SelectionError(f'Selector matches {len(matches):,} user turns. Use turn:N, an exact ID, or --since-occurrence N.', candidates=matches)
    return matches[0]


def _merge_ranges(ranges: Iterable[SourceRange]) -> list[SourceRange]:
    result: list[SourceRange] = []
    for current in sorted(ranges, key=lambda value: (value.byte_start, value.kind == 'context')):
        if result and current.byte_start < result[-1].byte_end:
            if current.byte_end <= result[-1].byte_end:
                continue
            raise SelectionError('Overlapping source ranges cannot be copied without duplicating records.')
        if (result and result[-1].kind == current.kind and result[-1].byte_end == current.byte_start
                and result[-1].line_end + 1 == current.line_start):
            result[-1] = replace(result[-1], line_end=current.line_end, byte_end=current.byte_end)
        else:
            result.append(current)
    return result


def _with_context(ranges: list[SourceRange], metadata: SourceRange | None,
                  contexts: Iterable[SourceRange]) -> list[SourceRange]:
    if not ranges:
        return []
    extra = []
    if metadata is not None and metadata.line_start < ranges[0].line_start:
        extra.append(metadata)
    contexts = tuple(contexts)
    context_lines = [value.line_start for value in contexts]
    selected_starts = [value.byte_start for value in ranges]
    for selected in ranges:
        prior_index = bisect_left(context_lines, selected.line_start) - 1
        if prior_index < 0:
            continue
        previous = contexts[prior_index]
        covering = bisect_right(selected_starts, previous.byte_start) - 1
        if covering < 0 or previous.byte_start >= ranges[covering].byte_end:
            extra.append(previous)
    return _merge_ranges([*ranges, *extra])


def _copy_excerpt(snapshot: SourceSnapshot, ranges: list[SourceRange], *, selection: dict[str, Any],
                  history_events: Iterable[HistoryEvent] = (), progress: Any = None) -> SourceSnapshot:
    if not snapshot.owns_staged_path():
        raise SelectionError('Native excerpts require an owned verified snapshot, not the live rollout.')
    before = _signature(snapshot.path)
    if before[2] != snapshot.size_bytes:
        raise SelectionError('Snapshot size changed before excerpt creation.')
    ranges = _merge_ranges(ranges)
    if not ranges or not any(value.kind == 'selected' for value in ranges):
        raise SelectionError('The selection has no native JSONL source records.')
    total = sum(value.byte_end - value.byte_start for value in ranges)
    descriptor, name = tempfile.mkstemp(prefix='selected-', suffix='.jsonl', dir=snapshot._owned_directory)
    target = Path(name)
    digest = hashlib.sha256()
    copied = lines = 0
    provenance = []
    try:
        with os.fdopen(descriptor, 'wb') as output, snapshot.path.open('rb') as source, _stage(progress, 'Extract selected JSONL', total) as task:
            for span in ranges:
                source.seek(span.byte_start)
                remaining = span.byte_end - span.byte_start
                start = copied
                span_digest = hashlib.sha256()
                while remaining:
                    block = source.read(min(CHUNK_BYTES, remaining))
                    if not block:
                        raise SelectionError('Snapshot was truncated while copying selected JSONL records.')
                    output.write(block)
                    digest.update(block)
                    span_digest.update(block)
                    copied += len(block)
                    remaining -= len(block)
                    task.update(copied)
                count = span.line_end - span.line_start + 1
                provenance.append({'kind': span.kind,
                    'source_line_start': span.line_start, 'source_line_end': span.line_end,
                    'source_byte_start': span.byte_start, 'source_byte_end': span.byte_end,
                    'excerpt_line_start': lines + 1, 'excerpt_line_end': lines + count,
                    'excerpt_byte_start': start, 'excerpt_byte_end': copied,
                    'sha256': span_digest.hexdigest()})
                lines += count
            output.flush()
            os.fsync(output.fileno())
        if _signature(snapshot.path) != before:
            raise SelectionError('Snapshot changed while creating its excerpt; no selection was published.')
        range_starts = [span.line_start for span in ranges]
        histories = [event for event in history_events
                     if (position := bisect_right(range_starts, event.line) - 1) >= 0
                     and event.line <= ranges[position].line_end]
        selection = {**selection,
            'policy': 'byte_exact_native_records',
            'line_numbers': '1-based inclusive; byte offsets are 0-based half-open',
            'original_snapshot': dict(snapshot.metadata),
            'input_snapshot_sha256': snapshot.sha256,
            'input_snapshot_size_bytes': snapshot.size_bytes,
            'sha256': digest.hexdigest(), 'size_bytes': copied, 'line_count': lines,
            'ranges': provenance,
            'context_lines': [line for span in ranges if span.kind == 'context'
                              for line in range(span.line_start, span.line_end + 1)],
            'embedded_history_record_count': sum(event.embedded_history for event in histories),
            'history_events': [{'line': event.line, 'kind': event.kind, 'embedded_history': event.embedded_history} for event in histories],
            'history_policy': 'Retain every selected native record, including post-anchor compaction or rollback events. Embedded replacement history may quote older content; it is not rewritten or silently omitted.',
        }
        metadata = {**snapshot.metadata, 'policy': 'selected_records_from_verified_snapshot',
                    'captured_size_bytes': copied, 'captured_sha256': digest.hexdigest(),
                    'selection': selection}
        return replace(snapshot, path=target, sha256=digest.hexdigest(), size_bytes=copied, metadata=metadata)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def excerpt_turns(snapshot: SourceSnapshot, start_turn: int | TurnAnchor, stop_turn: int | None = None,
                  *, index: TurnIndex | None = None, include_context: bool = True,
                  progress: Any = None) -> SourceSnapshot:
    """Copy the inclusive user-turn interval through stop_turn, or captured EOF."""
    index = index if index is not None else index_turns(snapshot, progress=progress)
    if (index.path.resolve() != snapshot.path.resolve() or index.sha256 != snapshot.sha256
            or index.size_bytes != snapshot.size_bytes or index.signature != _signature(snapshot.path)):
        raise SelectionError('Turn index belongs to a different or changed snapshot.')
    start_number = start_turn.number if isinstance(start_turn, TurnAnchor) else start_turn
    last = len(index.turns) if stop_turn is None else stop_turn
    if (type(start_number) is not int or type(last) is not int
            or not 1 <= start_number <= last <= len(index.turns)):
        raise SelectionError(f'Select an inclusive user-turn range within 1..{len(index.turns):,}.')
    first = index.turns[start_number - 1]
    if isinstance(start_turn, TurnAnchor) and start_turn != first:
        raise SelectionError('The selected anchor belongs to a different turn index.')
    next_turn = index.turns[last] if last < len(index.turns) else None
    ranges = [SourceRange(first.line, next_turn.line - 1 if next_turn else index.total_lines,
                          first.byte_start, next_turn.byte_start if next_turn else index.size_bytes)]
    if include_context:
        ranges = _with_context(ranges, index.session_meta, index.turn_contexts)
    return _copy_excerpt(snapshot, ranges, selection={
        'mode': 'turn_interval', 'start_turn': start_number, 'stop_turn': last,
        'source_turn_count': len(index.turns), 'selected_turn_count': last - start_number + 1,
        'start_turn_id': first.turn_id, 'start_timestamp': first.timestamp,
        'start_prompt_sha256': first.text_sha256,
        'repaired_json_lines': [line for line in index.repaired_json_lines if any(span.line_start <= line <= span.line_end for span in ranges)],
    }, history_events=index.history_events, progress=progress)


def excerpt_records(snapshot: SourceSnapshot, records: Iterable[Any], *, include_context: bool = True,
                    progress: Any = None) -> SourceSnapshot:
    """Copy precisely the native lines backing selected rendered records.

    Merged records must expose ``source_seqs`` with every contributing original
    line. Synthetic display summaries may have no source; reconstructed live
    context (negative source numbers) cannot be represented as a native excerpt.
    """
    selected: set[int] = set()
    for record in records:
        source_seqs = tuple(getattr(record, 'source_seqs', ()) or ())
        if not source_seqs:
            seq = int(getattr(record, 'seq', 0))
            source_seqs = (seq,) if seq else ()
        if any(seq < 0 for seq in source_seqs):
            raise SelectionError('Reconstructed --live-context records cannot produce a byte-exact native JSONL excerpt. Export chronological records instead.')
        selected.update(seq for seq in source_seqs if seq > 0)
    if not selected:
        raise SelectionError('The selected rendered records have no native source-line provenance.')
    before = _signature(snapshot.path)
    ranges: list[SourceRange] = []
    contexts: list[SourceRange] = []
    histories: list[HistoryEvent] = []
    metadata = None
    offset = reported = 0
    found: set[int] = set()
    with snapshot.path.open('rb') as source, _stage(progress, 'Map selected source records', snapshot.size_bytes) as task:
        for line, raw in _bounded_lines(source, before[2]):
            start, offset = offset, offset + len(raw)
            obj, _ = _parse(raw, line)
            if obj is not None:
                if str(obj.get('type') or '').casefold() == 'session_meta' and metadata is None:
                    metadata = SourceRange(line, line, start, offset, 'context')
                elif str(obj.get('type') or '').casefold() == 'turn_context':
                    contexts.append(SourceRange(line, line, start, offset, 'context'))
                history = _history(obj, line)
                if history is not None:
                    histories.append(history)
            if line in selected:
                found.add(line)
                ranges.append(SourceRange(line, line, start, offset))
            if offset - reported >= CHUNK_BYTES:
                task.update(offset, detail=f'{len(found):,}/{len(selected):,} selected source lines located')
                reported = offset
        task.update(offset)
    if found != selected:
        raise SelectionError(f'{len(selected - found):,} selected source lines do not exist in this snapshot.')
    if _signature(snapshot.path) != before:
        raise SelectionError('Snapshot changed while mapping selected source records.')
    ranges = _merge_ranges(ranges)
    if include_context:
        ranges = _with_context(ranges, metadata, contexts)
    return _copy_excerpt(snapshot, ranges, selection={
        'mode': 'rendered_record_lines', 'selected_source_line_count': len(selected),
    }, history_events=histories, progress=progress)
