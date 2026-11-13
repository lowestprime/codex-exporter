"""Interactive session selection without background output or stdout pollution.

The caller must enter this chooser outside live progress stages. Catalog metadata
is cached until refresh; browsing never parses entire rollouts or changes sources.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
import os
import re
import shutil
import sys
from typing import Any, TextIO

from .progress import _cells, _clean, _clip, _windows_vt

SORT_CHOICES = ('recent', 'oldest', 'size', 'title', 'project')
STATUS_CHOICES = ('all', 'available', 'live', 'archived', 'missing')
Row = dict[str, Any]


def parse_selection(raw: str, upper: int) -> list[int]:
    """Parse finite page-relative rows without expanding unchecked huge ranges."""
    if not raw.strip():
        return []
    selected: set[int] = set()
    for token in re.split(r'[\s,]+', raw.strip()):
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', token)
        if not match:
            raise ValueError('Use page row numbers such as 1,3-5 (or id PREFIX).')
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > upper:
            raise ValueError(f'Rows must be between 1 and {upper}; ranges must ascend.')
        selected.update(range(first, last + 1))
    return sorted(selected)


def _available(row: Mapping[str, Any]) -> bool:
    return row.get('path') is not None and not row.get('missing_rollout', False)


def _project_key(row: Mapping[str, Any]) -> str:
    """Group exact workspaces, never merge projects just because basenames match."""
    cwd = str(row.get('cwd') or '').replace('\\', '/')
    if cwd:
        if cwd.casefold().startswith('//?/unc/'):
            cwd = '//' + cwd[8:]
        elif cwd.startswith('//?/'):
            cwd = cwd[4:]
        cwd = cwd.rstrip('/') or '/'
        if re.match(r'^(?:[A-Za-z]:(?:/|$)|//)', cwd):
            cwd = cwd.casefold()
        return 'path:' + cwd
    if row.get('project_id'):
        return 'id:' + str(row['project_id'])
    return 'name:' + str(row.get('project') or '(unknown project)')


def _prepare_rows(rows: Iterable[Mapping[str, Any]]) -> list[Row]:
    found: dict[str, Row] = {}
    for item in rows:
        row = dict(item)
        sid = str(row.get('session_id') or '').strip()
        if not sid:
            raise ValueError('Session catalog contains a row without a session ID.')
        row['session_id'] = sid
        for name in ('title', 'cwd', 'project', 'modified'):
            row[name] = str(row.get(name) or '')
        row['size'] = max(0, int(row.get('size') or 0))
        row['mtime'] = float(row.get('mtime') or 0)
        row['archived'] = bool(row.get('archived', False))
        row['missing_rollout'] = not _available(row)
        key = sid.casefold()
        old = found.get(key)
        if old is None or (_available(row), row['mtime']) > (_available(old), old['mtime']):
            found[key] = row
    return list(found.values())


def _size(value: int) -> str:
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if amount < 1024 or unit == 'TiB':
            return f'{int(amount)} B' if unit == 'B' else f'{amount:.1f} {unit}'
        amount /= 1024
    return str(value)


def _stamp(row: Mapping[str, Any]) -> str:
    if row.get('mtime'):
        try:
            return datetime.fromtimestamp(row['mtime']).strftime('%Y-%m-%d %H:%M')
        except (ValueError, OverflowError, OSError):
            pass
    return str(row.get('modified') or 'unknown time')


class SessionBrowser:
    """Small line-command TUI; each prompt is explicitly flushed to stderr."""

    def __init__(self, rows_provider: Callable[[], Iterable[Mapping[str, Any]]], *,
                 input_stream: TextIO | None = None, output_stream: TextIO | None = None,
                 page_size: int | None = None, width: int | None = None,
                 query: str = '', status: str = 'all', sort: str = 'recent',
                 color: bool | None = None):
        if status not in STATUS_CHOICES or sort not in SORT_CHOICES:
            raise ValueError('Unsupported browser status filter or sort order.')
        if page_size is not None and not 1 <= page_size <= 200:
            raise ValueError('Browser page size must be between 1 and 200.')
        if width is not None and width < 1:
            raise ValueError('Browser display width must be positive.')
        self.provider = rows_provider
        self.input = input_stream if input_stream is not None else sys.stdin
        self.output = output_stream if output_stream is not None else sys.stderr
        self.fixed_width = width
        self.page_size = page_size or max(5, min(20, (self._terminal_size().lines - 12) // 2))
        self.query, self.status, self.sort = query.strip(), status, sort
        self.page = 0
        self.view = 'sessions'
        self.active_project = ''
        self.selected: dict[str, Row] = {}
        self.rows: list[Row] = []
        self.visible: list[Row] = []
        self.groups: list[tuple[str, list[Row]]] = []
        self.pages = 1
        self.unicode = True
        try:
            '—…'.encode(getattr(self.output, 'encoding', None) or 'utf-8')
        except (UnicodeError, LookupError):
            self.unicode = False
        try:
            tty = bool(self.output.isatty())
        except (AttributeError, OSError, ValueError):
            tty = False
        self.color = (color is not False and tty and 'NO_COLOR' not in os.environ
                      and os.environ.get('TERM', '').lower() != 'dumb' and _windows_vt(self.output))

    def _terminal_size(self):
        try:
            return os.get_terminal_size(self.output.fileno())
        except (AttributeError, OSError, ValueError):
            return shutil.get_terminal_size(fallback=(100, 32))

    @property
    def width(self) -> int:
        return self.fixed_width or self._terminal_size().columns

    def write(self, text: object = '', *, color: str = '', end: str = '\n') -> None:
        budget = max(0, self.width - (_cells(end) if end != '\n' else 0))
        value = _clip(_clean(text, limit=8192), budget, self.unicode)
        if self.color and color:
            value = f'\x1b[{color}m{value}\x1b[0m'
        try:
            self.output.write(value + end)
            self.output.flush()
        except UnicodeEncodeError:
            encoding = getattr(self.output, 'encoding', None) or 'ascii'
            self.output.write((value + end).encode(encoding, 'replace').decode(encoding))
            self.output.flush()
        except (OSError, ValueError) as exc:
            raise EOFError('Browser output is unavailable') from exc

    def prompt(self, text: str = 'Select rows, command, or q > ') -> str:
        self.write(text.rstrip(), color='36', end=' ')
        try:
            value = self.input.readline()
        except (OSError, ValueError) as exc:
            raise EOFError('Browser input is unavailable') from exc
        if not value:
            raise EOFError
        # Piped scripts do not echo their input; keep subsequent output on its own line.
        try:
            terminal = self.input.isatty()
        except (AttributeError, OSError, ValueError):
            terminal = False
        if not terminal:
            self.write()
        return value.strip()

    def refresh(self) -> None:
        self.rows = _prepare_rows(self.provider())
        current = {r['session_id'].casefold(): r for r in self.rows}
        for sid, row in list(self.selected.items()):
            self.selected[sid] = current.get(sid, {**row, 'path': None, 'missing_rollout': True})

    def filtered(self) -> list[Row]:
        words = self.query.casefold().split()
        candidates = list(self.selected.values()) if self.view == 'basket' else self.rows
        result = []
        for row in candidates:
            available = _available(row)
            if ((self.status == 'available' and not available)
                    or (self.status == 'missing' and available)
                    or (self.status == 'live' and (not available or row['archived']))
                    or (self.status == 'archived' and not row['archived'])):
                continue
            if self.view == 'project' and _project_key(row) != self.active_project:
                continue
            haystack = ' '.join(str(row.get(k) or '') for k in
                                ('title', 'cwd', 'project', 'session_id', 'path', 'db_rollout_path')).casefold()
            if all(word in haystack for word in words):
                result.append(row)
        functions = {
            'recent': lambda r: (-r['mtime'], r['session_id'].casefold()),
            'oldest': lambda r: (r['mtime'], r['session_id'].casefold()),
            'size': lambda r: (-r['size'], -r['mtime'], r['session_id'].casefold()),
            'title': lambda r: (r['title'].casefold(), r['session_id'].casefold()),
            'project': lambda r: (_project_key(r), -r['mtime'], r['session_id'].casefold()),
        }
        return sorted(result, key=functions[self.sort])

    def render(self) -> None:
        rows = self.filtered()
        self.write()
        dash = '—' if self.unicode else '-'
        self.write(f'Codex session browser {dash} select before export starts', color='1;36')
        missing = sum(not _available(r) for r in self.rows)
        self.write(f'{len(self.rows)} sessions | {missing} missing | {len(self.selected)} selected', color='2')
        context = f'View {self.view} | {self.status} | sort {self.sort}'
        if self.query:
            context += f' | search: {self.query}'
        self.write(context)
        if self.view == 'project':
            self.write('Workspace: ' + self.active_project.partition(':')[2], color='2')
        if self.view == 'projects':
            grouped: dict[str, list[Row]] = {}
            for row in rows:
                grouped.setdefault(_project_key(row), []).append(row)
            if self.sort in ('title', 'project'):
                groups = sorted(grouped.items(), key=lambda x: (x[1][0]['project'].casefold(), x[0]))
            elif self.sort == 'size':
                groups = sorted(grouped.items(), key=lambda x: (-sum(r['size'] for r in x[1]), x[0]))
            else:
                groups = sorted(grouped.items(), key=lambda x: (
                    (1 if self.sort == 'oldest' else -1) * max(r['mtime'] for r in x[1]), x[0]))
            count = len(groups)
        else:
            groups = []
            count = len(rows)
        self.pages = max(1, (count + self.page_size - 1) // self.page_size)
        self.page = min(max(0, self.page), self.pages - 1)
        start = self.page * self.page_size
        self.write(f'Page {self.page + 1}/{self.pages} | {count} matching '
                   + ('projects' if self.view == 'projects' else 'sessions'), color='1')
        self.visible = rows[start:start + self.page_size] if not groups else []
        self.groups = groups[start:start + self.page_size]
        if not count:
            self.write('No matches. / clears search; filter all restores all statuses.', color='33')
        for index, (_, items) in enumerate(self.groups, 1):
            row = items[0]
            count_selected = sum(r['session_id'].casefold() in self.selected for r in items)
            self.write(f' {index:>3}  {row["project"] or "(unknown project)"} | '
                       f'{len(items)} sessions | {count_selected} selected', color='36')
            self.write('      ' + (row['cwd'] or str(row.get('project_id') or '(no workspace path)')), color='2')
        for index, row in enumerate(self.visible, 1):
            marked = row['session_id'].casefold() in self.selected
            state = 'MISSING' if not _available(row) else 'ARCHIVED' if row['archived'] else 'LIVE'
            lead = f'{"[*]" if marked else "[ ]"} {index:>3} {state:<8} '
            self.write(lead + (row['title'] or '(untitled)'),
                       color='33' if not _available(row) else '32' if marked else '')
            detail = f'        {row["session_id"]} | {_stamp(row)} | {_size(row["size"])} | {row["project"]}'
            if self.width < 95:
                detail = f'        {row["session_id"][:8]} | {_size(row["size"])} | {row["project"]}'
            self.write(detail, color='2')
        self.write('Rows / ranges: export | +rows: add | -rows: remove | x: export basket')
        self.write('n/p pages | m projects | /search | v row details | r refresh | ? help | q quit')

    def help(self) -> None:
        for line in (
            'Session browser commands (row numbers refer to the displayed page):',
            '1,3-5       Export those sessions; in projects view select one project.',
            'a           Export every session on the current page.',
            '+1,3-5      Add rows to the selection basket, without exporting.',
            '-1,3-5      Remove those visible rows from the basket.',
            '+all        Add every matching session across all pages.',
            '+page       Add every row on this page.',
            'x / export  Export the basket. No missing rollout is silently skipped.',
            'basket      View selected sessions; clear empties the basket.',
            'n / p       Next / previous page. page N jumps to a page.',
            'm           Toggle projects / sessions. b goes back.',
            '/words      Search ID, title, workspace, project, filename (all words).',
            '/           Clear the search. Searches are literal, not regular expressions.',
            'id PREFIX   Export an unambiguous ID or filename match; s prompts for it.',
            'v N         Inspect full session ID, workspace, file path and metadata.',
            'filter NAME all, available, live (unarchived), archived, missing.',
            'sort NAME   recent, oldest, size, title, project.',
            'size N      Set 1..200 rows per page. r refreshes the catalog.',
            'q / Ctrl+C / EOF  Cancel cleanly without an export.',
        ):
            self.write(line)

    def inspect(self, token: str) -> None:
        items = self.groups if self.view == 'projects' else self.visible
        choice = parse_selection(token, len(items))
        if len(choice) != 1:
            raise ValueError('Use v followed by one visible row number.')
        if self.view == 'projects':
            _, rows = items[choice[0] - 1]
            self.write(f'Project: {rows[0]["project"] or "(unknown project)"}')
            self.write(f'Workspace: {rows[0]["cwd"] or "(none)"}')
            self.write(f'{len(rows)} matching sessions; {sum(not _available(r) for r in rows)} missing')
            return
        row = items[choice[0] - 1]
        fields = [('Session', row['session_id']), ('Title', row['title'] or '(untitled)'),
                  ('Workspace', row['cwd'] or '(none)'), ('Project ID', row.get('project_id') or '(none)'),
                  ('Source', str(row.get('path') or row.get('db_rollout_path') or '(missing rollout)')),
                  ('Modified', _stamp(row)), ('Size', _size(row['size'])),
                  ('State', 'missing rollout' if not _available(row) else 'archived' if row['archived'] else 'live'),
                  ('Compression', row.get('compression') or 'none'),
                  ('Title source', row.get('title_source') or 'catalog')]
        for name, value in fields:
            text = f'{name}: {_clean(value, limit=4096)}'
            while text:
                piece = ''
                for ch in text:
                    if _cells(piece + ch) > self.width:
                        break
                    piece += ch
                if not piece:
                    piece = text[0]
                self.write(piece)
                text = text[len(piece):]
        if len(str(row['title'])) > 4096:
            self.write('Title preview limited to 4096 characters; export retains its configured title.')

    def _export(self, rows: list[Row]) -> list[str]:
        if not rows:
            raise ValueError('No sessions selected. Choose rows or add them with +rows.')
        missing = [row['session_id'] for row in rows if not _available(row)]
        if missing:
            raise ValueError(f'{len(missing)} selected rollout(s) are unavailable: '
                             + ', '.join(missing) + '. Use v for details or filter available.')
        return list(dict.fromkeys(row['session_id'] for row in rows))

    def _chosen(self, raw: str) -> list[Row]:
        if self.view == 'projects':
            indices = parse_selection(raw, len(self.groups))
            return [r for i in indices for r in self.groups[i - 1][1]]
        return [self.visible[i - 1] for i in parse_selection(raw, len(self.visible))]

    def command(self, raw: str) -> tuple[list[str] | None, bool]:
        """Return export result (or None) and whether the view needs repainting."""
        lower = raw.casefold()
        command, _, value = raw.partition(' ')
        command = command.casefold()
        value = value.strip()
        if lower in ('q', 'quit', 'exit'):
            self.write('Browser cancelled; no export requested.', color='2')
            return [], False
        if not raw:
            return None, False
        if lower in ('?', 'help'):
            self.help()
            return None, False
        if lower in ('n', 'next', 'p', 'prev'):
            self.page = min(self.pages - 1, max(0, self.page + (1 if lower in ('n', 'next') else -1)))
        elif command == 'page':
            if not value.isdigit() or not 1 <= int(value) <= self.pages:
                raise ValueError(f'Page must be between 1 and {self.pages}.')
            self.page = int(value) - 1
        elif lower in ('m', 'projects', 'sessions', 'b', 'back', 'basket'):
            if lower == 'basket':
                self.view = 'basket'
            elif lower in ('b', 'back'):
                self.view = 'projects' if self.view == 'project' else 'sessions'
            else:
                self.view = 'sessions' if lower == 'sessions' or (lower == 'm' and self.view == 'projects') else 'projects'
            self.active_project, self.page = '', 0
        elif raw.startswith('/') or command == 'search':
            self.query, self.page = (raw[1:].strip() if raw.startswith('/') else value), 0
        elif command == 'filter':
            if value.casefold() not in STATUS_CHOICES:
                raise ValueError('Filter must be all, available, live, archived, or missing.')
            self.status, self.page = value.casefold(), 0
        elif command == 'sort':
            if value.casefold() not in SORT_CHOICES:
                raise ValueError('Sort must be recent, oldest, size, title, or project.')
            self.sort, self.page = value.casefold(), 0
        elif command == 'size':
            if not value.isdigit() or not 1 <= int(value) <= 200:
                raise ValueError('Page size must be between 1 and 200.')
            self.page_size, self.page = int(value), 0
        elif lower in ('r', 'refresh'):
            self.refresh()
        elif lower in ('c', 'clear'):
            self.selected.clear()
        elif lower in ('x', 'export'):
            return self._export(list(self.selected.values())), False
        elif command in ('v', 'preview'):
            self.inspect(value)
            return None, False
        elif command in ('s', 'id'):
            needle = (value or self.prompt('Session ID or filename prefix > ')).casefold()
            if not needle:
                raise ValueError('Enter a nonempty session ID or filename prefix.')
            matches = [r for r in self.rows if r['session_id'].casefold() == needle]
            if not matches:
                matches = [r for r in self.rows if needle in r['session_id'].casefold()
                           or needle in str(r.get('path') or r.get('db_rollout_path') or '').replace('\\', '/').split('/')[-1].casefold()]
            if len(matches) == 1:
                return self._export(matches), False
            if not matches:
                raise ValueError('No session ID or filename matched; use r to refresh.')
            self.query, self.view, self.active_project, self.page = needle, 'sessions', '', 0
            self.write(f'{len(matches)} matches; select the exact row.', color='33')
        elif raw[0] in ('+', '-'):
            expression = raw[1:].strip()
            rows = self.filtered() if expression.casefold() == 'all' else (
                [r for _, group in self.groups for r in group] if self.view == 'projects' else self.visible
            ) if expression.casefold() == 'page' else self._chosen(expression)
            if raw[0] == '+':
                self._export(rows)  # validate before mutating the basket
                self.selected.update((r['session_id'].casefold(), r) for r in rows)
            else:
                for row in rows:
                    self.selected.pop(row['session_id'].casefold(), None)
        elif lower == 'a':
            if self.view == 'projects':
                raise ValueError('Open a project first, or use +page then x to export all its visible projects.')
            return self._export(self.visible), False
        else:
            if self.view == 'projects':
                choices = parse_selection(raw, len(self.groups))
                if len(choices) != 1:
                    raise ValueError('Open one project row, or use +rows for multiple projects.')
                self.active_project = self.groups[choices[0] - 1][0]
                self.view, self.page = 'project', 0
            else:
                return self._export(self._chosen(raw)), False
        return None, True

    def run(self) -> list[str]:
        try:
            self.refresh()
            if not self.rows:
                self.write('No Codex sessions found. Inspect --audit-sessions or select --jsonl PATH.')
                return []
            repaint = True
            while True:
                if repaint:
                    self.render()
                try:
                    result, repaint = self.command(self.prompt())
                    if result is not None:
                        return result
                except (ValueError, OSError) as exc:
                    self.write(str(exc), color='33')
                    repaint = False
        except (KeyboardInterrupt, EOFError):
            try:
                self.write()
                self.write('Browser cancelled; no export requested.', color='2')
            except (EOFError, OSError, ValueError):
                pass
            return []


def browse_sessions(rows_provider: Callable[[], Iterable[Mapping[str, Any]]], *,
                    input_stream: TextIO | None = None, output_stream: TextIO | None = None,
                    page_size: int | None = None, width: int | None = None,
                    query: str = '', status: str = 'all', sort: str = 'recent',
                    color: bool | None = None) -> list[str]:
    """Select IDs interactively; q, EOF and Ctrl+C return an empty selection.

    Input may also be a finite line-command script. The UI and flushed prompt
    always use output_stream (stderr by default), leaving stdout for CLI results.
    """
    return SessionBrowser(rows_provider, input_stream=input_stream, output_stream=output_stream,
                          page_size=page_size, width=width, query=query, status=status,
                          sort=sort, color=color).run()
