# Windows upgrade to Codex Exporter 0.3.10

## Upgrade the interpreter used by your existing helper

If `cdx-thread` calls `Python313\Lib\site-packages\codex_exporter\cli.py`,
upgrade that interpreter. Existing module-routed helpers automatically receive
live snapshots and review packages; no profile edit, reload, or Codex restart is
required for the default command.

Use your existing interpreter if it is installed elsewhere:

```powershell
$ErrorActionPreference = 'Stop'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'Set $py to the Python interpreter used by your existing exporter.'
}

$wheel = 'https://github.com/lowestprime/codex-exporter/releases/download/v0.3.10/codex_exporter-0.3.10-py3-none-any.whl'
& $py -m pip install --upgrade --no-deps $wheel
if ($LASTEXITCODE -ne 0) { throw 'Exporter upgrade failed.' }

$version = (& $py -m codex_exporter --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne 'codex-export 0.3.10') {
    throw "Unexpected exporter version: $version"
}

# Explicitly replace the old LZMA/9 compression preference with portable Deflate.
# Keep all other existing preferences, including the saved external archiver path.
& $py -m codex_exporter --set-archive-default method=Deflate `
    --set-archive-default level=6 --set-archive-default backend=python
if ($LASTEXITCODE -ne 0) { throw 'Could not save portable archive preferences.' }

$configText = & $py -m codex_exporter --show-archive-config
if ($LASTEXITCODE -ne 0) { throw 'Could not verify archive preferences.' }
$config = ($configText | Out-String) | ConvertFrom-Json
if ($config.method -ne 'Deflate' -or $config.level -ne 6 -or $config.backend -ne 'python') {
    throw 'Archive preferences did not match the requested settings.'
}
$version
$config | Format-List
```

The package upgrade itself does not rewrite stored preferences. An existing
explicit `method=LZMA` would otherwise remain active and continue the old
compression cost for the full archive. The three configuration changes above
are intentional. `backend=python` bypasses the old 7-Zip path while retaining it
for later use. The built-in backend does not use LZMA dictionary/word-size or
multi-thread controls. The release also provides `SHA256SUMS.txt` for pinning or
checking downloaded distributions. No new required dependency was added.

## Export a running thread

```powershell
cdx-thread '11111111-2222-4333-8444-555555555555'
```

The printed primary path now ends in `.review.zip`. In the same output folder:

- `<name>.review.zip` contains a portable text-only review package.
- `<name>.review\` retains direct-upload TXT parts, `START_HERE.txt`,
  `RECENT.txt`, and `review-index.json`.
- `<name>.zip` retains the complete ordinary archival export, including the
  captured native JSONL when source-copy is enabled.

Archive cleanup affects only the ordinary loose export files after the full
archive and review package succeed. It does not remove review TXT files.
`RECENT.txt` is a bounded excerpt, not a complete-thread summary. Every rendered
Markdown byte is preserved across the numbered parts, including long lines and
encoded payloads. Review does not restore material omitted by the original
renderer or by Codex itself.

If the receiving chat cannot access a ZIP, attach `START_HERE.txt`, `RECENT.txt`,
and the relevant numbered TXT parts directly. Use the index to retrieve more
history. A successful upload does not mean the entire history fits in a model's
context or has been reviewed.

## Repair an existing export without rerunning its thread

Use the direct Python command so this also works with older PowerShell helpers:

```powershell
$oldZip = 'C:\Users\YourName\Documents\Codex Exports\previous-export.zip'
& $py -m codex_exporter --review-from $oldZip
if ($LASTEXITCODE -ne 0) { throw 'Could not create the review package.' }
```

This reads the existing Markdown and matching manifest with Python's ZIP reader,
which supports the previous LZMA ZIPs. It creates a new review ZIP and text folder
next to the original, without altering it or reading the potentially huge
native JSONL member. Only the selected ZIP members receive CRC validation during
this conversion. Native-source hashes are carried forward as provenance, not
presented as newly verified native bytes. `.md` and `.txt` inputs also work.

`--review-from` preserves the existing rendered selection. It rejects redaction,
record selection, and rendering overrides rather than silently ignoring them.
To create a redacted review, first export a redacted transcript with
`--redact --no-source-copy`; then review that output.

## Direct controls and verification

Existing helpers need no new switches for default review output. Use the direct
CLI for optional controls if the installed helper does not yet expose them:

```powershell
# Ordinary full archival export only; suppress the review companion.
& $py -m codex_exporter --session-id '11111111-2222-4333-8444-555555555555' `
    --mode thread --no-review

# Smaller direct-upload parts; cap includes each UTF-8 header.
& $py -m codex_exporter --review-from $oldZip --review-chunk-bytes 500000

# Exact machine-readable paths and verification states.
$json = & $py -m codex_exporter --session-id '11111111-2222-4333-8444-555555555555' `
    --mode thread --json
if ($LASTEXITCODE -ne 0) { throw 'Export failed.' }
$result = ($json | Out-String) | ConvertFrom-Json
$result | Select-Object review_archive, review_directory, review_part_count, review_verified,
    archive, archive_verified, archive_backend, source_copy_strategy, loose_files_removed
$result.source_snapshot | Format-List
```

The default part limit is 1,000,000 UTF-8 bytes, including the header; accepted
values are 4,096 through 1,500,000. These are byte limits, not exact token counts.
The index records SHA-256, original byte and line ranges, and the header length
needed to reconstruct the exact rendered transcript.

For compact archival storage with a compatible extractor, retain LZMA explicitly:

```powershell
& $py -m codex_exporter --session-id '11111111-2222-4333-8444-555555555555' `
    --mode thread --archive-backend 7zip --archive-method LZMA --archive-level 9
```

The review companion still uses Deflate. Ordinary extractors may not support
LZMA ZIP members.

## Standalone-script installations

Updating a wheel does not replace a separately installed `Export-CodexThread.py`.
For a helper pinned to the standalone script, extract the 0.3.10 source
release and run its installer with the same installation directory/interpreter:

```powershell
# Run from the extracted 0.3.10 source directory.
.\Install-CodexThreadExporter.ps1 -InstallDir 'C:\projects\CodexTools' -PythonPath $py -InstallPackage
```

Omitting `-AppendProfile` preserves the existing profile. The installer backs up
its previous script/package directory. Profile-routing changes are only needed
when deliberately updating which installation a helper invokes. Existing
exports and their original archives are not automatically deleted or replaced.
