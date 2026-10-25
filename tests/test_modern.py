"""Smoke and compatibility regressions for metadata reconciliation and streaming."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'Export-CodexThread.py'
SAMPLE = ROOT / 'tests/fixtures/sample-session.jsonl'


def cli(args: list[str], home: Path, **opts):
    return subprocess.run([sys.executable, str(ENTRY), *args], capture_output=True, encoding='utf-8',
        text=True, timeout=40, env={**os.environ, 'CODEX_HOME': str(home),
                                  'CODEX_EXPORT_CONFIG': str(home / 'config.json'), **opts})


def make_home(tmp_path: Path):
    home = tmp_path / '.codex'
    active = home / 'sessions/2026/10/20'
    archived = home / 'archived_sessions'
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
           '33333333-3333-4333-8333-333333333333']
    a = active / ('rollout-2026-10-20T01-00-00-' + ids[0] + '.jsonl')
    b = archived / ('rollout-2026-10-20T02-00-00-' + ids[1] + '.jsonl')
    a.write_bytes(SAMPLE.read_bytes()); b.write_bytes(SAMPLE.read_bytes())
    con = sqlite3.connect(home / 'state_5.sqlite')
    con.execute('CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, archived INTEGER, rollout_path TEXT)')
    for sid, path, archived_flag in [(ids[0],a,0),(ids[1],b,1),(ids[2],home/'missing.jsonl',0)]:
        con.execute('INSERT INTO threads VALUES (?,?,?,?,?)', (sid, r'\\?\C:\Users\Example\Desktop\Full-Time',
            'Job Engine', archived_flag, str(path)))
    con.commit();con.close()
    return home,ids


def test_audit_and_project_discovery(tmp_path):
    home, ids = make_home(tmp_path)
    audit = cli(['--audit-sessions','--project','Full-Time'],home)
    assert audit.returncode == 0, audit.stderr
    result = json.loads(audit.stdout)
    assert result['indexed_threads'] == 3
    assert result['available_rollouts'] == 2
    assert result['missing_rollouts'] == [ids[2]]
    listing = cli(['--list-sessions'],home)
    assert listing.returncode == 0, listing.stderr
    assert '[ROLLOUT MISSING]' in listing.stdout
    assert listing.stdout.count('Job Engine') == 3


def test_stream_project_and_lossless_pieces(tmp_path):
    home,ids = make_home(tmp_path)
    out = tmp_path / 'exports'
    run = cli(['--project','Full-Time','--mode','thread','--fast','--no-progress',
               '--out-dir',str(out),'--json'],home)
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout)
    assert len(results) == 2
    assert all(Path(x['file']).is_file() for x in results)
    assert all(x['source_sha256'] for x in results)
    assert ids[2] in run.stderr
    for x in results:
        content = Path(x['file']).read_text(encoding='utf-8')
        assert 'Chronological records' in content
        assert 'Source JSONL line' in content
        manifest = json.loads(Path(x['manifest']).read_text())
        assert manifest['source_sha256'] == x['source_sha256']


def test_stream_respects_force_and_collision(tmp_path):
    home,ids = make_home(tmp_path)
    args = ['--session-id',ids[0],'--mode','thread','--fast','--no-progress','--out-dir',str(tmp_path/'out'),
            '--json','--tokenizer','regex']
    first=cli(args,home); assert first.returncode==0,first.stderr
    second=cli(args,home); assert second.returncode==0,second.stderr
    assert json.loads(first.stdout)['file'] != json.loads(second.stdout)['file']
    force=cli(['--jsonl',str(SAMPLE),'--mode','thread','--fast','--last-n-turns','2'],home)
    assert force.returncode != 0
    assert '--fast only supports' in force.stderr


def test_stream_unknown_event_sidecar(tmp_path):
    home, ids = make_home(tmp_path)
    session=home/'sessions/2026/10/20'/('rollout-2026-10-20T01-00-00-' + ids[0] + '.jsonl')
    with session.open('a',encoding='utf-8') as fp:
        fp.write(json.dumps({'type':'new_unrecognized_version_100','payload':{'new_field':'preserve in sidecar'}})+'\n')
    run = cli(['--session-id', ids[0], '--mode','thread','--fast','--no-progress',
               '--unknown-events','sidecar','--out-dir',str(tmp_path/'out'),'--json'],home)
    assert run.returncode==0,run.stderr
    data=json.loads(run.stdout)
    manifest=json.loads(Path(data['manifest']).read_text())
    assert manifest['records']['unknown_events'] >= 1
    assert 'preserve in sidecar' in Path(manifest['unknown_sidecar']).read_text()


def test_chatgpt_takeout_archive_branches(tmp_path):
    from zipfile import ZipFile
    home = tmp_path/'.codex';home.mkdir()
    conv={'id':'work-local-1', 'title':'A Work project conversation', 'create_time':1760000000,
          'project_id':'project-a', 'mapping':{
              'root': {'message': None, 'parent': None, 'children':['user']},
              'user': {'parent':'root','children':['assistant_a','assistant_b'],
                       'message':{'author':{'role':'user'},'content':{'content_type':'text','parts':['Prompt and code: `x=1`']}}},
              'assistant_a':{'parent':'user','children':[],
                       'message':{'author':{'role':'assistant'},'content':{'content_type':'text','parts':['First branch']}}},
              'assistant_b':{'parent':'user','children':[],
                       'message':{'author':{'role':'assistant'},'content':{'content_type':'text','parts':['Second branch']}}}}}
    data=tmp_path/'chatgpt.zip'
    with ZipFile(data,'w') as z:
        z.writestr('test/conversations.json',json.dumps([conv]))
    r=cli(['--chatgpt-export',str(data),'--chatgpt-id','work-local-1','--chatgpt-project-id','project-a',
           '--out-dir',str(tmp_path/'converted'),'--json'],home)
    assert r.returncode==0,r.stderr
    rows=json.loads(r.stdout); assert len(rows)==1
    text=Path(rows[0]['file']).read_text()
    assert all(x in text for x in ['Prompt and code: `x=1`','First branch','Second branch'])
    man=json.loads(Path(rows[0]['manifest']).read_text())
    assert man['node_count']==4
    assert man['message_roles']['assistant']==2
