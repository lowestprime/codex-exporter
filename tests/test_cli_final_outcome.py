"""Final CLI progress must reflect failures after rendering and before return."""
from __future__ import annotations

from contextlib import ExitStack
import json
import sys

import pytest

from codex_exporter import cli


@pytest.mark.parametrize('late_failure', ['cleanup', 'clipboard'])
def test_cli_does_not_emit_success_before_late_failure(tmp_path, monkeypatch, capsys, late_failure):
    source = tmp_path / 'source.jsonl'
    raw = b'{"type":"event_msg","payload":{"type":"user_message","message":"hello"}}\n'
    source.write_bytes(raw)
    monkeypatch.setenv('CODEX_EXPORT_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setattr(cli, 'codex_home', lambda: tmp_path / 'unused-home')
    argv = ['codex-export', '--jsonl', str(source), '--mode', 'thread', '--no-fast',
            '--no-archive', '--out-dir', str(tmp_path / 'output'), '--no-remember-out-dir',
            '--tokenizer', 'regex', '--progress-style', 'json', '--json']
    if late_failure == 'cleanup':
        class FailingExitStack(ExitStack):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise PermissionError('Synthetic final temporary cleanup failed')
        monkeypatch.setattr(cli, 'ExitStack', FailingExitStack)
    else:
        def clipboard(_):
            raise PermissionError('Synthetic clipboard write failed')
        monkeypatch.setattr(cli, 'copy_to_clipboard', clipboard)
        argv.append('--clipboard')
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(PermissionError, match='Synthetic'):
        cli.main()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{')]
    final = [event for event in events if event['event'] in ('export_complete', 'export_failed')]
    assert len(final) == 1
    assert final[0]['event'] == 'export_failed'
    assert final[0]['detail'] == '1 export(s)'
    assert source.read_bytes() == raw
