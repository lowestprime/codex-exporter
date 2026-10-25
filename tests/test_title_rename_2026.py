from pathlib import Path
import json, sqlite3
from codex_exporter.modern import discover
from codex_exporter import cli

SID = '01a0fecf-02ee-77d2-99a2-59e86e27cc9e'
def test_renamed_index_overrides_stale_sqlite(tmp_path, monkeypatch):
    home=tmp_path/'.codex'; home.mkdir()
    file=home/'sessions'/'2026'/'10'/'02'/f'rollout-{SID}.jsonl'
    file.parent.mkdir(parents=True);file.write_text(json.dumps({'type':'session_meta','payload':{'id':SID}})+'\n')
    db=sqlite3.connect(home/'state_5.sqlite')
    db.execute('create table threads (id text, cwd text, title text, archived integer, rollout_path text)')
    db.execute('insert into threads values (?,?,?,?,?)',(SID,'C:/work/Full-Time','/goal original prompt',0,str(file)))
    db.commit(); db.close()
    (home/'session_index.jsonl').write_text(json.dumps({'id':SID,'thread_name':'OLD NAME'})+'\n'+json.dumps({'id':SID,'thread_name':'ORCHESTRATOR'})+'\n')
    monkeypatch.setenv('CODEX_HOME',str(home))
    cli.read_all_session_index_titles.cache_clear();cli.read_all_sqlite_titles.cache_clear()
    rows=discover(home)
    assert rows[0]['title']=='ORCHESTRATOR'
    assert rows[0]['title_source']=='session_index'
    assert cli.discover_title([],{},None,SID)=='ORCHESTRATOR'

def test_stream_title_and_headers_end_to_end(tmp_path, monkeypatch):
    import os, subprocess,sys
    home=tmp_path/'.codex';p=home/'sessions/2026/10/02';p.mkdir(parents=True)
    raw=p/f'rollout-2026-10-02T00-00-00-{SID}.jsonl'
    raw.write_text('\n'.join(json.dumps(x) for x in [
        {'type':'session_meta','payload':{'id':SID,'cwd':'C:/work/Full-Time'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'Hello'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'World'}}])+ '\n')
    cx=sqlite3.connect(home/'state_5.sqlite');cx.execute('create table threads(id text,cwd text,title text,archived integer,rollout_path text)')
    cx.execute('insert into threads values (?,?,?,?,?)',(SID,'C:/work/Full-Time','/goal stale title',0,str(raw)));cx.commit();cx.close()
    (home/'session_index.jsonl').write_text(json.dumps({'id':SID,'thread_name':'ORCHESTRATOR'})+'\n')
    dest=tmp_path/'out';env={**os.environ,'CODEX_HOME':str(home),'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    run=subprocess.run([sys.executable,'-m','codex_exporter','--mode','thread','--session-id',SID,'--fast','--no-progress','--out-dir',str(dest)],env=env,text=True,capture_output=True,timeout=15)
    assert run.returncode==0,run.stderr
    output=next(dest.glob('*.md'));md=output.read_text()
    assert 'orchestrator' in output.name.lower() and SID[-8:] in output.name
    assert 'title: "ORCHESTRATOR"' in md and '# ORCHESTRATOR' in md
    assert '## Thread export map' in md and '## Chronological records' in md
    assert 'World' in md
