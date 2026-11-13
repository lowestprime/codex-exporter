"""Behavioral browser checks: selection fidelity, cancellation, terminal isolation."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from codex_exporter import browser


def row(index: int, *, project='demo', cwd='/work/demo', missing=False, archived=False,
        title=None, mtime=None):
    return {'session_id': f'00000000-0000-4000-8000-{index:012d}',
            'path': None if missing else Path(f'/rollouts/thread-{index}.jsonl'),
            'title': title or f'Session {index}', 'cwd': cwd, 'project': project,
            'mtime': index if mtime is None else mtime, 'size': index * 1024,
            'archived': archived, 'missing_rollout': missing}


def run(commands, rows=None, **options):
    output = io.StringIO()
    choices = browser.browse_sessions(lambda: rows or [row(1), row(2), row(3)],
                                     input_stream=io.StringIO(commands), output_stream=output,
                                     page_size=options.pop('page_size', 2),
                                     width=options.pop('width', 100), **options)
    return choices, output.getvalue()


def test_page_selection_maps_to_exact_displayed_session():
    choices, output = run('n\n1\n')
    assert choices == [row(1)['session_id']]
    assert 'Page 2/2' in output


def test_stable_basket_spans_pages_and_searches_without_duplicate_exports():
    choices, _ = run('+1\nn\n+1\n/Session 3\n+1\nx\n')
    assert choices == [row(3)['session_id'], row(1)['session_id']]


def test_basket_removal_does_not_remove_unrelated_sessions():
    choices, _ = run('+1-2\n-2\nx\n')
    assert choices == [row(3)['session_id']]


def test_add_all_means_all_filtered_pages_and_preserves_catalog_order():
    choices, _ = run('+all\nx\n')
    assert choices == [row(3)['session_id'], row(2)['session_id'], row(1)['session_id']]


def test_empty_and_invalid_ranges_cannot_silently_select_everything():
    assert browser.parse_selection('', 3) == []
    assert browser.parse_selection('1, 3-4 2', 4) == [1, 2, 3, 4]
    for text in ('0', '2-1', '-1', '1-999999999999', 'all', '1,,x', '1-2-3'):
        with pytest.raises(ValueError):
            browser.parse_selection(text, 4)
    choices, output = run('1-999999999999\n1\n')
    assert choices == [row(3)['session_id']]
    assert 'ranges must ascend' in output


def test_missing_rollout_blocks_explicit_selection_and_add_all_without_omission():
    rows = [row(1), row(2, missing=True)]
    choices, output = run('+all\nx\nfilter available\na\n', rows)
    assert choices == [row(1)['session_id']]
    assert '1 selected rollout(s) are unavailable' in output
    assert 'No sessions selected' in output
    assert 'MISSING' in output


def test_id_lookup_handles_missing_paths_without_none_name_crash():
    rows = [row(1), row(2, missing=True)]
    choices, output = run(f'id {rows[1]["session_id"]}\nq\n', rows)
    assert choices == []
    assert 'unavailable' in output


def test_id_lookup_exact_match_takes_priority_and_can_use_filename():
    choices, _ = run('s thread-1\n')
    assert choices == [row(1)['session_id']]
    choices, _ = run('s\nthread-2\n')
    assert choices == [row(2)['session_id']]


def test_ambiguous_id_search_lists_choices_instead_of_choosing_first():
    choices, output = run('id 00000000\n2\n')
    assert choices == [row(2)['session_id']]
    assert '3 matches; select the exact row' in output


def test_workspace_identity_keeps_equal_project_names_separate():
    rows = [row(1, cwd='C:\\one\\demo'), row(2, cwd='C:\\two\\demo')]
    choices, output = run('m\n2\na\n', rows)
    assert choices == [row(1)['session_id']]
    assert '2 matching projects' in output
    assert 'C:\\one\\demo' in output and 'C:\\two\\demo' in output


def test_workspace_identity_keeps_posix_case_and_normalizes_windows_case():
    assert browser._project_key(row(1, cwd='/work/Project')) != browser._project_key(row(2, cwd='/work/project'))
    assert browser._project_key(row(1, cwd='C:\\WORK\\Demo')) == browser._project_key(row(2, cwd='c:/work/demo/'))
    assert browser._project_key(row(1, cwd='/')) == 'path:/'
    assert browser._project_key(row(1, cwd='C:\\')) == browser._project_key(row(2, cwd='c:/'))
    assert browser._project_key(row(1, cwd='//?/C:/work/demo')) == browser._project_key(row(2, cwd='c:/work/demo'))


def test_project_basket_addition_selects_all_matching_project_sessions():
    rows = [row(1), row(2), row(3, project='other', cwd='/work/other')]
    choices, _ = run('m\n+2\nx\n', rows)
    assert choices == [row(2)['session_id'], row(1)['session_id']]


def test_refresh_preserves_basket_but_does_not_export_disappeared_sessions():
    calls = []
    def provider():
        calls.append(True)
        return [row(1), row(2)] if len(calls) == 1 else [row(1)]
    output = io.StringIO()
    choices = browser.browse_sessions(provider, input_stream=io.StringIO('+1\nr\nx\nq\n'),
                                     output_stream=output, page_size=2, width=100)
    assert len(calls) == 2
    assert choices == []
    assert 'unavailable' in output.getvalue()


def test_navigation_search_and_preview_do_not_rescan_catalog():
    calls = []
    def provider():
        calls.append(True)
        return [row(1), row(2), row(3)]
    output = io.StringIO()
    browser.browse_sessions(provider, input_stream=io.StringIO('n\np\n/Session\nv 1\nq\n'),
                            output_stream=output, page_size=2, width=100)
    assert len(calls) == 1
    assert f"Source: {row(3)['path']}" in output.getvalue()


def test_query_is_casefolded_literal_and_matches_all_words():
    rows = [row(1, title='NVIDIA gamma fix'), row(2, title='Gamma issue'), row(3, title='NVIDIA older')]
    choices, _ = run('a\n', rows, query='nvidia FIX')
    assert choices == [row(1)['session_id']]
    choices, output = run('/[untrusted.*\nq\n', rows)
    assert choices == []
    assert 'No matches' in output


def test_sort_status_and_page_size_have_consistent_effects():
    rows = [row(1), row(2, archived=True), row(3), row(4, archived=True)]
    choices, output = run('filter archived\nsort oldest\nsize 1\npage 2\n1\n', rows)
    assert choices == [row(4)['session_id']]
    assert 'Page 2/2' in output


@pytest.mark.parametrize('commands', ['q\n', '', 's\n', '?\n'])
def test_quit_and_eof_are_successful_cancellation(commands):
    choices, output = run(commands)
    assert choices == []
    assert 'Browser cancelled' in output


def test_ctrl_c_and_closed_input_cancel_without_traceback():
    class Interrupted(io.StringIO):
        def readline(self, *args):
            raise KeyboardInterrupt
    for stream in (Interrupted(), io.StringIO()):
        if type(stream) is io.StringIO:
            stream.close()
        output = io.StringIO()
        result = browser.browse_sessions(lambda: [row(1)], input_stream=stream, output_stream=output)
        assert result == []
        assert 'Browser cancelled' in output.getvalue()


def test_browser_defaults_to_stderr_and_keeps_stdout_clean(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('1\n'))
    result = browser.browse_sessions(lambda: [row(1)])
    captured = capsys.readouterr()
    assert result == [row(1)['session_id']]
    assert captured.out == ''
    assert 'Codex session browser' in captured.err
    assert 'Select rows, command, or q >' in captured.err
    assert '\x1b' not in captured.err


def test_prompt_is_flushed_before_reading():
    events = []
    class Output(io.StringIO):
        def flush(self):
            events.append(('flush', self.getvalue()))
    class Input(io.StringIO):
        def readline(self, *args):
            assert events[-1][0] == 'flush'
            assert events[-1][1].endswith('> ')
            return super().readline(*args)
    assert browser.browse_sessions(lambda: [row(1)], input_stream=Input('q\n'), output_stream=Output()) == []


def test_terminal_controls_and_long_wide_titles_cannot_escape_width():
    rows = [row(1, title='\x1b[2JBad\nTitle\x1b]0;attack\x07 ' + '漢字' * 1000)]
    _, output = run('v 1\nq\n', rows, width=32)
    assert '\x1b' not in output and '\x07' not in output
    assert all(browser._cells(line) <= 32 for line in output.splitlines())


def test_color_respects_terminal_no_color_and_ascii_encoding(monkeypatch):
    class Terminal(io.StringIO):
        encoding = 'ascii'
        def isatty(self):
            return True
    monkeypatch.setattr(browser, '_windows_vt', lambda stream: True)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm')
    output = Terminal()
    browser.browse_sessions(lambda: [row(1)], input_stream=io.StringIO('q\n'), output_stream=output)
    assert '\x1b[' in output.getvalue()
    assert '—' not in output.getvalue()
    monkeypatch.setenv('NO_COLOR', '')
    output = Terminal()
    browser.browse_sessions(lambda: [row(1)], input_stream=io.StringIO('q\n'), output_stream=output)
    assert '\x1b' not in output.getvalue()


def test_duplicate_catalog_rows_prefer_available_latest_and_export_once():
    rows = [row(1, missing=True, mtime=99), row(1, mtime=1), row(1, title='Current', mtime=2)]
    choices, output = run('a\n', rows)
    assert choices == [row(1)['session_id']]
    assert '1 sessions' in output and 'Current' in output


def test_empty_catalog_and_invalid_options_do_not_start_input_loop():
    output = io.StringIO()
    assert browser.browse_sessions(lambda: [], input_stream=io.StringIO(''), output_stream=output) == []
    assert 'No Codex sessions found' in output.getvalue()
    for options in ({'page_size': 0}, {'page_size': 201}, {'status': 'unknown'}, {'sort': 'unknown'}, {'width': 0}):
        with pytest.raises(ValueError):
            browser.browse_sessions(lambda: [], **options)
