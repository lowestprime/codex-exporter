# Windows upgrade to Codex Exporter 0.3.9

## Existing package-routed 0.3.8 installation

If the failing traceback points to
`Python313\Lib\site-packages\codex_exporter\cli.py`, update that same interpreter.
The live snapshot behavior is automatic; no new helper switch or profile edit is
required. Existing PowerShell archive overrides remain compatible.

The following example uses a standard per-user CPython 3.13 installation. Select
your existing interpreter explicitly if it resides elsewhere.

```powershell
$ErrorActionPreference = 'Stop'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'Set $py to the Python interpreter used by your existing exporter.'
}

# Record existing preferences; the package upgrade must not change them.
$before = (& $py -m codex_exporter --show-archive-config | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read the current exporter configuration.' }

$wheel = 'https://github.com/lowestprime/codex-exporter/releases/download/v0.3.9/codex_exporter-0.3.9-py3-none-any.whl'
& $py -m pip install --upgrade --no-deps $wheel
if ($LASTEXITCODE -ne 0) { throw 'Exporter package upgrade failed.' }

$version = (& $py -m codex_exporter --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne 'codex-export 0.3.9') {
    throw "Unexpected exporter version: $version"
}
$after = (& $py -m codex_exporter --show-archive-config | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not verify archive preferences after the upgrade.' }
if ($before -cne $after) { throw 'Archive preferences changed unexpectedly; inspect before exporting.' }

$version
$after
```

`--no-deps` preserves the working optional packages; 0.3.9 introduces no new
required dependency. A fresh installation that needs compressed rollouts or exact
token counts can install the `zstd` and `exact` extras from the source distribution.
The GitHub Release also supplies `SHA256SUMS.txt` for downloaded distributions.

Run the existing command while the target thread remains active:

```powershell
cdx-thread '11111111-2222-4333-8444-555555555555'
```

Or bypass any unqualified command/PATH ambiguity with the selected interpreter:

```powershell
& $py -m codex_exporter --session-id '11111111-2222-4333-8444-555555555555' --mode thread
if ($LASTEXITCODE -ne 0) { throw 'Export failed; inspect the diagnostic above.' }
```

The default successful result is a verified ZIP containing Markdown, a manifest,
and the captured native JSONL. Events appended after the capture boundary are
available to the next export. The live Codex thread continues uninterrupted.

## Inspect snapshot evidence

For a machine-readable result, use the direct CLI:

```powershell
$json = & $py -m codex_exporter --session-id '11111111-2222-4333-8444-555555555555' --mode thread --json
if ($LASTEXITCODE -ne 0) { throw 'Export failed.' }
$result = $json | ConvertFrom-Json
$result | Select-Object archive, archive_verified, source_copy_strategy, loose_files_removed, parse_error_count
$result.source_snapshot | Format-List
```

The ZIP manifest also includes `source_snapshot`. `captured_sha256` identifies
its native JSONL member. A nonzero `deferred_tail_bytes` means the initial cutoff
ended in an unfinished or invalid unterminated record. It does not mean a completed
newline-terminated record was silently dropped.

## Standalone-script installations

If a helper invokes an old `Export-CodexThread.py` directly, updating a wheel alone
does not update that separate script. Extract the 0.3.9 source distribution and run
its installer with the same existing installation directory and interpreter:

```powershell
# Run from the extracted 0.3.9 source directory.
.\Install-CodexThreadExporter.ps1 -InstallDir 'C:\projects\CodexTools' -PythonPath $py -InstallPackage
```

Omitting `-AppendProfile` preserves the existing profile. The installer backs up
its previous script/package directory. Only use `-AppendProfile` when deliberately
updating helper routing; existing package-routed 0.3.8 helpers do not require it.

## Existing files from failed attempts

Previous failed attempts may have left loose `.md` and `.manifest.json` files.
They are not automatically deleted by this upgrade. New exports use the existing
collision policy and never silently replace a completed ZIP. Keep older exports
until their contents are reviewed; no regeneration is required for already valid
archives.
