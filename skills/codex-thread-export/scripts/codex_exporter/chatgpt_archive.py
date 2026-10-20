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


@contextmanager
def _open_json(input_path: Path):
    if input_path.suffix.lower() != '.zip':
        with input_path.open('rb') as f:
            yield f, input_path.stat().st_size
        return
    with zipfile.ZipFile(input_path) as z:
        names = [x for x in z.namelist() if x.endswith('/conversations.json') or x == 'conversations.json']
        if len(names) != 1:
            raise RuntimeError('Expected exactly one conversations.json inside the ChatGPT export ZIP')
        info = z.getinfo(names[0])
        with z.open(info) as f:
            yield f, info.file_size


def iter_conversations(path: Path) -> Iterator[dict[str, Any]]:
    with _open_json(path) as (f, size):
        try:
            import ijson  # type: ignore
        except ImportError:
            ijson = None
        if ijson:
            for item in ijson.items(f, 'item'):
                if isinstance(item, dict):
                    yield item
        else:
            if size > 64 * 1024 * 1024:
                raise RuntimeError('conversations.json exceeds 64 MiB; install optional streaming parser: py -3.13 -m pip install ijson')
            result = json.load(f)
            if not isinstance(result, list):
                raise ValueError('Expected conversations.json to contain an array')
            yield from (item for item in result if isinstance(item, dict))


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
    for conv in iter_conversations(input_path):
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
        manifest = {'source_archive': str(input_path), 'conversation_id': sid, 'title': title,
                    'node_count': len(nodes), 'message_roles': dict(roles),
                    'nonmessage_nodes': missing_messages, 'source_project_id': conv.get('project_id'),
                    'export_path': str(dest), 'source_fidelity': 'source archive retained externally; all mapping nodes visited'}
        man_path = dest.with_suffix('.manifest.json')
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        results.append({'file': str(dest), 'manifest': str(man_path), 'conversation_id': sid,
                        'nodes': len(nodes), 'title': title, 'source': str(input_path)})
        if progress:
            print(f'ChatGPT archive: {scanned} inspected, {len(results)} exported', file=__import__('sys').stderr, flush=True)
    return results
