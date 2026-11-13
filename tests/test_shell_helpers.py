"""Real shell argument forwarding, version routing, and isolated installer checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
PWSH = os.environ.get('CODEX_TEST_PWSH') or shutil.which('pwsh')
BASH_HELPER = ROOT / 'bash' / 'codex-thread-export.sh'
PS_HELPER = ROOT / 'powershell' / 'CodexThreadExport-profile-block.v8.ps1'


def _environment(**extra):
    env = os.environ.copy()
    for key in ('CODEX_THREAD_EXPORTER', 'CODEX_EXPORT_PYTHON', 'CODEX_EXPORT_FALLBACK_SCRIPT'):
        env.pop(key, None)
    env.update(extra)
    return env


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.skipif(not BASH, reason='Bash not installed')
@pytest.mark.parametrize('command', [
    'cdx-sessions', 'cdx-browse', 'cdx-config', 'cdx-tokenizer',
    'cdx-set-dir', 'cdx-set-dir "a directory"',
    'cdx-set-template', 'cdx-set-template "a template"',
    'cdx-latest', 'cdx-latest actions', 'cdx-list session',
    'cdx-responses session', 'cdx-thread session', 'cdx-thread-clip session',
    'cdx-response session', 'cdx-response-block session 2', 'cdx-turn session 2',
    'cdx-last session 2', 'cdx-live session', 'cdx-batch one two',
    'cdx-batch one two --', 'cdx-final session', 'cdx-msg session 2',
    'cdx-range session 2 3', 'cdx-since session "text:exact anchor"',
    'cdx-turns session', 'cdx-project demo', 'cdx-fast session',
    'cdx-audit', 'cdx-chatgpt "some archive.zip"', 'cdx-archive-config',
])
def test_bash_helpers_preserve_custom_options_as_distinct_arguments(command):
    script = (f'source {shlex.quote(str(BASH_HELPER))}\n'
              '_codex_export_run() { printf "%s\\0" "$@"; }\n'
              + command + ' --codex-home "a path with spaces" --archive-max-bytes 123456')
    result = subprocess.run([BASH, '-eu', '-c', script], capture_output=True, check=True)
    args = result.stdout.decode().rstrip('\0').split('\0')
    assert args[-4:] == ['--codex-home', 'a path with spaces', '--archive-max-bytes', '123456']
    assert args.count('--archive-max-bytes') == 1


@pytest.mark.skipif(not BASH, reason='Bash not installed')
def test_bash_since_and_turns_keep_exact_selectors_and_occurrences():
    script = (f'source {shlex.quote(str(BASH_HELPER))}\n'
              '_codex_export_run() { printf "%s\\0" "$@"; }\n'
              'cdx-since session "text:GPU brightness; $(literal)" --since-occurrence 2')
    # The dollar expression is deliberately quoted literally, like a shell user's quoted prompt.
    script = script.replace('"text:GPU brightness; $(literal)"', "'text:GPU brightness; $(literal)'")
    result = subprocess.run([BASH, '-eu', '-c', script], capture_output=True, check=True)
    assert result.stdout.decode().rstrip('\0').split('\0') == [
        '--session-id', 'session', '--mode', 'thread', '--since',
        'text:GPU brightness; $(literal)', '--since-occurrence', '2']
    result = subprocess.run([BASH, '-eu', '-c',
                             f'source {shlex.quote(str(BASH_HELPER))}; cdx-since'],
                            capture_output=True, env=_environment())
    assert result.returncode == 2 and b'Usage:' in result.stderr


def _fake_codes(tmp_path, module_version, script_version):
    module_root = tmp_path / 'modules'
    package = module_root / 'codex_exporter'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('from .cli import __version__\n')
    (package / 'cli.py').write_text(f'__version__ = "{module_version}"\n')
    emit = 'import json, sys\nprint(json.dumps({"route": ROUTE, "args": sys.argv[1:]}))\n'
    (package / '__main__.py').write_text('ROUTE = "module"\n' + emit)
    fallback = tmp_path / 'fallback script.py'
    fallback.write_text(f'__version__ = "{script_version}"\nROUTE = "script"\n' + emit)
    return module_root, fallback


@pytest.mark.parametrize('shell', ['bash', 'powershell'])
@pytest.mark.parametrize('module_version,script_version,explicit,expected', [
    ('0.4.0', '0.3.12', False, 'module'),
    ('0.3.12', '0.4.0', False, 'script'),
    ('0.4.0', '0.4.0', False, 'module'),
    ('0.4.0', '0.3.12', True, 'script'),
])
def test_helpers_choose_upgraded_code_without_overriding_explicit_choice(
        tmp_path, shell, module_version, script_version, explicit, expected):
    if not (BASH if shell == 'bash' else PWSH):
        pytest.skip(f'{shell} not installed')
    module_root, fallback = _fake_codes(tmp_path, module_version, script_version)
    env = _environment(CODEX_EXPORT_PYTHON=sys.executable, PYTHONPATH=str(module_root))
    if explicit:
        env['CODEX_THREAD_EXPORTER'] = str(fallback)
    if shell == 'bash':
        env['CODEX_EXPORT_FALLBACK_SCRIPT'] = str(fallback)
        command = [BASH, '-eu', '-c',
                   f'source {shlex.quote(str(BASH_HELPER))}\n_codex_export_run --probe "a b"']
    else:
        command = [PWSH, '-NoProfile', '-Command',
                   f"$ErrorActionPreference='Stop'; . {_ps_quote(PS_HELPER)}; "
                   f'$script:CodexThreadExporter={_ps_quote(fallback)}; '
                   "Invoke-CodexExporterRaw -ArgumentList @('--probe','a b')"]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'route': expected, 'args': ['--probe', 'a b']}


@pytest.mark.skipif(not PWSH, reason='PowerShell not installed')
def test_powershell_all_helpers_forward_and_all_sources_parse():
    result = subprocess.run([PWSH, '-NoProfile', '-File',
                             str(ROOT / 'powershell' / 'Test-CodexExporterHelpers.ps1')],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    assert 'PASS: base helper forwarding' in result.stdout
    assert 'PASS: overrides helper forwarding' in result.stdout
    command = ("$ErrorActionPreference='Stop'; $failed=@(); "
               "Get-ChildItem -LiteralPath . -Filter *.ps1 -Recurse | "
               "Where-Object FullName -NotMatch '[\\/]\\.venv[\\/]' | ForEach-Object { "
               "$e=$null; $t=$null; [System.Management.Automation.Language.Parser]::ParseFile("
               "$_.FullName,[ref]$t,[ref]$e)>$null; if($e.Count){ $failed += $e } }; "
               "if($failed.Count){ $failed | Out-String | Write-Error; exit 1 }")
    subprocess.run([PWSH, '-NoProfile', '-Command', command], cwd=ROOT,
                   capture_output=True, text=True, check=True)


@pytest.mark.skipif(not BASH, reason='Bash not installed')
def test_bash_installer_copies_modules_and_profile_is_idempotent(tmp_path):
    target = tmp_path / 'installed with spaces'
    profile = tmp_path / 'shell profile'
    profile.write_text('function unrelated() { :; }\n')
    args = [BASH, str(ROOT / 'bash' / 'install.sh'), '--install-dir', str(target), '--profile', str(profile)]
    env = _environment(CODEX_EXPORT_PYTHON=sys.executable)
    subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    before = profile.read_bytes()
    subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert profile.read_bytes() == before
    assert (target / 'codex_exporter' / 'browser.py').is_file()
    result = subprocess.run([BASH, '-eu', '-c', f'source {shlex.quote(str(profile))}; _codex_export_run --version'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.startswith('codex-export ')


@pytest.mark.skipif(not PWSH, reason='PowerShell not installed')
def test_powershell_installer_replaces_late_stale_overrides_and_is_idempotent(tmp_path):
    target = tmp_path / 'installed with spaces'
    profile = tmp_path / 'profile.ps1'
    profile.write_text("function Unrelated { 'kept' }\n# CodexExporter-ArchiveOverrides (v0.3.7)\n"
                       ". 'C:\\old\\CodexExporter-ArchiveOverrides.ps1'\n")
    command = [PWSH, '-NoProfile', '-File', str(ROOT / 'Install-CodexThreadExporter.ps1'),
               '-InstallDir', str(target), '-PythonPath', sys.executable,
               '-ProfilePath', str(profile), '-AppendProfile']
    subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    before = profile.read_bytes()
    subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert profile.read_bytes() == before
    content = profile.read_text(encoding='utf-8-sig')
    assert "function Unrelated { 'kept' }" in content
    assert 'C:\\old\\CodexExporter-ArchiveOverrides.ps1' not in content
    assert content.count('# CodexExporter-ArchiveOverrides') == 1
    assert (target / 'codex_exporter' / 'browser.py').is_file()
    assert (target / 'CodexExporter-ArchiveOverrides.ps1').is_file()


@pytest.mark.skipif(not PWSH, reason='PowerShell not installed')
@pytest.mark.parametrize('newline', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
def test_powershell_legacy_profile_migration_is_literal_and_idempotent(tmp_path, newline):
    """Exercise the CI legacy-region fixture, including its preserved trailing code."""
    target = tmp_path / 'codex exporter custom path' / 'tool'
    profile = tmp_path / 'Microsoft.PowerShell_profile.ps1'
    lines = [
        b"function Existing-ProfileFunction { 'preserved' }",
        b'# Codex Thread Exporter PowerShell helper functions',
        b"function Legacy-CodexHelper { 'replace me' }",
        b'# List all functions defined in this profile script',
        b"function Existing-TrailingFunction { 'also preserved' }",
    ]
    profile.write_bytes(newline.join(lines) + newline)
    command = [PWSH, '-NoProfile', '-File', str(ROOT / 'Install-CodexThreadExporter.ps1'),
               '-InstallDir', str(target), '-PythonPath', sys.executable,
               '-ProfilePath', str(profile), '-AppendProfile']
    subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    first = profile.read_bytes()
    assert b'Legacy-CodexHelper' not in first
    assert first.count(b'Existing-ProfileFunction') == 1
    assert first.count(b'Existing-TrailingFunction') == 1
    assert first.count(b'# >>> Codex Thread Exporter helpers >>>') == 1
    assert first.endswith(newline.join(lines[-2:]) + newline)
    backups = sorted(tmp_path.glob(profile.name + '.codex-exporter-backup-*'))
    assert len(backups) == 1

    # The first migration must produce usable helpers, not merely repair itself on rerun.
    check = ("$ErrorActionPreference='Stop'; . " + _ps_quote(profile) + '; '
             'function Invoke-CodexExporterRaw { param([object[]]$ArgumentList); '
             'ConvertTo-Json -InputObject @($ArgumentList) -Compress }; '
             "Invoke-CodexThreadExport 'synthetic-session' --since-text 'exact anchor'")
    invocation = subprocess.run([PWSH, '-NoProfile', '-Command', check], cwd=tmp_path,
                                capture_output=True, text=True, check=True)
    forwarded = json.loads(invocation.stdout)
    assert forwarded[forwarded.index('--mode') + 1] == 'thread'
    assert forwarded[forwarded.index('--since-text') + 1] == 'exact anchor'

    subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert profile.read_bytes() == first
    assert sorted(tmp_path.glob(profile.name + '.codex-exporter-backup-*')) == backups
