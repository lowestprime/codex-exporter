"""Regression fixtures for real large ChatGPT export layouts; never use personal data."""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]

def conv(cid,title,project=None):
    c={'id':cid,'title':title,'create_time':12345678,'mapping':{'root':{'message':{'author':{'role':'user'},'content':{'parts':['record '+cid]}},'parent':None,'children':[]}}}
    if project is not None:c['project_id']=project
    return c

def execute(tmp_path,*args):
    return subprocess.run([sys.executable,str(ROOT/'Export-CodexThread.py'),*map(str,args)],
        cwd=tmp_path,text=True,capture_output=True,timeout=30,
        env={**os.environ,'CODEX_HOME':str(tmp_path/'.codex'),
             'CODEX_EXPORT_CONFIG':str(tmp_path/'config.json')})

def create_shards(root):
    root.mkdir(parents=True,exist_ok=True)
    (root/'conversations-002.json').write_text(json.dumps([conv('C','Final project')]),encoding='utf-8')
    (root/'conversations-000.json').write_text(json.dumps([conv('A','Full-Time project','p-1')]),encoding='utf-8')
    (root/'conversations-001.json').write_text(json.dumps([conv('B','Unrelated')]),encoding='utf-8')
    (root/'chat.html').write_text('unused',encoding='utf-8')
    (root/'file-media.dat').write_bytes(b'not conversation data')
    return root

def test_sharded_directory_audit_order_and_filtered_export(tmp_path):
    folder=create_shards(tmp_path/'extracted')
    a=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-audit','--chatgpt-title-regex','Full-Time')
    assert a.returncode==0,a.stderr
    audit=json.loads(a.stdout)
    assert audit['source_file_count']==3
    assert audit['conversations_scanned']==3
    assert audit['unique_conversation_ids']==3
    assert audit['matched_count']==1
    assert audit['matched'][0]['source_member']=='conversations-000.json'
    out=tmp_path/'output'
    e=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-title-regex','Full-Time',
              '--out-dir',out,'--json')
    assert e.returncode==0,e.stderr
    rows=json.loads(e.stdout)
    assert len(rows)==1
    assert rows[0]['source_member']=='conversations-000.json'
    assert 'record A' in Path(rows[0]['file']).read_text()
    assert json.loads(Path(rows[0]['manifest']).read_text())['source_member']=='conversations-000.json'
    assert len(list(out.glob('*.md')))==1

def test_sharded_zip_in_nested_dir_without_extracting(tmp_path):
    folder=create_shards(tmp_path/'extracted')
    pack=tmp_path/'backup.zip'
    with zipfile.ZipFile(pack,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in folder.iterdir():z.write(p,arcname='some-export/'+p.name)
    a=execute(tmp_path,'--chatgpt-export',pack,'--chatgpt-audit','--chatgpt-id','C')
    assert a.returncode==0,a.stderr
    j=json.loads(a.stdout)
    assert j['source_file_count']==3 and j['matched_count']==1
    assert j['matched'][0]['id']=='C'
    assert j['matched'][0]['source_member']=='some-export/conversations-002.json'

@pytest.mark.parametrize('which',[0,1])
def test_incomplete_shards_fail_before_writes(tmp_path,which):
    folder=create_shards(tmp_path/'extracted')
    (folder/f'conversations-{which:03d}.json').unlink()
    p=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-audit')
    assert p.returncode!=0
    assert 'Missing numbered conversation shards' in p.stderr

def test_duplicate_shard_id_fails(tmp_path):
    folder=create_shards(tmp_path/'extracted')
    (folder/'second').mkdir()
    (folder/'second/conversations-002.json').write_text('[]')
    p=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-audit')
    assert p.returncode!=0
    assert 'Duplicate numbered conversation shards' in p.stderr

def test_missing_optional_parser_large_source_guidance(tmp_path):
    # Use a sparse >64 MiB file to exercise refusal before materialization.
    folder=tmp_path/'extracted';folder.mkdir()
    f=folder/'conversations-000.json'
    with f.open('wb') as writer:writer.truncate(65*1024*1024)
    import importlib.util
    if importlib.util.find_spec('ijson') is not None:pytest.skip('ijson installed')
    p=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-audit')
    assert p.returncode!=0
    assert 'codex-exporter[chatgpt]' in p.stderr

def test_35_shards_matches_user_backup_numbering(tmp_path):
    folder=tmp_path/'extracted';folder.mkdir()
    for i in range(35):
        (folder/f'conversations-{i:03d}.json').write_text(json.dumps([conv(str(i),f'title {i}')]))
    audit=execute(tmp_path,'--chatgpt-export',folder,'--chatgpt-audit', '--chatgpt-id','34')
    assert audit.returncode==0,audit.stderr
    result=json.loads(audit.stdout)
    assert result['source_file_count']==35 and result['conversations_scanned']==35
    assert result['matched_count']==1 and result['matched'][0]['source_member']=='conversations-034.json'
