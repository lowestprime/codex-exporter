"""Sort fidelity, complete scripted output, console navigation and state cleanup."""
from __future__ import annotations

import io
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import types

import pytest

from codex_exporter import display


class Terminal(io.StringIO):
    def isatty(self):
        return True


class UnreadInput(io.StringIO):
    def readline(self, *args):
        raise AssertionError('Noninteractive output attempted to read input')


def test_sort_keeps_native_turn_numbers_objects_and_stable_ties():
    rows = [{'turn': 91, 'timestamp': '2026-11-14T17:50:00-08:00', 'preview': 'z'},
            {'turn': 4, 'timestamp': '2026-11-15T01:49:00Z', 'preview': 'a'},
            {'turn': 7, 'timestamp': '2026-11-15T01:49:00Z', 'preview': 'a'}]
    original = list(rows)
    result = display.sort_rows(rows, kind='turns', sort='timestamp:asc,preview:desc')
    assert [r['turn'] for r in result] == [4, 7, 91]
    assert result[0] is rows[1] and rows == original
    assert [r['turn'] for r in display.sort_rows(rows, kind='turns')] == [4, 7, 91]


def test_sort_numeric_columns_nulls_and_per_field_direction():
    rows = [{'session_id': 'a', 'size': 9, 'project': 'Beta'},
            {'session_id': 'b', 'size': 100, 'project': 'alpha'},
            {'session_id': 'c', 'size': None, 'project': 'alpha'},
            {'session_id': 'd', 'size': 2, 'project': 'ALPHA'}]
    result = display.sort_rows(rows, kind='sessions', sort='project:asc,size:desc', order='desc')
    assert [r['session_id'] for r in result] == ['b', 'd', 'c', 'a']
    for direction in ('asc', 'desc'):
        assert display.sort_rows(rows, kind='sessions', sort='size', order=direction)[-1]['session_id'] == 'c'
    assert display.sort_rows(rows, kind='sessions', sort='native') == rows


@pytest.mark.parametrize('kind,field,values', [
    ('messages', 'chars', [20, 200, 3]), ('responses', 'tokens', ['20', '2,000', '3']),
    ('events', 'count', [20, 200, 3]), ('projects', 'count', [20, 200, 3]),
    ('audit', 'size', [20, 200, 3]),
])
def test_every_listing_kind_supports_numeric_columns(kind, field, values):
    rows = [{field: value, 'tag': index} for index, value in enumerate(values)]
    assert [r['tag'] for r in display.sort_rows(rows, kind=kind, sort=f'{field}:asc')] == [2, 0, 1]


@pytest.mark.parametrize('spec', ['unknown', 'title:reverse', '-title:asc', 'title,,size', ','])
def test_invalid_sort_is_actionable(spec):
    with pytest.raises(ValueError, match='sort|Sort|fields|field'):
        display.sort_rows([], kind='sessions', sort=spec)


def test_recent_oldest_and_native_are_explicit():
    rows = [{'turn': 1, 'timestamp': '2026-11-01T00:00:00Z'},
            {'turn': 2, 'timestamp': '2026-11-02T00:00:00Z'}]
    assert display.sort_rows(rows, kind='turns', sort='recent') == rows[::-1]
    assert display.sort_rows(rows, kind='turns', sort='oldest') == rows
    assert display.sort_rows(rows, kind='turns', sort='native', order='desc') == rows


@pytest.mark.parametrize('pager', ['auto', 'always', 'never'])
def test_redirected_output_never_pages_or_truncates(pager):
    source, target = UnreadInput(), io.StringIO()
    lines = [f'{i} ' + '長' * 100 for i in range(100)]
    display.print_listing(lines, settings=display.DisplaySettings(pager=pager, page_size=2),
                          input_stream=source, output_stream=target)
    assert target.getvalue() == '\n'.join(lines) + '\n'
    assert '\x1b' not in target.getvalue()


def test_json_never_pages_even_on_tty():
    source, target = Terminal(), Terminal()
    display.print_listing(['{"rows": [1, 2, 3]}'], settings=display.DisplaySettings(pager='always'),
                          input_stream=source, output_stream=target, json_output=True,
                          key_reader=lambda: pytest.fail('JSON read input'))
    assert target.getvalue() == '{"rows": [1, 2, 3]}\n'


def test_terminal_navigation_limits_frames_and_restores_screen(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setattr(display, '_terminal_size', lambda stream: os.terminal_size((90, 10)))
    target, source = Terminal(), Terminal()
    keys = iter(['down', 'pagedown', 'up', 'end', 'home', 'q'])
    display.print_listing([f'row {i}' for i in range(30)], settings=display.DisplaySettings(page_size=999),
                          input_stream=source, output_stream=target, key_reader=lambda: next(keys))
    content = target.getvalue()
    assert content.startswith('\x1b[?1049h\x1b[?25l')
    assert content.endswith('\x1b[?25h\x1b[?1049l')
    assert 'lines 1-6/30' in content and 'lines 25-30/30' in content
    frames = content.split('\x1b[H\x1b[2J')[1:]
    assert len(frames) == 6
    assert all(frame.count('\n') <= 7 for frame in frames)
    assert all('Enter/Space/PgDn next' in frame for frame in frames)


def test_keyboard_interrupt_restores_terminal(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm')
    target = Terminal()
    def interrupt():
        raise KeyboardInterrupt
    display.print_listing(['row'] * 100, settings=display.DisplaySettings(pager='always'),
                          input_stream=Terminal(), output_stream=target, key_reader=interrupt)
    assert target.getvalue().endswith('\x1b[?25h\x1b[?1049l')


def test_wrap_preserves_all_printable_characters_and_cell_width():
    values = ['alpha  beta ' + '長い列' * 12 + ' cafe\u0301', '']
    wrapped = display._wrap_lines(values, 17)
    assert ''.join(wrapped) == ''.join(values)
    assert all(display._cells(row) <= 17 for row in wrapped)
    assert display._wrap_lines(['safe\x1b]0;spoof\x07\x1b[31mred'], 100) == ['safered']


def test_windows_console_extended_keys(monkeypatch):
    chars = iter(['\xe0', 'H', '\x00', 'Q', '\xe0', 'I', '\xe0', 'G', '\xe0', 'O', '\r', 'q'])
    monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(getwch=lambda: next(chars)))
    reader = display.TerminalKeys(io.StringIO())
    reader.windows = True
    with reader:
        assert [reader.read() for _ in range(7)] == ['up', 'pagedown', 'pageup', 'home', 'end', 'enter', 'q']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pseudo-terminal integration; Windows keys tested separately')
def test_real_pty_pages_and_restores_terminal_attributes():
    import fcntl
    import pty
    import struct
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 80, 0, 0))
    code = ("import sys,termios; from codex_exporter.display import *; "
            "before=termios.tcgetattr(0); "
            "print_listing([f'row {i}' for i in range(40)], settings=DisplaySettings()); "
            "assert before==termios.tcgetattr(0); print('TERMINAL_RESTORED', flush=True)")
    env = {**os.environ, 'TERM': 'xterm-256color'}
    process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                               cwd=Path(__file__).resolve().parents[1], env=env)
    os.close(slave)
    output = bytearray()
    def until(marker):
        deadline = time.monotonic() + 5
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        assert marker in output, output.decode(errors='replace')
    try:
        until(b'Enter/Space/PgDn')
        assert b'row 39' not in output
        os.write(master, b'\x1b[F')  # End
        until(b'row 39')
        os.write(master, b'q')
        until(b'TERMINAL_RESTORED')
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
