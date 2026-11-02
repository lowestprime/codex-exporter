# Isolated no-export smoke test of the nine corrected cdx-* archive wrappers.
# Run using: & pwsh -NoProfile -File <this-script> -Overrides <installed override file>
# It creates no archives, modifies no profile files, and calls no Python executable.
[CmdletBinding()]
param(
    [string]$Overrides = (Join-Path $PSScriptRoot 'CodexExporter-ArchiveOverrides.ps1')
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Overrides -PathType Leaf)) { throw "Missing: $Overrides" }

$script:Seen = @()
function Invoke-CodexExporterRaw {
    param([object[]]$ArgumentList)
    $script:Seen = @($ArgumentList | ForEach-Object { [string]$_ })
}
. $Overrides

function Assert-Call {
    param([string]$Name, [scriptblock]$Call, [string[]]$Must, [string[]]$MustNot)
    $script:Seen = @()
    & $Call
    if ($script:Seen.Count -eq 0) { throw "$Name did not call the invoker." }
    foreach ($arg in $Must) {
        if ($script:Seen -cnotcontains $arg) {
            throw "$Name missing '$arg': $($script:Seen -join ' ')"
        }
    }
    foreach ($arg in $MustNot) {
        if ($script:Seen -ccontains $arg) {
            throw "$Name unexpectedly forwarded '$arg'."
        }
    }
    Write-Host "PASS: $Name"
}

$id = '019db64d-c0d1-7140-95a5-2ba248ee69e7'
$binary = 'C:\Program Files\7-Zip-Zstandard\7z.exe'
Assert-Call 'cdx-thread + named archive overrides' {
    cdx-thread $id -NoArchiveCleanup -ArchiveLevel 7 -ArchiveThreads 8 -ArchiverPath $binary
} @('--session-id',$id,'--mode','thread','--no-archive-cleanup','--archive-level','7',
    '--archive-threads','8','--archiver-path',$binary) @('--archive-cleanup','--no-source-copy')
Assert-Call 'cdx-thread with no overrides' {
    cdx-thread $id
} @('--session-id',$id,'--mode','thread') @('--no-archive','--no-archive-cleanup','--archive-level')
Assert-Call 'cdx-response' {
    cdx-response $id -NoArchive
} @('--mode','last-response','--no-archive') @('--archive')
Assert-Call 'cdx-project' {
    cdx-project 'Full-Time' -NoArchiveCleanup
} @('--project','Full-Time','--no-archive-cleanup') @('--no-archive')
Assert-Call 'cdx-range' {
    cdx-range $id 1 2 -NoSourceCopy
} @('--mode','range','--from-message','1','--to-message','2','--no-source-copy') @('--source-copy')
Assert-Call 'cdx-batch' {
    cdx-batch @($id) -ArchiveLevel 7
} @('--include-session-short-id','--archive-level','7') @('--no-archive')
Assert-Call 'cdx-turn' {
    cdx-turn $id 1 -NoArchiveCleanup
} @('--mode','turn','--response','1','--no-archive-cleanup') @('--archive-cleanup')
Assert-Call 'cdx-last' {
    cdx-last $id 2 -ArchiveThreads 3
} @('--mode','thread','--last-n-turns','2','--archive-threads','3') @('--no-archive')
Assert-Call 'cdx-final' {
    cdx-final $id -NoArchiveCleanup
} @('--mode','last-substantial','--no-archive-cleanup') @('--archive-cleanup')
Assert-Call 'cdx-fast' {
    cdx-fast $id -NoProgress -ArchiverPath $binary
} @('--fast','--no-progress','--archiver-path',$binary) @('--no-archive')
Write-Host 'PASS: All ten wrapper invocations forwarded expected arguments without executing Codex.'
