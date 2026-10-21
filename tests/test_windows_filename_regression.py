"""Regression coverage for real Windows project titles containing attachment paths."""
from __future__ import annotations

from pathlib import Path

import json
import re
import subprocess
import sys

from codex_exporter.cli import safe_export_filename, compact_session_cell
from test_modern import make_home, cli

BAD_TITLE = (r'/goal Referenced pasted text files: '
             '- pasted text file: C:\\Users\\Cooper\\.codex\\attachments\\'
             r'ad838633-92e6-4a05-8d78-5aaeb2354250\pasted-text-1.txt. '
             'Read this file before continuing.')


def test_basename_is_windows_safe_for_attachment_path():
    name = safe_export_filename('codex_' + BAD_TITLE + '_thread_f8c8abcd.md')
    assert name.endswith('.md') and 'f8c8abcd' in name
    assert len(name) <= 220
    assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    assert safe_export_filename('CON.md') == '_CON.md'
    assert safe_export_filename('nested\\Windows/C:bad\\title.md') == 'nested_Windows_C_bad_title.md'


def test_stream_single_and_project_survive_windows_title(tmp_path):
    home, ids = make_home(tmp_path)
    import sqlite3
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        c.execute('UPDATE threads SET title=?', (BAD_TITLE,))
    out = tmp_path / 'out'
    for arguments, count in [(['--session-id',ids[0]],1), (['--project','Full-Time'],2)]:
        result = cli([*arguments, '--mode','thread','--fast','--no-progress',
                      '--unknown-events','sidecar','--out-dir',str(out),'--json'],home)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        rows = data if isinstance(data,list) else [data]
        assert len(rows)==count
        from pathlib import Path
        for row in rows:
            p=Path(row['file'])
            assert p.exists() and p.parent==out
            assert p.name.endswith('.md')
            assert 'Source JSONL line' in p.read_text(encoding='utf-8')
            assert Path(row['manifest']).is_file()


def test_stream_custom_template_normalizes_substitution_and_rejects_directories(tmp_path):
    home,ids=make_home(tmp_path)
    result=cli(['--session-id',ids[0], '--mode','thread','--fast','--no-progress',
       '--name',BAD_TITLE,'--filename-template','{title}_{mode}_{session_short}.md',
       '--out-dir',str(tmp_path/'out'),'--json'],home)
    assert result.returncode==0,result.stderr
    filename=json.loads(result.stdout)['file']
    assert not re.search(r'[<>:"\\|?*]', Path(filename).name)
    rejected=cli(['--session-id',ids[0], '--mode','thread','--fast',
       '--filename-template','subdir/{title}.md'],home)
    assert rejected.returncode!=0
    assert 'basename, not a directory' in rejected.stderr


def test_multiline_titled_listing_stays_bounded_and_one_row(tmp_path):
    home,ids=make_home(tmp_path)
    import sqlite3
    enormous = 'First line\n' + ('A huge prompt line.\n' * 12000) + '| end'
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        c.execute('UPDATE threads SET title=?', (enormous,))
    result=cli(['--list-sessions'],home)
    assert result.returncode==0,result.stderr
    lines=result.stdout.splitlines()
    assert len(lines)==5
    assert all('A huge prompt line.' not in x or len(x)<500 for x in lines)
    assert all(len(x)<500 for x in lines)
    full=cli(['--list-sessions','--full-session-titles'],home)
    assert full.returncode==0
    assert len(full.stdout.splitlines())==5
    assert '\\| end' in full.stdout


def test_multiple_bad_windows_filenames_is_collision_safe(tmp_path):
    home, ids = make_home(tmp_path)
    import sqlite3
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        c.execute('UPDATE threads SET title=?', (BAD_TITLE,))
    out=tmp_path/'many'
    args=['--project','Full-Time','--mode','thread','--fast','--no-progress','--out-dir',str(out),'--json']
    a=cli(args,home);b=cli(args,home)
    assert a.returncode==b.returncode==0, (a.stderr,b.stderr)
    names=[r['file'] for r in json.loads(a.stdout)+json.loads(b.stdout)]
    assert len(names)==len(set(names))==4