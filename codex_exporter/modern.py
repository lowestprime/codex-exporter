"""Bounded-memory Codex session discovery and chronological Markdown export.

Never writes app-owned state. Local Codex rollouts are not the same as cloud ChatGPT/Work history.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def clean_path(value: str) -> str:
    """Normalize separators, trailing slashes, and Windows long-path prefixes for matching."""
    value = str(value or '').strip().replace('/', '\\')
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return value.rstrip('\\').casefold()


def _catalog_db(home: Path) -> Path | None:
    # Do not merge stale shadow copies with the authoritative root catalog.
    for candidate in (home / 'state_5.sqlite', home / 'state.sqlite', home / 'sqlite' / 'state_5.sqlite'):
        if candidate.is_file():
            return candidate
    return None


def read_catalog(home: Path) -> dict[str, dict[str, Any]]:
    """SQLite catalog is metadata, not evidence that a rollout still exists."""
    db = _catalog_db(home)
    if not db:
        return {}
    result: dict[str, dict[str, Any]] = {}
    # URI read-only + query_only. WAL visibility is retained; never use immutable=1 on live state.
    import urllib.parse
    uri = db.resolve().as_uri() + '?mode=ro'
    try:
        with sqlite3.connect(uri, uri=True, timeout=3) as cx:
            cx.execute('PRAGMA query_only=ON')
            tables = {row[0] for row in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'threads' not in tables:
                return {}
            columns = {row[1] for row in cx.execute('PRAGMA table_info("threads")')}
            keys = ('id', 'cwd', 'title', 'archived', 'rollout_path', 'updated_at', 'created_at', 'project_id')
            names = [x for x in keys if x in columns]
            if 'id' not in names:
                return {}
            sql = 'SELECT ' + ', '.join('"' + x + '"' for x in names) + ' FROM "threads"'
            for values in cx.execute(sql):
                item = dict(zip(names, values))
                sid = str(item.get('id') or '').lower()
                if UUID.fullmatch(sid):
                    result[sid] = item
    except (sqlite3.Error, OSError) as exc:
        print(f'WARNING: Codex SQLite catalog unavailable ({type(exc).__name__}: {exc})', file=sys.stderr)
    return result


def discover(home: Path) -> list[dict[str, Any]]:
    """Reconcile live/archived rollouts and SQLite rows, reporting missing payloads."""
    catalog = read_catalog(home)
    found: dict[str, dict[str, Any]] = {}
    for folder in (home / 'sessions', home / 'archived_sessions'):
        if not folder.is_dir():
            continue
        for path in list(folder.rglob('*.jsonl')) + list(folder.rglob('*.jsonl.zst')):
            match = UUID.search(path.name)
            if not match:
                continue
            sid = match.group(0).lower()
            try:
                stat = path.stat()
            except OSError:
                continue
            old = found.get(sid)
            # Canonical plain JSONL takes priority if both representations exist.
            if old is None or (not path.name.endswith('.zst') and old['path'].name.endswith('.zst')) or (path.name.endswith('.zst') == old['path'].name.endswith('.zst') and stat.st_mtime > old['mtime']):
                found[sid] = {'session_id': sid, 'path': path, 'size': stat.st_size, 'mtime': stat.st_mtime,
                              'archived': folder.name == 'archived_sessions', 'source': 'filesystem'}
    for sid, row in catalog.items():
        item = found.get(sid)
        raw_path = str(row.get('rollout_path') or '')
        if item is None and raw_path:
            # Allow catalog paths outside the two conventional session roots.
            candidate = Path(raw_path[4:] if raw_path.startswith('\\\\?\\') else raw_path)
            # Codex cold-storage compression can retain only the .jsonl.zst sibling.
            if not candidate.is_file() and candidate.name.endswith('.jsonl'):
                candidate = Path(str(candidate) + '.zst')
            if candidate.is_file():
                try:
                    stat = candidate.stat()
                    item = {'session_id': sid, 'path': candidate, 'size': stat.st_size,
                            'mtime': stat.st_mtime, 'archived': bool(row.get('archived')),
                            'source': 'sqlite_path'}
                    found[sid] = item
                except OSError:
                    pass
        if item is None:
            item = {'session_id': sid, 'path': None, 'size': 0, 'mtime': 0.0,
                    'archived': bool(row.get('archived')), 'source': 'sqlite_missing_rollout'}
            found[sid] = item
        item.update(cwd=str(row.get('cwd') or ''), title=str(row.get('title') or ''),
                    project_id=str(row.get('project_id') or ''),
                    archived=bool(row.get('archived', item['archived'])), catalog=True,
                    db_rollout_path=raw_path)
    # Latest deliberate rename in append-only session_index.jsonl overrides the
    # older, sometimes stale threads.title. This does not modify app-owned state.
    index_titles: dict[str, str] = {}
    for index_path in (home / 'session_index.jsonl', home / 'sessions' / 'session_index.jsonl'):
        if not index_path.is_file():
            continue
        try:
            with index_path.open(encoding='utf-8', errors='replace') as index_stream:
                for line in index_stream:
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    sid = str(entry.get('id') or entry.get('session_id') or entry.get('thread_id') or '').lower()
                    title = next((entry[k].strip() for k in ('thread_name', 'thread_title', 'title', 'name')
                        if isinstance(entry.get(k), str) and entry[k].strip()), '')
                    if UUID.fullmatch(sid) and title:
                        index_titles[sid] = title
        except OSError:
            pass
    for sid, title in index_titles.items():
        if sid in found:
            found[sid]['title'] = title
            found[sid]['title_source'] = 'session_index'
    for item in found.values():
        item.setdefault('title_source', 'sqlite' if item.get('catalog') else 'source')
        item.setdefault('cwd', '')
        item.setdefault('title', '')
        item.setdefault('catalog', False)
        item['project'] = clean_path(item['cwd']).split('\\')[-1] if item['cwd'] else ''
        item['missing_rollout'] = item['path'] is None
        item['compression'] = 'zstandard' if item['path'] is not None and item['path'].name.endswith('.jsonl.zst') else 'none'
    return sorted(found.values(), key=lambda row: row['mtime'], reverse=True)


def choose_project(rows: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    target_norm = clean_path(target)
    is_path = '\\' in target_norm or ':' in target_norm
    result = []
    for row in rows:
        cwd = clean_path(row.get('cwd', ''))
        project = clean_path(row.get('project', ''))
        if (cwd == target_norm if is_path else (project == target_norm or str(row.get('project_id', '')).casefold() == target_norm)):
            result.append(row)
    return result


def audit(home: Path, project: str = '') -> dict[str, Any]:
    rows = discover(home)
    if project:
        rows = choose_project(rows, project)
    return {'codex_home': str(home), 'project': project or None, 'indexed_threads': len(rows),
            'available_rollouts': sum(not r['missing_rollout'] for r in rows),
            'compressed_rollouts': sum(r.get('compression') == 'zstandard' for r in rows),
            'missing_rollouts': [r['session_id'] for r in rows if r['missing_rollout']],
            'unindexed_rollouts': [r['session_id'] for r in rows if not r['catalog']],
            'threads': [{k: (str(v) if isinstance(v, Path) else v) for k, v in r.items()}
                        for r in rows]}


def _progress(done: int, total: int, begin: float, *, force: bool = False) -> None:
    if not force and not sys.stderr.isatty():
        return
    elapsed = max(time.monotonic() - begin, .001)
    pct = 100.0 * done / total if total else 100.0
    msg = f'\rCodex stream: {pct:5.1f}%  {done / 1048576:,.1f}/{total / 1048576:,.1f} MiB  {done / elapsed / 1048576:,.1f} MiB/s'
    print(msg, end='\n' if done >= total else '', file=sys.stderr, flush=True)


def stream_export(source: Path, destination: Path, *, session_id: str, title: str, title_source: str = "",
                  include_reasoning_summaries: bool = False, tool_outputs: str = 'full',
                  max_tool_chars: int = 20_000, strip: bool = False,
                  max_line_chars: int = 20_000, max_repeated_lines: int = 25,
                  collision: str = 'rename', progress: bool = True, redact: bool = False, strict_events: bool = False,
                  source_truncation: str = 'annotate', unknown_events: str = 'report') -> dict[str, Any]:
    """Bounded-memory one-pass export; stable readable headings, provenance and audit.

    The original event JSONL remains the fidelity authority. No assembled giant
    string, full source read_bytes(), whole-thread tokenization, or source write.
    """
    from . import cli
    if unknown_events not in {'report', 'sidecar'}:
        raise ValueError('unknown_events must be report or sidecar')
    if source_truncation not in {'annotate', 'preserve', 'error'}:
        raise ValueError('Unsupported truncation policy')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Avoid truncating a prior export or colliding with another batch member.
    if destination.exists():
        if collision == 'error':
            raise FileExistsError(destination)
        if collision == 'skip':
            return {'file': str(destination), 'skipped': True, 'session_id': session_id}
        if collision == 'rename':
            i = 1
            while destination.exists():
                destination = destination.with_name(f'{destination.stem}-{i}{destination.suffix}')
                i += 1
        elif collision != 'overwrite':
            raise ValueError(f'Invalid collision policy {collision}')
    # Staged output: a parser failure never overwrites a completed archive.
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix='.codex-export-', suffix='.partial', dir=destination.parent)
    os.close(fd)
    temp = Path(tmp)
    sidecar = destination.with_suffix('.unknown.jsonl') if unknown_events == 'sidecar' else None
    staged_unknown = None
    sha = hashlib.sha256()
    total = source.stat().st_size
    is_zstd = source.name.lower().endswith('.jsonl.zst')
    decoded_size = 0
    counts: Counter[str] = Counter()
    marker_locations: list[dict[str, int]] = []
    first = time.monotonic()
    last_progress = first
    last_key: tuple[str, str, str] | None = None
    upstream_truncations: list[dict[str, Any]] = []
    error_samples: list[dict[str, Any]] = []
    # Protect original user data by explicitly preserving selected policy state.
    old_policy = cli._SOURCE_TRUNCATION_POLICY
    cli._SOURCE_TRUNCATION_POLICY = source_truncation
    try:
        with ExitStack() as stack:
            physical = stack.enter_context(source.open('rb', buffering=1024 * 1024))
            if is_zstd:
                try:
                    import zstandard as zstd
                except ImportError as exc:
                    raise RuntimeError('Compressed Codex rollouts require: py -3.13 -m pip install zstandard') from exc
                decompressed = stack.enter_context(zstd.ZstdDecompressor().stream_reader(physical, read_across_frames=True, closefd=False))
                src = stack.enter_context(io.BufferedReader(decompressed, buffer_size=1024 * 1024))
            else:
                src = physical
            output = stack.enter_context(temp.open('w', encoding='utf-8', newline='\n'))
            if sidecar:
                unk_fd, unk_path = tempfile.mkstemp(prefix='.codex-unknown-', suffix='.partial', dir=destination.parent)
                os.close(unk_fd)
                staged_unknown = Path(unk_path)
            unknown = staged_unknown.open('wb') if staged_unknown else None
            try:
                from .cli import yaml_quote
                created = datetime.now().astimezone().isoformat(timespec='seconds')
                output.write('---\n' + f'title: {yaml_quote(title)}\n'
                             + f'session_id: {yaml_quote(session_id)}\n'
                             + f'source_jsonl: {yaml_quote(str(source))}\n'
                             + f'title_source: {yaml_quote(title_source or "unavailable")}\n'
                             + f'exported_at: {yaml_quote(created)}\n'
                             + 'mode: "thread"\n'
                             + 'history_semantics: "chronological_source_history"\n'
                             + f'source_encoding: {yaml_quote("zstandard" if is_zstd else "plain")}\n'
                             + 'token_count_method: "not_computed_streaming"\n---\n\n')
                output.write(f'# {title}\n\n')
                output.write('> Local chronological rollout export. The original JSONL remains the authoritative source. '
                             'This streaming layout cannot provide exact output token counts or the classic turn-level map.\n\n')
                output.write('## Thread export map\n\n'
                             '| Metric | Value |\n|---|---|\n'
                             + f'| Thread title | {title.replace(chr(124), chr(92)+chr(124))} |\n'
                             + f'| Session ID | `{session_id}` |\n'
                             + f'| Source encoding | {"Zstandard" if is_zstd else "plain JSONL"} |\n'
                             + '| Structure | Chronological source events |\n'
                             + '| Token counts | Not computed (streaming) |\n\n'
                             + '## Chronological records\n\n')
                line_no = 0
                while True:
                    # Plain sources are bounded to their initial size; compressed
                    # sources are decoded incrementally until frame EOF.
                    available = max(0, total - physical.tell())
                    raw = src.readline() if is_zstd else (src.readline(available) if available else b'')
                    if not raw:
                        break
                    decoded_size += len(raw)
                    now = time.monotonic()
                    if progress and now - last_progress >= 1:
                        _progress(min(physical.tell(), total), total, first, force=True)
                        last_progress = now
                    if not raw.endswith(b'\n') and (is_zstd or physical.tell() == total):
                        # A final complete valid JSON object need not end with newline.
                        pass
                    sha.update(raw)
                    line_no += 1
                    counts['source_lines'] += 1
                    if not raw.strip():
                        continue
                    try:
                        line = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        counts['utf8_replacement_lines'] += 1
                        line = raw.decode('utf-8', errors='replace')
                    # Record upstream omissions without reproducing or hiding them.
                    if 'tokens truncated' in line:
                        for hit in cli.SOURCE_TRUNCATION_RE.finditer(line):
                            counts['raw_truncation_markers'] += 1
                            if len(upstream_truncations) < 10000:
                                upstream_truncations.append({'source_line': line_no,
                                    'tokens_reported': int(hit.group('count').replace(',', '')),
                                    'original_marker': hit.group(0)})
                    obj, status = cli.parse_jsonl_line(line)
                    if obj is None:
                        counts['parse_errors'] += 1
                        if len(error_samples) < 100:
                            error_samples.append({'line': line_no, 'sha256': hashlib.sha256(raw).hexdigest(),
                                                  'error': status.get('error', 'invalid JSON')})
                        continue
                    if status.get('status') != 'exact':
                        counts['repaired_lines'] += 1
                    key = cli.event_key(obj)
                    rec = cli.classify_record(obj, seq=line_no, include_actions=True,
                        include_reasoning_summaries=include_reasoning_summaries,
                        tool_outputs=tool_outputs, max_tool_chars=max_tool_chars,
                        strip=strip, max_line_chars=max_line_chars, max_repeated_lines=max_repeated_lines)
                    if rec is None or not rec.text.strip():
                        reason = cli.known_ignored_reason(obj, include_reasoning_summaries)
                        counts['ignored_' + reason] += 1
                        if reason == 'unrecognized_schema':
                            counts['unknown_events'] += 1
                            if unknown:
                                unknown.write(raw)
                        continue
                    # Preserve exact sequential records unless adjacent duplicates are provably identical.
                    current = (rec.kind, rec.role, rec.text)
                    if current == last_key:
                        counts['adjacent_duplicates_retained'] += 1
                    last_key = current
                    counts['rendered_records'] += 1
                    counts['event_' + key] += 1
                    if cli.SOURCE_TRUNCATION_RE.search(rec.text):
                        counts['remaining_source_markers'] += 1
                    if 'SOURCE TOOL OUTPUT TRUNCATED BY CODEX' in rec.text:
                        counts['annotated_source_markers'] += 1
                        if len(marker_locations) < 100000:
                            marker_locations.append({'source_line': line_no, 'rendered_record': counts['rendered_records']})
                    label = (rec.role or rec.kind or 'event').replace('\n', ' ').strip()
                    output.write(f'### Event {line_no} — {label} ({rec.kind})\n\n')
                    if rec.timestamp:
                        output.write(f'Timestamp: `{rec.timestamp}`  \n')
                    output.write(f'Event schema: `{key}`  \nSource JSONL line: `{line_no}`\n\n')
                    output.write(cli.redact(rec.text, redact).rstrip() + '\n\n')
                output.write('\n## Export integrity and truncation notes\n\n')
                ignored_total = sum(value for key, value in counts.items() if key.startswith('ignored_'))
                output.write('Tool-output truncation occurring *inside Codex* cannot be reversed. '
                             'Rendered content is an interpretation of locally recorded events. '
                             'Unrecognized schemas and unparsable source lines are recorded in the sibling manifest.\n\n'
                             '### Source coverage and integrity\n\n'
                             '| Metric | Recorded value |\n|---|---:|\n'
                             f'| Source JSONL records | {counts["source_lines"]:,} |\n'
                             f'| Rendered chronological records | {counts["rendered_records"]:,} |\n'
                             f'| Classified non-rendered records | {ignored_total:,} |\n'
                             f'| Unrecognized event schemas | {counts["unknown_events"]:,} |\n'
                             f'| JSON parse errors | {counts["parse_errors"]:,} |\n'
                             f'| Source truncation markers | {counts["raw_truncation_markers"]:,} |\n'
                             f'| Decoded source bytes | {decoded_size:,} |\n'
                             f'| Decoded source SHA-256 | `{sha.hexdigest()}` |\n\n'
                             'Counts describe recorded events, not unique conversation turns. '
                             'This audit is derived from the original source, not a guarantee of semantic completeness.\n')
                output.flush()
                os.fsync(output.fileno())
            finally:
                if unknown:
                    unknown.close()
        if strict_events and counts['unknown_events']:
            raise RuntimeError(f"Strict event audit failed: {counts['unknown_events']} unknown event schemas")
        digest = sha.hexdigest()
        manifest = {'exporter': f'codex-export {cli.__version__}', 'mode': 'stream_thread',
                    'session_id': session_id, 'title': title, 'title_source': title_source or 'unavailable', 'source': str(source),
                    'source_sha256': digest, 'source_size_bytes_at_open': total,
                    'source_size_bytes_at_end': source.stat().st_size,
                    'source_encoding': 'zstandard' if is_zstd else 'plain',
                    'decoded_source_bytes': decoded_size, 'records': dict(counts),
                    'parse_error_samples': error_samples, 'truncation_marker_locations': marker_locations,
                    'upstream_truncations': upstream_truncations,
                    'source_truncation_policy': source_truncation, 'redacted': redact,
                    'unknown_events_policy': unknown_events, 'unknown_sidecar': str(sidecar) if sidecar else None,
                    'text_fidelity': 'rendered_text_only; original JSONL remains authoritative',
                    'partial_final_line_possible': counts['parse_errors'] > 0}
        if source.stat().st_size != total:
            manifest['source_changed_during_export'] = True
        os.replace(temp, destination)
        if staged_unknown:
            os.replace(staged_unknown, sidecar)
        manifest_path = destination.with_suffix('.manifest.json')
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        if progress:
            _progress(total, total, first, force=True)
        return {'file': str(destination), 'manifest': str(manifest_path), 'session_id': session_id,
                'source_jsonl': str(source), 'source_sha256': digest,
                'source_lines': counts['source_lines'], 'rendered_records': counts['rendered_records'],
                'parse_error_count': counts['parse_errors'], 'unknown_event_count': counts['unknown_events'],
                'source_changed_during_export': bool(manifest.get('source_changed_during_export'))}
    finally:
        cli._SOURCE_TRUNCATION_POLICY = old_policy
        temp.unlink(missing_ok=True)
        if staged_unknown:
            staged_unknown.unlink(missing_ok=True)
