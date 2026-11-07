"""The archive is always verified by a separate ZIP decoder before cleanup."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'Export-CodexThread.py'
SID = '11111111-2222-4333-8444-555555555555'


def prepare(tmp_path, broken=False):
    home = tmp_path / 'home'
    folder = home / 'sessions'
    folder.mkdir(parents=True)
    source = folder / f'rollout-2026-11-01T00-00-00-{SID}.jsonl'
    rows = [
        {'type':'session_meta','payload':{'id':SID,'cwd':'C:/ucla','timestamp':'2026-11-01T00:00:00Z'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'Hello UCLA original'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Complete answer'}},
    ]
    source.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    # Synthetic 7z-compatible stub: honors the requested ZIP compression method.
    archiver=tmp_path/('7z-test.cmd' if os.name == 'nt' else '7z-test')
    stub=tmp_path/'mock-seven-zip.py'
    stub.write_text("#!/usr/bin/env python3\nimport sys,zipfile\nargv=sys.argv[1:]; out=next(a for a in argv if a.endswith('.zip'))\nfiles=argv[argv.index(out)+1:]\nmethod=zipfile.ZIP_LZMA if '-mm=LZMA' in argv else zipfile.ZIP_DEFLATED\nwith zipfile.ZipFile(out,'w',compression=method) as z:\n    for f in files:\n        if BROKEN: z.writestr(f,'tampered')\n        else: z.write(f,arcname=f)\n".replace('BROKEN', 'True' if broken else 'False'))
    if os.name == 'nt':
        archiver.write_text('@echo off\n"' + sys.executable + '" "' + str(stub) + '" %*\n', encoding='utf-8')
    else:
        archiver.write_text(stub.read_text())
        archiver.chmod(0o755)
    env={**os.environ,'CODEX_HOME':str(home),'CODEX_EXPORT_CONFIG':str(home/'cfg.json')}
    return source,original_hash,archiver,env


def run(env,*args):
    return subprocess.run([sys.executable,str(ENTRY),*args],env=env,text=True,
                          capture_output=True,timeout=90)


def test_full_pipeline_and_positional_uuid(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    out=tmp_path/'outputs'
    r=run(env,'--mode','thread',SID,'--fast','--no-progress',
          '--archiver-path',str(arch),'--out-dir',str(out),'--json')
    assert r.returncode==0,r.stderr
    value=json.loads(r.stdout)
    assert value['archive_verified'] is True
    assert value['file'] is None and value['manifest'] is None
    assert value['loose_files_removed'] is True
    assert value['source_copy_sha256']==digest
    zp=Path(value['archive'])
    assert zp.exists()
    assert len(list(out.glob('*.md')))==0
    assert len(list(out.glob('*.jsonl')))==0
    assert len(list(out.glob('*.manifest.json')))==0
    with zipfile.ZipFile(zp) as z:
        assert len(z.namelist())==3
        assert z.namelist()==value['archive_members'] or set(z.namelist())==set(value['archive_members'])
        assert {x.split('.manifest.json')[0].split('.jsonl')[0].split('.md')[0] for x in z.namelist()}=={zp.stem}
        srcdata=z.read(zp.stem+'.jsonl')
        assert hashlib.sha256(srcdata).hexdigest()==digest
        assert b'Hello UCLA original' in srcdata
        assert z.read(zp.stem+'.md').startswith(b'---')
        assert zipfile.ZIP_LZMA==z.getinfo(zp.stem+'.md').compress_type
    assert src.exists() and hashlib.sha256(src.read_bytes()).hexdigest()==digest


def test_no_cleanup_keeps_loose_triplet(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    r=run(env,'--session-id',SID,'--mode','thread','--fast','--no-progress','--json',
          '--archiver-path',str(arch),'--no-archive-cleanup','--out-dir',str(tmp_path/'out'))
    assert r.returncode==0,r.stderr
    result=json.loads(r.stdout)
    assert all(Path(result[k]).exists() for k in ('file','manifest','source_copy','archive'))
    assert result['loose_files_removed'] is False


def test_disable_archive_keep_copy(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    r=run(env,'--session-id',SID,'--mode','thread','--fast','--json','--no-progress',
          '--no-archive','--out-dir',str(tmp_path/'out'))
    assert r.returncode==0,r.stderr
    result=json.loads(r.stdout)
    assert 'archive' not in result
    assert Path(result['source_copy']).exists()
    assert Path(result['file']).exists()


def test_failure_keeps_export_triplet(tmp_path):
    src,digest,arch,env=prepare(tmp_path,broken=True)
    r=run(env,'--session-id',SID,'--mode','thread','--fast','--no-progress',
          '--archiver-path',str(arch),'--out-dir',str(tmp_path/'out'))
    assert r.returncode!=0
    files=list((tmp_path/'out').iterdir())
    assert len(list((tmp_path/'out').glob('*.md')))==1
    assert len(list((tmp_path/'out').glob('*.jsonl')))==1
    assert len(list((tmp_path/'out').glob('*.manifest.json')))==1
    assert not [path for path in (tmp_path/'out').glob('*.zip')
                if not path.name.endswith('.review.zip')]
    assert src.exists()


def test_source_copy_disabled_two_member_archive(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    r=run(env,'--session-id',SID,'--mode','thread','--fast','--no-progress','--json',
          '--archiver-path',str(arch),'--no-source-copy','--out-dir',str(tmp_path/'out'))
    assert r.returncode==0,r.stderr
    result=json.loads(r.stdout)
    with zipfile.ZipFile(result['archive']) as z: assert len(z.namelist())==2


def test_archive_default_config_and_override(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    cfg=run(env,'--set-archive-default','archiver='+str(arch),
            '--set-archive-default','level=9', '--set-archive-default','cleanup=false')
    assert cfg.returncode==0,cfg.stderr
    show=run(env,'--show-archive-config');assert show.returncode==0
    assert json.loads(show.stdout)['cleanup'] is False
    r=run(env,'--mode','thread',SID,'--fast','--no-progress','--json',
          '--out-dir',str(tmp_path/'out'))
    assert r.returncode==0,r.stderr
    value=json.loads(r.stdout)
    assert value['loose_files_removed'] is False
    # per-job --archive-cleanup must override persistent cleanup=false.
    second=run(env,'--mode','thread',SID,'--fast','--no-progress','--json',
               '--archive-cleanup','--out-dir',str(tmp_path/'out2'))
    assert second.returncode==0,second.stderr
    assert json.loads(second.stdout)['loose_files_removed'] is True


def test_non_streaming_variant_and_nofile(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    r=run(env,'--session-id',SID,'--mode','last-response','--json',
          '--archiver-path',str(arch),'--out-dir',str(tmp_path/'classic'))
    assert r.returncode==0,r.stderr
    val=json.loads(r.stdout);assert val['archive_verified']
    with zipfile.ZipFile(val['archive']) as z: assert len(z.namelist())==3
    quiet=run(env,'--session-id',SID,'--mode','last-response','--no-file','--json')
    assert quiet.returncode==0,quiet.stderr


def test_redaction_refuses_unredacted_source(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    out=tmp_path/'out'
    run1=run(env,'--mode','thread',SID,'--fast','--no-progress','--redact',
             '--archiver-path',str(arch),'--out-dir',str(out))
    assert run1.returncode != 0
    assert '--no-source-copy' in run1.stderr
    assert not out.exists()


def test_repeat_export_never_overwrites_existing_zip(tmp_path):
    src,digest,arch,env=prepare(tmp_path)
    args=('--session-id',SID,'--mode','thread','--fast','--no-progress','--json',
          '--archiver-path',str(arch),'--filename-template','session_{title}_{mode}.md',
          '--out-dir',str(tmp_path/'out'))
    a=run(env,*args); assert a.returncode==0,a.stderr
    b=run(env,*args); assert b.returncode==0,b.stderr
    first=json.loads(a.stdout)['archive']; second=json.loads(b.stdout)['archive']
    assert first != second
    assert Path(first).exists() and Path(second).exists()
    assert Path(second).stem.endswith('-1')


def test_default_cli_prints_only_one_lzma_archive_with_three_members(tmp_path):
    source, digest, archiver, env = prepare(tmp_path)
    output = tmp_path / 'out'
    completed = run(env, '--mode', 'thread', SID, '--fast', '--no-progress',
                    '--archiver-path', str(archiver), '--out-dir', str(output))
    assert completed.returncode == 0, completed.stderr
    archive_path = Path(completed.stdout.strip())
    assert archive_path.is_file() and archive_path.suffix == '.zip'
    assert not archive_path.name.endswith('.review.zip')
    assert list(output.iterdir()) == [archive_path]
    assert 'Review text files:' not in completed.stderr
    with zipfile.ZipFile(archive_path) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_LZMA}
        assert set(archive.namelist()) == {archive_path.stem + suffix
                                          for suffix in ('.md', '.manifest.json', '.jsonl')}
        assert archive.read(archive_path.stem + '.jsonl') == source.read_bytes()
        details = json.loads(archive.read(archive_path.stem + '.manifest.json'))
        assert details['archive_plan']['method'] == 'LZMA'
        assert details['archive_plan']['level'] == 9


def test_no_review_cli_preserves_full_archive_output(tmp_path):
    source, digest, archiver, env = prepare(tmp_path)
    output = tmp_path / 'out'
    completed = run(env, '--mode', 'thread', SID, '--fast', '--no-progress',
                    '--no-review', '--archiver-path', str(archiver), '--out-dir', str(output))
    assert completed.returncode == 0, completed.stderr
    archive_path = Path(completed.stdout.strip())
    assert archive_path.is_file() and archive_path.suffix == '.zip'
    assert not archive_path.name.endswith('.review.zip')
    assert not list(output.glob('*.review*'))
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read(archive_path.stem + '.jsonl') == source.read_bytes()
    assert source.is_file()
