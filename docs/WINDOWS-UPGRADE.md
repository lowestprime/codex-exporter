# Windows upgrade to Codex Exporter 0.4.0

Upgrade both the Python package and the PowerShell helpers. A wheel updates the
exporter, while the source release supplies the installer and the new
`cdx-since`, `cdx-turns`, and browser helpers. The main installer also installs
and loads the current archive overrides; a second archive-override installation
is unnecessary.

Run the following in a normal PowerShell 7 session for your account. Select the
same Python interpreter used by the existing exporter. The example uses the
Python 3.13 and 7-Zip-Zstandard paths from the reported Windows installation.
It installs helper files in a directory under your own LocalAppData and updates
`$PROFILE.CurrentUserCurrentHost`, preserving unrelated profile content and
backing up the exporter files/profile entries it replaces.

## Download, verify, and install the wheel and helpers

Use the wheel and source distribution from the **same v0.4.0 release**. The code
checks both downloaded artifacts against that release's `SHA256SUMS.txt` before
installing or extracting them. A checksum obtained from the same release verifies
consistency with the published artifact list; it is not an independent
signature or independent proof of publisher identity. For a deployment supplied
with separately verified pinned hashes, compare against those exact hashes.

```powershell
$ErrorActionPreference = 'Stop'
$releaseVersion = '0.4.0'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
$archiver = 'C:\Program Files\7-Zip-Zstandard\7z.exe'
$installDir = Join-Path $env:LOCALAPPDATA 'codex-exporter\tools'
$profilePath = $PROFILE.CurrentUserCurrentHost
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'Set $py to the interpreter used by the existing exporter.'
}
if (-not (Test-Path -LiteralPath $archiver -PathType Leaf)) {
    throw 'Set $archiver to the installed compatible 7-Zip CLI.'
}

$releaseBase = "https://github.com/lowestprime/codex-exporter/releases/download/v$releaseVersion"
$wheelName = "codex_exporter-$releaseVersion-py3-none-any.whl"
$sourceName = "codex_exporter-$releaseVersion.tar.gz"
$work = Join-Path $env:TEMP ('codex-exporter-upgrade-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
foreach ($assetName in @($wheelName, $sourceName, 'SHA256SUMS.txt')) {
    Invoke-WebRequest -Uri "$releaseBase/$assetName" -OutFile (Join-Path $work $assetName)
}

$expectedHashes = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $work 'SHA256SUMS.txt')) {
    if ($line -match '^(?<hash>[A-Fa-f0-9]{64})\s+\*?(?<name>\S+)$') {
        $assetName = $Matches.name
        if ($expectedHashes.ContainsKey($assetName)) { throw "Duplicate checksum entry: $assetName" }
        $expectedHashes[$assetName] = $Matches.hash
    }
}
foreach ($assetName in @($wheelName, $sourceName)) {
    if (-not $expectedHashes.ContainsKey($assetName)) { throw "Missing checksum: $assetName" }
    $actualHash = (Get-FileHash -LiteralPath (Join-Path $work $assetName) -Algorithm SHA256).Hash
    if ($actualHash -ine $expectedHashes[$assetName]) { throw "Checksum mismatch: $assetName" }
}

$wheel = Join-Path $work $wheelName
$source = Join-Path $work $sourceName
$extractDir = Join-Path $work 'source'
New-Item -ItemType Directory -Path $extractDir | Out-Null

# Work outside an old checkout so its local package cannot shadow the installed wheel.
Push-Location $work
try {
    & $py -m pip install --upgrade --force-reinstall --no-deps $wheel
    if ($LASTEXITCODE -ne 0) { throw 'Exporter wheel installation failed.' }

    # Python 3.13's data filter rejects unsafe tar extraction paths and special files.
    @'
import sys, tarfile
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    archive.extractall(sys.argv[2], filter='data')
'@ | & $py - $source $extractDir
    if ($LASTEXITCODE -ne 0) { throw 'Source-release extraction failed.' }

    $sourceRoots = @(Get-ChildItem -LiteralPath $extractDir -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'Install-CodexThreadExporter.ps1') -PathType Leaf
    })
    if ($sourceRoots.Count -ne 1) { throw 'Expected one source-release directory containing the installer.' }
    $installer = Join-Path $sourceRoots[0].FullName 'Install-CodexThreadExporter.ps1'

    # The verified wheel is already installed. Do not rebuild/reinstall it from the sdist.
    & $installer -InstallDir $installDir -PythonPath $py -ProfilePath $profilePath -AppendProfile

    # Load only these helpers into the current session; a new session loads the updated profile.
    $installedHelper = Join-Path $installDir 'CodexThreadExport-profile-block.v8.ps1'
    . $installedHelper

    $packageVersion = (& $py -m codex_exporter --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $packageVersion -ne "codex-export $releaseVersion") {
        throw "Unexpected package version: $packageVersion"
    }
    $helperVersion = (Invoke-CodexExporterRaw -ArgumentList @('--version') | Out-String).Trim()
    if ($helperVersion -ne $packageVersion) {
        throw "Helper routing differs: $helperVersion. Inspect CODEX_EXPORT_PYTHON and CODEX_THREAD_EXPORTER."
    }
    foreach ($helperName in @('cdx-since', 'cdx-turns', 'cdx-browse')) {
        Get-Command $helperName -CommandType Function -ErrorAction Stop | Out-Null
    }
    Write-Host "PASS: $packageVersion and current helpers installed."
    Write-Host "Profile: $profilePath"
    Write-Host "Verified release files: $work"
} finally {
    Pop-Location
}
```

The installer replaces its marked helper region and recognized legacy archive
loader entries, so an old late loader does not replace the newly installed
functions. It includes the full standalone Python module directory as a fallback.
The profile records the selected Python path. Helper routing prefers the newer
installed package or standalone copy, using the package when versions match.

Explicit `CODEX_EXPORT_PYTHON` and `CODEX_THREAD_EXPORTER` settings remain
intentional overrides. If the version check reports a mismatch, inspect where
those variables are set and update the conflicting override to the intended
interpreter/script. The installer does not silently remove user overrides,
elevate itself, or change Machine PATH. An independently defined helper later in
an unmarked part of a custom profile remains under that profile's control.

An unqualified `codex-export` command can still resolve to a different Python's
console executable on PATH. The commands above verify the chosen package and
`cdx-*` routing directly. Inspect `Get-Command codex-export -All` when needed;
[explicit PATH repair](../README.md#windows-path-conflict-repair-opt-in) remains
available separately.

## Restore the requested LZMA policy and size ceiling

Package installation preserves saved settings. If an earlier workaround selected
Deflate, restore LZMA explicitly. These commands also set the hard maximum to
512,000,000 bytes and keep default output to the ordinary archive set.

```powershell
& $py -m codex_exporter `
    --set-archive-default archive=true `
    --set-archive-default "archiver=$archiver" `
    --set-archive-default backend=auto `
    --set-archive-default method=LZMA `
    --set-archive-default level=9 `
    --set-archive-default dictionary=256m `
    --set-archive-default word_size=64 `
    --set-archive-default threads=19 `
    --set-archive-default max_bytes=512000000 `
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
    max_bytes = 512000000; cleanup = $true; copy_source = $true; review = $false
}
foreach ($key in $expected.Keys) {
    if ($config.$key -ne $expected[$key]) { throw "Archive preference did not match: $key" }
}
$config | Format-List
```

For a smaller per-export cap, append `--archive-max-bytes N`, or use
`-ArchiveMaxBytes N` on the updated archive wrappers. Accepted values are
16,384–512,000,000 bytes. The configured LZMA parameters are retained; the exporter
does not silently substitute Deflate. Requested threads are a backend limit,
not a promise that every phase uses that many workers.

## Select and export the required context

```powershell
# Discover exact native user-turn anchors before selecting one.
cdx-turns 'SESSION-ID'
cdx-since 'SESSION-ID' 'text:distinct phrase from the user prompt'
cdx-since 'SESSION-ID' 'turn:42'
cdx-since 'SESSION-ID' 'id:recorded-turn-or-message-id'
cdx-since 'SESSION-ID' 'time:2026-11-13T09:30:00-08:00'
cdx-since 'SESSION-ID' 'text:repeated phrase' -Occurrence 2
# Full history, last-N turns, one exact turn, or interactive session selection.
cdx-thread 'SESSION-ID'
cdx-last 'SESSION-ID' 5
cdx-turn 'SESSION-ID' 42
cdx-browse --browse-query 'project words' --browse-status available
```

A since export includes the selected user prompt and all later native events
through the captured snapshot end. Its Markdown and native JSONL use that selected
scope. Text ambiguity produces candidate turns instead of choosing arbitrarily.
Clock timestamps require an explicit offset or `Z`; a date alone means local
midnight. Read the [selection and browser guide](BOUNDED-EXPORTS-AND-SELECTION.md)
for prompt matching, retained metadata, compaction records, and all controls.

With the default source-copy/manifest/archive settings, a fitting single-session
export produces one ZIP containing its MD, manifest, and matching JSONL. Oversized
exports produce independently readable `.partNNNN-of-NNNN.zip` files, each at most
512,000,000 bytes. These are complete ZIPs, not split ZIP volumes. Multiple selected
sessions share one bounded archive set and an internal `BATCH_INDEX.json`; use
`--separate-archives` for a separate bounded set per session. Every final ZIP path
is printed on stdout. Progress and browser UI use stderr.

No review archive or folder is generated by default. Successful cleanup removes
only the generated loose export members after complete verification/publication.
Old exports remain intact. The live rollout remains untouched; later appended
events belong to the next export. Every overflow part contains reconstruction
metadata for original files that needed segmentation. A `.bytes` fragment is
explicitly not a standalone JSON record.

## Progress and optional review text

```powershell
cdx-thread 'SESSION-ID' -ProgressStyle auto
cdx-thread 'SESSION-ID' -ProgressStyle plain
cdx-thread 'SESSION-ID' -NoProgress
# Preserve JSON results and save stage events separately.
& $py -m codex_exporter --session-id 'SESSION-ID' --mode thread --json `
    --progress-style json 2> progress.jsonl
# Include bounded review TXT parts and navigation inside the same archive set.
cdx-thread 'SESSION-ID' -Review
```

Progress reports capture, indexing/selection, rendering, staging, compression,
verification, publication, and cleanup separately. Compression percentages are
those reported by the backend. Unknown-duration work displays elapsed time;
percentages and throughput are not fabricated.

`--review` adds review content to the ordinary archive set. Explicit
`--review --no-archive` creates a verified review text directory alongside loose
exports. `--review-from PATH` is a separate conversion request: it preserves an
existing Markdown/export ZIP and produces bounded review archives plus a retained
review directory. Add `--no-archive` for only the review directory. This standalone
converter retains its explicit Deflate behavior; ordinary exports retain the
configured LZMA policy.

## Diagnose access or receiving-runtime failures

```powershell
& $py -m codex_exporter --diagnose-access 'C:\Exports\codex_example.zip'
& $py -m codex_exporter --diagnose-archive 'C:\Exports\codex_example.zip'
# Explicit repair of a recognized older exporter artifact, only when needed:
& $py -m codex_exporter --repair-access 'C:\Exports\codex_example.zip'
if ($LASTEXITCODE -ne 0) { throw 'Inspect the reported access-repair result.' }
```

Access diagnostics inspect actual reading and Windows permissions. Archive
diagnostics separately inspect ZIP structure, methods, hashes, CRCs, and decoder
capabilities in the runtime where they run. A screenshot of an access error does
not itself establish corruption or a compression-method failure.

Scoped access repair restores an explicitly selected recognized export's
parent-inherited ACL. It does not take ownership, add Everyone access, modify the
live source, or elevate itself. The output parent must already grant appropriate
access. Custom filenames can be diagnosed but may fall outside the recognized
repair scope. Broad directories, links/junctions, and unexpected review contents
are rejected.

The publication path creates fresh Windows destination files, which inherit
permissions from the output directory, then verifies their bytes. It avoids
transferring private staging ACLs through a move or hard link. See the
[access and publication discussion](../README.md#windows-access-permissions-and-existing-exports)
and [archive diagnostics guide](ARCHIVE-DIAGNOSTICS.md).

A valid archive within the size ceiling does not guarantee that a receiving
ChatGPT conversation materializes the upload, supports its decoder, parses every
member, or reads all its contents. Diagnose the actual receiving runtime and use
matching native excerpts or optional bounded review text for the needed scope.
