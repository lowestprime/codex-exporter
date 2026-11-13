# Isolated helper validation; captures arguments and never exports or edits a real profile.
[CmdletBinding()]
param([string]$Helper = (Join-Path $PSScriptRoot 'CodexThreadExport-profile-block.v8.ps1'),
      [string]$Overrides = (Join-Path $PSScriptRoot 'CodexExporter-ArchiveOverrides.ps1'))
$ErrorActionPreference = 'Stop'
. $Helper
function Invoke-CodexExporterRaw {
    param([object[]]$ArgumentList)
    $script:Seen = @($ArgumentList | ForEach-Object { [string]$_ })
}
$id = '11111111-2222-4333-8444-555555555555'
$homeOption = 'C:\A directory with spaces\Codex'
function Assert-Forward {
    param([string]$Name, [scriptblock]$Call, [string[]]$Expected)
    $script:Seen = @()
    & $Call
    foreach ($value in $Expected) {
        if ($script:Seen -cnotcontains $value) { throw "$Name did not forward '$value': $script:Seen" }
    }
    if (@($script:Seen | Where-Object { $_ -ceq '--codex-home' }).Count -ne 1) {
        throw "$Name duplicated or dropped custom flags: $script:Seen"
    }
    if ($script:Seen[-2] -cne '--codex-home' -or $script:Seen[-1] -cne $homeOption) {
        throw "$Name changed the last option or a path with spaces: $script:Seen"
    }
}
$cases = @(
    @{ Name='sessions'; Call={ cdx-sessions --codex-home $homeOption } },
    @{ Name='browse'; Call={ cdx-browse --browse-query 'anchor words' --browse-page-size 5 --codex-home $homeOption } },
    @{ Name='config'; Call={ cdx-config --codex-home $homeOption } },
    @{ Name='tokenizer'; Call={ cdx-tokenizer --codex-home $homeOption } },
    @{ Name='set template'; Call={ cdx-set-template 'codex_{title}.md' --codex-home $homeOption } },
    @{ Name='print template'; Call={ cdx-set-template --codex-home $homeOption } },
    @{ Name='set directory'; Call={ cdx-set-dir 'D:\Exports here' --codex-home $homeOption } },
    @{ Name='choose directory'; Call={ cdx-set-dir --codex-home $homeOption } },
    @{ Name='latest'; Call={ cdx-latest --codex-home $homeOption } },
    @{ Name='latest with mode'; Call={ cdx-latest actions --codex-home $homeOption } },
    @{ Name='list'; Call={ cdx-list $id --codex-home $homeOption } },
    @{ Name='responses'; Call={ cdx-responses $id --codex-home $homeOption } },
    @{ Name='thread'; Call={ cdx-thread $id --since-text 'anchor words' --archive-max-bytes 123456 --codex-home $homeOption } },
    @{ Name='thread clipboard'; Call={ cdx-thread-clip $id --codex-home $homeOption } },
    @{ Name='response'; Call={ cdx-response $id --codex-home $homeOption } },
    @{ Name='response block'; Call={ cdx-response-block $id 2 --codex-home $homeOption } },
    @{ Name='turn'; Call={ cdx-turn $id 2 --codex-home $homeOption } },
    @{ Name='last'; Call={ cdx-last $id 2 --codex-home $homeOption } },
    @{ Name='live'; Call={ cdx-live $id --codex-home $homeOption } },
    @{ Name='batch'; Call={ cdx-batch @($id,'another') --codex-home $homeOption } },
    @{ Name='final'; Call={ cdx-final $id --codex-home $homeOption } },
    @{ Name='message'; Call={ cdx-msg $id 2 --codex-home $homeOption } },
    @{ Name='range'; Call={ cdx-range $id 2 4 --codex-home $homeOption } },
    @{ Name='project'; Call={ cdx-project demo --codex-home $homeOption } },
    @{ Name='audit'; Call={ cdx-audit --codex-home $homeOption } },
    @{ Name='audit project'; Call={ cdx-audit demo --codex-home $homeOption } },
    @{ Name='fast'; Call={ cdx-fast $id --codex-home $homeOption } },
    @{ Name='chatgpt import'; Call={ cdx-chatgpt 'export with spaces.zip' --codex-home $homeOption } },
    @{ Name='since'; Call={ cdx-since $id 'text:anchor words' -Occurrence 2 -ArchiveMaxBytes 123456 --codex-home $homeOption } },
    @{ Name='turn listing'; Call={ cdx-turns $id --codex-home $homeOption } }
)
foreach ($stage in @('base','overrides')) {
    if ($stage -eq 'overrides') { . $Overrides }
    foreach ($case in $cases) { Assert-Forward "$stage $($case.Name)" $case.Call @('--codex-home',$homeOption) }
    Assert-Forward "$stage since anchor and cap" {
        cdx-since $id 'text:anchor words' -Occurrence 2 -ArchiveMaxBytes 123456 --codex-home $homeOption
    } @('--since','text:anchor words','--since-occurrence','2','--archive-max-bytes','123456')
    Assert-Forward "$stage turns" {
        cdx-turns $id --codex-home $homeOption
    } @('--list-turns')
    Write-Host "PASS: $stage helper forwarding ($($cases.Count + 2) checks)"
}
Assert-Forward 'Invoke-CodexThreadExport typed selectors' {
    Invoke-CodexThreadExport $id -Mode thread -SinceText 'exact anchor' -SinceOccurrence 2 -ArchiveMaxBytes 512000000 --codex-home $homeOption
} @('--since-text','exact anchor','--since-occurrence','2','--archive-max-bytes','512000000')
Assert-Forward 'Invoke-CodexThreadExport list turns' {
    Invoke-CodexThreadExport $id -ListTurns --codex-home $homeOption
} @('--list-turns')
Assert-Forward 'Invoke defaults since to the full remaining thread' {
    Invoke-CodexThreadExport $id -SinceTurn 2 --codex-home $homeOption
} @('--mode','thread','--since-turn','2')
Assert-Forward 'Invoke defaults raw since to the full remaining thread' {
    Invoke-CodexThreadExport $id --since-id 'exact-id' --codex-home $homeOption
} @('--mode','thread','--since-id','exact-id')
Assert-Forward 'since raw occurrence is preserved' {
    cdx-since $id 'text:anchor words' --since-occurrence 2 --codex-home $homeOption
} @('--since','text:anchor words','--since-occurrence','2')
foreach ($limit in @(16383,512000001)) {
    $script:Seen = @()
    $rejected = $false
    try { cdx-thread $id -ArchiveMaxBytes $limit } catch { $rejected = $true }
    if (-not $rejected -or $script:Seen.Count) { throw "Invalid archive cap $limit reached the invoker." }
}
Write-Host 'PASS: All helper forwarding checks completed without exporting.'
