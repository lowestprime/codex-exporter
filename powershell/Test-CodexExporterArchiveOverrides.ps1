# Isolated no-export smoke test of the nine cdx-* archive and review wrappers.
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

$id = '11111111-2222-4333-8444-555555555555'
$binary = 'C:\Program Files\7-Zip-Zstandard\7z.exe'
Assert-Call 'cdx-thread + named archive overrides' {
    cdx-thread $id -NoArchiveCleanup -ArchiveLevel 7 -ArchiveThreads 8 -ArchiverPath $binary
} @('--session-id',$id,'--mode','thread','--no-archive-cleanup','--archive-level','7',
    '--archive-threads','8','--archiver-path',$binary) @('--archive-cleanup','--no-source-copy')
Assert-Call 'cdx-thread with no overrides' {
    cdx-thread $id
} @('--session-id',$id,'--mode','thread') @('--no-archive','--no-archive-cleanup','--archive-level',
    '--review','--no-review','--review-chunk-bytes','--archive-backend')
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

# Every wrapper accepts the same review controls without replacing its mode options.
$reviewCases = @(
    @{ Name='cdx-thread'; Call={ param($Options) cdx-thread $id @Options } },
    @{ Name='cdx-response'; Call={ param($Options) cdx-response $id @Options } },
    @{ Name='cdx-project'; Call={ param($Options) cdx-project 'Full-Time' @Options } },
    @{ Name='cdx-range'; Call={ param($Options) cdx-range $id 1 2 @Options } },
    @{ Name='cdx-batch'; Call={ param($Options) cdx-batch @($id) @Options } },
    @{ Name='cdx-turn'; Call={ param($Options) cdx-turn $id 1 @Options } },
    @{ Name='cdx-last'; Call={ param($Options) cdx-last $id 2 @Options } },
    @{ Name='cdx-final'; Call={ param($Options) cdx-final $id @Options } },
    @{ Name='cdx-fast'; Call={ param($Options) cdx-fast $id @Options } }
)
foreach ($case in $reviewCases) {
    $invokeCase = $case.Call
    Assert-Call "$($case.Name) review controls" {
        & $invokeCase @{ Review=$true; ReviewChunkBytes=4096; ArchiveBackend='PYTHON' }
    } @('--review','--review-chunk-bytes','4096','--archive-backend','python') @('--no-review')
    Assert-Call "$($case.Name) no-review controls" {
        & $invokeCase @{ NoReview=$true; ReviewChunkBytes=1500000; ArchiveBackend='7zip' }
    } @('--no-review','--review-chunk-bytes','1500000','--archive-backend','7zip') @('--review')
}
Assert-Call 'False review switches leave Python defaults in force' {
    cdx-thread $id -Review:$false -NoReview:$false -ArchiveBackend auto
} @('--session-id',$id,'--archive-backend','auto') @('--review','--no-review','--review-chunk-bytes')

function Assert-Rejected {
    param([string]$Name, [scriptblock]$Call)
    $script:Seen = @()
    $rejected = $false
    try { & $Call } catch { $rejected = $true }
    if (-not $rejected) { throw "$Name should have failed validation." }
    if ($script:Seen.Count -ne 0) { throw "$Name reached the invoker despite invalid arguments." }
    Write-Host "PASS: $Name"
}
Assert-Rejected 'Review part size below minimum' { cdx-thread $id -ReviewChunkBytes 4095 }
Assert-Rejected 'Review part size above maximum' { cdx-thread $id -ReviewChunkBytes 1500001 }
Assert-Rejected 'Unknown archive backend' { cdx-thread $id -ArchiveBackend unsupported }
Assert-Rejected 'Conflicting review switches' { cdx-thread $id -Review -NoReview }
Write-Host 'PASS: All archive and review wrapper checks completed without executing Codex.'
