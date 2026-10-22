"""Read user-provided official ChatGPT export archives (including eligible Work chats).

This is NOT an API for retrieving cloud history: only supplied export ZIP/JSON files
are read. Branches, uncommon roles, and non-text asset descriptors are retained.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import hashlib
import sys
import json
import re
import zipfile


def safe_name(value: Any) -> str:
    s = re.sub(r'[^\w.\-]+', '_', str(value or 'chat').strip(), flags=re.U).strip('._')
    return (s[:90] or 'chat')


def _extract_text(value: Any) -> list[str]:
    """Keep all textual leaf values and show honest placeholders for non-text payloads."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [text for member in value for text in _extract_text(member)]
    if isinstance(value, dict):
        t = str(value.get('content_type') or value.get('type') or '')
        if t in {'image_asset_pointer', 'audio_asset_pointer', 'video_frame', 'asset_pointer'}:
            return [f'[Non-text asset: type={t}; reference={str(value.get("asset_pointer") or value.get("id") or "not supplied")}]']
        for keys in (('parts',), ('text', 'markdown', 'output_text'), ('content',), ('result',)):
            output = [part for key in keys if key in value for part in _extract_text(value[key])]
            if output:
                return output
        # Avoid silent loss of unusual content structures, but do not dump opaque b64 media.
        return [f'[Structured non-text content: keys={", ".join(map(str, value.keys()))}]']
    return [str(value)] if value is not None else []


def _date(val: Any) -> str:
    try:
        return datetime.fromtimestamp(float(val), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return str(val or '')


# Official exports may use conversations.json OR numbered conversation shards.
# Selection is limited to conversation JSON files: never process .dat/media or
# extract the user's entire multi-gigabyte archive to disk.
SHARD_RE = re.compile(r"^conversations-(\d{3,})\.json$", re.I)


def _discover_sources(input_path: Path) -> list[tuple[str, Any]]:
    """Return ordered (display-name, source) entries, without reading their payloads."""
    if input_path.is_dir():
        found = [(str(p.relative_to(input_path)).replace('\\', '/'), p)
                 for p in input_path.rglob('*.json')
                 if p.name.lower() == 'conversations.json' or SHARD_RE.fullmatch(p.name)]
    elif input_path.is_file() and input_path.suffix.casefold() == '.zip':
        with zipfile.ZipFile(input_path) as z:
            found = [(i.filename, i.filename) for i in z.infolist()
                     if not i.is_dir() and (i.filename.rsplit('/', 1)[-1].lower() == 'conversations.json'
                     or SHARD_RE.fullmatch(i.filename.rsplit('/', 1)[-1]))]
    elif input_path.is_file() and input_path.suffix.casefold() == '.json':
        found = [(input_path.name, input_path)]
    else:
        raise FileNotFoundError(f'Expected an existing ChatGPT ZIP, directory, or JSON: {input_path}')
    if not found:
        raise FileNotFoundError('No conversations.json or conversations-NNN.json files found in input')
    numbered = [(name, src, int(SHARD_RE.fullmatch(name.rsplit('/', 1)[-1]).group(1)))
                for name, src in found if SHARD_RE.fullmatch(name.rsplit('/', 1)[-1])]
    ordinary = [(name, src) for name, src in found if name.rsplit('/', 1)[-1].lower() == 'conversations.json']
    if numbered and ordinary:
        raise ValueError('Both conversations.json and numbered conversation shards found: select one export explicitly to avoid duplicates')
    if ordinary:
        if len(ordinary) != 1:
            raise ValueError(f'Ambiguous export: {len(ordinary)} conversations.json files; specify a single export directory')
        return ordinary
    if numbered:
        ids = [n for _, _, n in numbered]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate numbered conversation shards detected; use a single export directory')
        # For a complete ZIP or directory, every numbered member is expected.
        if input_path.suffix.casefold() != '.json':
            expected = set(range(0, max(ids) + 1))
            if expected != set(ids):
                gaps = sorted(expected - set(ids))
                raise ValueError(f'Missing numbered conversation shards: {gaps[:30]} (refusing incomplete archive scan)')
        return [(name, src) for name, src, _ in sorted(numbered, key=lambda t: t[2])]
    # Explicit direct path to an arbitrary JSON is allowed, not ZIP/folder.
    if input_path.is_file() and input_path.suffix.casefold() == '.json':
        return found
    raise FileNotFoundError('No recognized ChatGPT conversation JSON files')


@contextmanager
def _open_source(input_path: Path, source: Any):
    if input_path.is_file() and input_path.suffix.casefold() == '.zip':
        with zipfile.ZipFile(input_path) as z:
            info = z.getinfo(source)
            with z.open(info) as f:
                yield f, info.file_size
    else:
        with Path(source).open('rb') as f:
            yield f, Path(source).stat().st_size


def iter_conversation_records(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Stream numbered backup shards independently; memory bounded per conversation.

    Large arrays require ijson; never fall back to loading a whole 90 MiB+
    shard or 12 GiB account export in memory. The caller controls output.
    """
    sources = _discover_sources(path)
    try:
        import ijson  # type: ignore
    except ImportError:
        ijson = None
    for name, source in sources:
        with _open_source(path, source) as (f, size):
            if ijson is not None:
                for conv in ijson.items(f, 'item'):
                    if isinstance(conv, dict):
                        yield name, conv
            else:
                if size > 64 * 1024 * 1024:
                    raise RuntimeError(f'{name} is {size:,} bytes. Install bounded-memory JSON parser: '
                                       'py -3.13 -m pip install "codex-exporter[chatgpt]"')
                data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError(f'{name} does not contain a JSON array of conversations')
                for conv in data:
                    if isinstance(conv, dict):
                        yield name, conv


def iter_conversations(path: Path) -> Iterator[dict[str, Any]]:
    """Backward-compatible conversation-only iterator."""
    for _name, conv in iter_conversation_records(path):
        yield conv


def _matches(conv: dict[str, Any], wanted_ids: set[str], pattern: Any, project_id: str) -> bool:
    sid = str(conv.get('id') or conv.get('conversation_id') or '')
    if wanted_ids and sid.casefold() not in wanted_ids:
        return False
    if pattern and not pattern.search(str(conv.get('title') or sid or '')):
        return False
    if project_id and str(conv.get('project_id') or '') != project_id:
        return False
    return True


def audit_chatgpt(path: Path, *, conversation_ids: list[str] | None = None,
                  title_match: str = '', project_id: str = '', progress: bool = False) -> dict[str, Any]:
    """Read-only inventory; no output export, extraction or mutation."""
    wanted_ids = {x.casefold() for x in (conversation_ids or [])}
    pattern = re.compile(title_match, re.I) if title_match else None
    sources = _discover_sources(path)
    counts: Counter[str] = Counter()
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: list[dict[str, str]] = []
    for name, conv in iter_conversation_records(path):
        counts['scanned'] += 1
        counts['source:' + name] += 1
        sid = str(conv.get('id') or conv.get('conversation_id') or '')
        if sid:
            if sid in seen:
                counts['duplicate_id_records'] += 1
                if len(duplicates) < 30: duplicates.append({'id': sid, 'source': name})
            seen.add(sid)
        if 'project_id' in conv and conv.get('project_id') is not None:
            counts['explicit_project_ids'] += 1
        if _matches(conv, wanted_ids, pattern, project_id):
            counts['matched'] += 1
            if len(matched) < 1000:
                matched.append({'id': sid, 'title': str(conv.get('title') or ''),
                                'project_id': conv.get('project_id'), 'source_member': name,
                                'mapping_nodes': len(conv.get('mapping') or {})})
        if progress and counts['scanned'] % 1000 == 0:
            print(f'ChatGPT backup audit: {counts["scanned"]:,} scanned; {counts["matched"]:,} matched',
                  file=sys.stderr, flush=True)
    return {'input': str(path), 'source_files': [name for name, _ in sources],
            'source_file_count': len(sources), 'conversations_scanned': counts['scanned'],
            'unique_conversation_ids': len(seen), 'duplicate_id_records': counts['duplicate_id_records'],
            'duplicate_id_samples': duplicates, 'explicit_project_id_records': counts['explicit_project_ids'],
            'matched_count': counts['matched'], 'matched_sample_limit': 1000,
            'matched': matched, 'source_counts': {k[7:]: v for k,v in counts.items() if k.startswith('source:')},
            'notes': 'ChatGPT account conversation IDs are not equivalent to local Codex rollout UUIDs; '
                     'absence of a title/ID match does not prove local Codex rollout was erased.'}


def _ordered_nodes(conv: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    mapping = conv.get('mapping')
    if not isinstance(mapping, dict):
        return []
    # Preserve every node including alternate branches. Deterministic parent-before-child ordering.
    roots = [str(k) for k, v in mapping.items() if not isinstance(v, dict) or v.get('parent') not in mapping]
    ordered: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    def visit(start: str) -> None:
        stack = [start]
        while stack:
            key = stack.pop()
            if key in seen or key not in mapping:
                continue
            seen.add(key)
            node = mapping[key]
            if not isinstance(node, dict):
                continue
            ordered.append((key, node))
            # Following declared sibling order preserves the transcript's branch structure.
            children = node.get('children')
            if isinstance(children, list):
                stack.extend(reversed([str(x) for x in children]))
    for key in roots:
        visit(key)
    for key in mapping:
        visit(str(key))
    return ordered


def _choose_output(dest: Path) -> Path:
    index = 0
    candidate = dest
    while candidate.exists():
        index += 1
        candidate = dest.with_name(f'{dest.stem}-{index}{dest.suffix}')
    return candidate


def export_chatgpt(input_path: Path, out_dir: Path, *, conversation_ids: list[str],
                   title_match: str, project_id: str = '', progress: bool = False) -> list[dict[str, Any]]:
    """Convert an explicit account-export archive; never fetch account data remotely."""
    wanted_ids = {x.casefold() for x in conversation_ids}
    pattern = re.compile(title_match, re.I) if title_match else None
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    scanned = 0
    for source_member, conv in iter_conversation_records(input_path):
        scanned += 1
        sid = str(conv.get('id') or conv.get('conversation_id') or '')
        title = str(conv.get('title') or sid or f'Conversation {scanned}')
        if wanted_ids and sid.casefold() not in wanted_ids:
            continue
        if pattern and not pattern.search(title):
            continue
        # Project relation is not part of a guaranteed universal account-export schema.
        if project_id and str(conv.get('project_id') or '') != project_id:
            continue
        filename = f'chatgpt_{safe_name(title)}_{safe_name(sid or scanned)}.md'
        dest = _choose_output(out_dir / filename)
        roles = Counter()
        missing_messages = 0
        nodes = _ordered_nodes(conv)
        with dest.open('w', encoding='utf-8', newline='\n') as f:
            f.write(f'# {title}\n\n')
            f.write('> Converted from a user-supplied ChatGPT account data export; all recorded branches '
                    'are retained. This is not a guarantee that every cloud Work record was included in the supplied export.\n\n')
            f.write(f'- Conversation ID: `{sid}`\n- Created (UTC): `{_date(conv.get("create_time"))}`\n'
                    f'- Updated (UTC): `{_date(conv.get("update_time"))}`\n'
                    f'- Project ID in source: `{conv.get("project_id") or "not provided"}`\n'
                    f'- Source archive: `{input_path}`\n\n')
            for position, (nid, node) in enumerate(nodes, 1):
                msg = node.get('message')
                if not isinstance(msg, dict):
                    missing_messages += 1
                    continue
                author = msg.get('author') or {}
                role = str(author.get('role') or msg.get('role') or 'unknown') if isinstance(author, dict) else str(author)
                roles[role] += 1
                content = msg.get('content')
                parts = _extract_text(content)
                f.write(f'## Node {position}: {role}\n\n')
                f.write(f'- Node ID: `{nid}`\n- Parent: `{node.get("parent") or "root"}`\n'
                        f'- Children: `{", ".join(map(str, node.get("children") or []))}`\n'
                        f'- Timestamp (UTC): `{_date(msg.get("create_time"))}`\n'
                        f'- Channel: `{(msg.get("metadata") or {}).get("channel", "unspecified")}`\n\n')
                if parts:
                    f.write('\n\n'.join(parts).rstrip() + '\n\n')
                else:
                    f.write('_No readable text recorded in this message node._\n\n')
        manifest = {'source_archive': str(input_path), 'source_member': source_member, 'conversation_id': sid, 'title': title,
                    'node_count': len(nodes), 'message_roles': dict(roles),
                    'nonmessage_nodes': missing_messages, 'source_project_id': conv.get('project_id'),
                    'export_path': str(dest), 'source_fidelity': 'source archive retained externally; all mapping nodes visited'}
        man_path = dest.with_suffix('.manifest.json')
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        results.append({'file': str(dest), 'manifest': str(man_path), 'conversation_id': sid,
                        'nodes': len(nodes), 'title': title, 'source': str(input_path), 'source_member': source_member})
        if progress:
            print(f'ChatGPT archive: {scanned} inspected, {len(results)} exported', file=__import__('sys').stderr, flush=True)
    return results
