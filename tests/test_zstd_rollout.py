"""Compressed cold Codex rollout regression tests; never mutate a real Codex home."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'Export-CodexThread.py'
SID = '01a0f13c-001a-75e0-9095-fb002bd91abb'


def fixture(tmp_path: Path):
    zstd = pytest.importorskip('zstandard')
    home = tmp_path / '.codex'
    folder = home / 'sessions/2026/09/30'
    folder.mkdir(parents=True)
    path = folder / f'rollout-2026-09-30T00-33-56-{SID}.jsonl.zst'
    raw = ('\n'.join(json.dumps(row) for row in [
        {'type':'session_meta','payload':{'id':SID,'cwd':r'C:\Users\Example\Desktop\job\Full-Time','timestamp':'2026-09-30T00:33:56Z'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'FIRST USER TURN'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'SECOND ASSISTANT TURN'}},
    ])+'\n').encode()
    path.write_bytes(zstd.ZstdCompressor(level=3).compress(raw))
    db = sqlite3.connect(home/'state_5.sqlite')
    db.execute('CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, archived INTEGER, rollout_path TEXT)')
    db.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)',
        (SID, r'C:\Users\Example\Desktop\job\Full-Time', 'Compressed Codex', 0, str(path)[:-4]))
    db.commit();db.close()
    env={**os.environ, 'CODEX_HOME':str(home), 'CODEX_EXPORT_CONFIG':str(home/'config.json')}
    return home,path,raw,env


def cli(env,args):
    return subprocess.run([sys.executable,str(ENTRY),*args],env=env,timeout=30,capture_output=True,text=True)


def test_compressed_rollout_audit_and_fast_stream(tmp_path):
    home,path,raw,env=fixture(tmp_path)
    audit=cli(env,['--audit-sessions','--project','Full-Time'])
    assert audit.returncode==0,audit.stderr
    data=json.loads(audit.stdout)
    assert data['available_rollouts']==1
    assert data['missing_rollouts']==[]
    assert data['compressed_rollouts']==1
    assert data['threads'][0]['compression']=='zstandard'
    assert data['threads'][0]['path']==str(path)
    out=tmp_path/'exports'
    run=cli(env,['--project','Full-Time','--mode','thread','--fast','--no-progress',
        '--out-dir',str(out),'--json'])
    assert run.returncode==0,run.stderr
    results=json.loads(run.stdout)
    if isinstance(results, dict): results=[results]
    assert len(results)==1
    markdown=Path(results[0]['file']).read_text(encoding='utf-8')
    assert 'FIRST USER TURN' in markdown and 'SECOND ASSISTANT TURN' in markdown
    assert results[0]['source_sha256']==hashlib.sha256(raw).hexdigest()
    manifest=json.loads(Path(results[0]['manifest']).read_text(encoding='utf-8'))
    assert manifest['source_encoding']=='zstandard'
    assert manifest['decoded_source_bytes']==len(raw)
    assert path.read_bytes()!=raw  # Source remains compressed.


def test_plain_rollout_wins_over_compressed_sibling(tmp_path):
    home,compressed,raw,env=fixture(tmp_path)
    uncompressed=Path(str(compressed)[:-4]);uncompressed.write_bytes(raw)
    audit=cli(env,['--audit-sessions','--project','Full-Time'])
    assert audit.returncode==0,audit.stderr
    rows=json.loads(audit.stdout)
    assert rows['available_rollouts']==1
    assert rows['compressed_rollouts']==0
    assert rows['threads'][0]['path']==str(uncompressed)


def test_standalone_normal_path_resolves_compressed(tmp_path):
    home,path,raw,env=fixture(tmp_path)
    run=cli(env,['--session-id',SID,'--mode','thread','--fast','--no-progress',
                 '--out-dir',str(tmp_path/'out'),'--json'])
    assert run.returncode==0,run.stderr
    rows=json.loads(run.stdout)
    assert rows['source_sha256']==hashlib.sha256(raw).hexdigest()
