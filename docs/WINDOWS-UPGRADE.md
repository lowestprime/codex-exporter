# Windows upgrade to Codex Exporter 0.3.11

## Upgrade the interpreter used by the existing helper

If `cdx-thread` calls `Python313\Lib\site-packages\codex_exporter\cli.py`,
upgrade that interpreter. An unrelated Python or `uv` environment does not update
that installation. Existing module-routed helpers receive the new package
behavior without restarting Codex. Separately copied scripts need updating too.

These commands restore the requested LZMA settings explicitly, including any
preferences changed to Deflate by the 0.3.10 upgrade instructions. Adjust `$py`
and `$archiver` only if those tools are installed elsewhere. The quoted archiver
path is intentional: a WindowsApps alias or another `7z` on PATH may identify a
different executable.

```powershell
$ErrorActionPreference = 'Stop'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
$archiver = 'C:\Program Files\7-Zip-Zstandard\7z.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'Set $py to the interpreter used by the existing exporter.'
}
if (-not (Test-Path -LiteralPath $archiver -PathType Leaf)) {
    throw 'Set $archiver to the installed compatible 7-Zip CLI.'
}

$wheel = 'https://github.com/lowestprime/codex-exporter/releases/download/v0.3.11/codex_exporter-0.3.11-py3-none-any.whl'
& $py -m pip install --upgrade --no-deps $wheel
if ($LASTEXITCODE -ne 0) { throw 'Exporter upgrade failed.' }

$version = (& $py -m codex_exporter --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne 'codex-export 0.3.11') {
    throw "Unexpected exporter version: $version"
}

& $py -m codex_exporter `
    --set-archive-default archive=true `
    --set-archive-default "archiver=$archiver" `
    --set-archive-default backend=auto `
    --set-archive-default method=LZMA `
    --set-archive-default level=9 `
    --set-archive-default dictionary=256m `
    --set-archive-default word_size=64 `
    --set-archive-default threads=19 `
    --set-archive-default cleanup=true `
    --set-archive-default copy_source=true `
    --set-archive-default review=false
if ($LASTEXITCODE -ne 0) { throw 'Could not save the requested archive preferences.' }

$configText = & $py -m codex_exporter --show-archive-config
if ($LASTEXITCODE -ne 0) { throw 'Could not verify archive preferences.' }
$config = ($configText | Out-String) | ConvertFrom-Json
$expected = @{
    archive = $true; archiver = $archiver; backend = 'auto'; method = 'LZMA'
    level = 9; dictionary = '256m'; word_size = 64; threads = 19
    cleanup = $true; copy_source = $true; review = $false
}
foreach ($key in $expected.Keys) {
    if ($config.$key -ne $expected[$key]) {
        throw "Archive preference did not match: $key"
    }
}
$version
$config | Format-List
```

The package upgrade does not silently rewrite saved choices; the configuration
commands above make these changes deliberately. The new `review_chunk_bytes`
setting is also displayed; it defaults to 1,000,000 and has no effect until review
content is requested. The release includes `SHA256SUMS.txt` for verifying the
actual downloaded wheel or constructing a SHA-256-pinned installation URL.
No new required Python dependency is introduced.

`threads=19` requests that backend limit; actual LZMA worker utilization, memory,
compression ratio, and duration depend on the implementation and input. The
exporter does not substitute Deflate silently or claim that all phases run on
19 CPU threads.

## Export a running thread

```powershell
cdx-thread '11111111-2222-4333-8444-555555555555'
```

The printed path ends in the ordinary `.zip`. After successful cleanup the
export job leaves **one archive containing three files**:

1. `<stem>.md`: the rendered transcript;
2. `<stem>.manifest.json`: metadata and integrity/provenance records;
3. `<stem>.jsonl`: the captured native JSONL.

No `.review.zip` or `.review` directory is created by default. Old files from
previous releases are not deleted by upgrading or exporting. The three-file
archive applies to the default source-copy/manifest/archive settings; explicit
output options can alter that inventory.

Codex can continue appending during export. The archive represents the verified
complete-record prefix captured at the beginning of that invocation. Later
records belong to a subsequent export. The original rollout remains untouched.

## Diagnose and repair access to an older export

An access-denied message concerns operating-system access, not proof of damaged
ZIP data. Before recreating a large export, inspect the exact affected file:

```powershell
$oldZip = 'C:\Exports\codex_example.zip'
& $py -m codex_exporter --diagnose-access $oldZip
& $py -m codex_exporter --repair-access $oldZip
if ($LASTEXITCODE -ne 0) { throw 'Inspect the reported access-repair result.' }
```

For an explicitly selected old review directory:

```powershell
$oldReview = 'C:\Exports\codex_example.review'
& $py -m codex_exporter --diagnose-access $oldReview
& $py -m codex_exporter --repair-access $oldReview
if ($LASTEXITCODE -ne 0) { throw 'Inspect the reported review-access repair result.' }
```

Both switches are repeatable and return JSON. Diagnostics attempt a real read
and include owner SID, ACL protection/inheritance, and parent ACL. Repair resets
only recognized `codex_` exports to their existing parent's inherited ACL. A
selected `.review` directory is prevalidated and its flat recognized members
are processed individually. Wildcards, general output directories, `.codex`
source paths, links/junctions, multiply linked files, and unexpected members are
refused. Custom filename-template outputs can be diagnosed but are outside this
limited repair scope.

Repair does not change ownership, add Everyone permissions, change the output
parent, or elevate itself. The caller needs permission to change the selected
DACL; the account/token that created a protected export may be required. The
parent must already grant access to the intended user. A failure remains visible
in JSON and the exit status; no broader permission reset is attempted.

The publication fix creates fresh Windows destination files, which inherit the
output directory's ACL, and verifies copied bytes before cleanup. This removes
the staging-ACL transfer caused by hard links and renames. A matching mechanism
is documented in [CPython issue #157876](https://github.com/python/cpython/issues/157876);
the actual owner/DACL for a particular failed export must be observed separately.
Microsoft documents [creation-time inheritance](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights),
[shared hard-link security descriptors](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw),
and [the scoped `icacls /reset` operation](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls).

## Progress and optional review text

```powershell
# A capable interactive terminal receives bars, icons, colors, and measured rates.
& $py -m codex_exporter --session-id UUID --mode thread --progress-style auto
# Preserve stdout JSON and save machine-readable progress separately on stderr.
& $py -m codex_exporter --session-id UUID --mode thread --json `
    --progress-style json 2> progress.jsonl
# Add review TXT parts/index/guide inside the same ordinary ZIP.
& $py -m codex_exporter --session-id UUID --mode thread --review
# Explicitly suppress progress.
& $py -m codex_exporter --session-id UUID --mode thread --no-progress
```

`auto`, `rich`, `plain`, `json`, and `off` are available. `rich` uses the built-in
terminal renderer without a new dependency; redirection and unsupported
terminals fall back safely. Snapshot, rendering, compression, independent member verification, publication,
and cleanup are distinct stages. Windows publication
reports source checksum, copy, and readback independently. 7-Zip percentages
reflect actual compression reports. Python independently decodes and hashes every
archive member with measured byte counters; no redundant `7z t` pass is required.
Unknown-duration stages do not show invented percentage or byte rates.

The updated PowerShell wrapper exposes `-ProgressStyle`; an already loaded or
older wrapper can use the direct Python switches above. Existing `cdx-thread`
default commands do not require new switches.

`--review` keeps bounded review parts inside the same ZIP during an archived
export. Explicit `--review --no-archive` requests only a verified `.review`
text directory alongside the loose core export; no ZIP is created.
`--no-archive` alone creates no review output. For direct TXT files and a separate Deflate package, explicitly convert
an existing ZIP/MD/TXT:

```powershell
& $py -m codex_exporter --review-from $oldZip --review-chunk-bytes 1000000
if ($LASTEXITCODE -ne 0) { throw 'Could not create the requested standalone review package.' }
```

Add `--no-archive` to `--review-from` for a verified text directory without a ZIP.
The converter preserves the original and reads only the Markdown and matching manifest
from a ZIP, with CRC checks. Native-source hashes are carried forward as
provenance; the converter does not reread the native JSONL. Numbered parts
preserve the rendered Markdown exactly, including long lines and encoded
payloads. Their configured limit is UTF-8 bytes including the header, not tokens.
`RECENT.txt` is a bounded excerpt. Successful extraction or upload does not
establish that a model has reviewed all history.

## Standalone-script installations

Updating a wheel does not replace a separately copied `Export-CodexThread.py`.
From the extracted 0.3.11 source release, reinstall with the same directory and
interpreter:

```powershell
.\Install-CodexThreadExporter.ps1 -InstallDir 'C:\projects\CodexTools' `
    -PythonPath $py -InstallPackage
```

Omitting `-AppendProfile` preserves the existing profile. The installer backs up
its previous script/package directory. Reload/update a profile only when
intentionally changing helper routing or exposing new helper parameters.
Existing exports are not automatically removed, replaced, or permission-repaired.
