"""Public diagnostic CLI contract, including non-mutation of export configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def invoke(tmp_path, *args):
    config = tmp_path / 'settings.json'
    env = dict(os.environ, CODEX_EXPORT_CONFIG=str(config), PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, '-m', 'codex_exporter', *map(str, args)],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    return result, config


def test_missing_attachment_receipt_does_not_create_config(tmp_path):
    result, config = invoke(tmp_path, '--diagnose-archive', tmp_path / 'absent.zip')
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt['classification'] == 'path_unavailable'
    assert receipt['runtime']['remote_chatgpt_compatibility'] == 'not_tested'
    assert not config.exists()
    assert list(tmp_path.iterdir()) == []


def test_metadata_cli_and_repeatable_paths_keep_receipts_separate(tmp_path):
    archive = tmp_path / 'example.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_LZMA) as z:
        z.writestr('body.txt', 'data ' * 200)
    result, config = invoke(tmp_path, '--diagnose-archive', archive,
                            '--archive-check', 'metadata')
    receipt = json.loads(result.stdout)
    assert result.returncode == 0
    assert receipt['classification'] == 'metadata_readable'
    assert not receipt['content_verified'] and receipt['archive_sha256'] is None
    result, config = invoke(tmp_path, '--diagnose-archive', archive,
                            '--diagnose-archive', tmp_path / 'absent.zip')
    receipts = json.loads(result.stdout)
    assert result.returncode == 1
    assert [r['classification'] for r in receipts] == ['verified', 'path_unavailable']
    assert not config.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['example.zip']


@pytest.mark.parametrize('arguments', [
    ['--set-archive-default', 'dictionary=64m'],
    ['--jsonl', 'session.jsonl'],
    ['--archive-max-dictionary-mib', '0'],
    ['--repair-access', 'codex_example.zip'],
])
def test_diagnostic_rejects_conflicts_before_any_mutation(tmp_path, arguments):
    result, config = invoke(tmp_path, '--diagnose-archive', 'absent.zip', *arguments)
    assert result.returncode == 2
    assert not config.exists()
    assert list(tmp_path.iterdir()) == []
