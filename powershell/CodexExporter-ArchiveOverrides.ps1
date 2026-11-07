# Codex Exporter 0.3.11 archive, review and progress controls for existing PowerShell helpers.
# This file is dot-sourced AFTER existing profile. Existing Invoker is retained.
function Get-CodexArchiveCliArgs {
    param([System.Collections.IDictionary]$Bound)
    if ((@($Bound.Keys) -contains 'Review') -and [bool]$Bound['Review'] -and
        (@($Bound.Keys) -contains 'NoReview') -and [bool]$Bound['NoReview']) {
        throw 'Choose either -Review or -NoReview.'
    }
    $items = [System.Collections.Generic.List[string]]::new()
    $switches = @{
        NoArchive='--no-archive'; NoSourceCopy='--no-source-copy';
        NoArchiveCleanup='--no-archive-cleanup'; Archive='--archive';
        SourceCopy='--source-copy'; ArchiveCleanup='--archive-cleanup';
        Review='--review'; NoReview='--no-review'; NoProgress='--no-progress'
    }
    foreach ($key in $switches.Keys) {
        if ((@($Bound.Keys) -contains $key) -and [bool]$Bound[$key]) {
            $items.Add($switches[$key])
        }
    }
    $values = @{
        ArchiverPath='--archiver-path'; ArchiveLevel='--archive-level';
        ArchiveMethod='--archive-method'; ArchiveDictionary='--archive-dictionary';
        ArchiveWordSize='--archive-word-size'; ArchiveThreads='--archive-threads';
        ArchiveBackend='--archive-backend'; ReviewChunkBytes='--review-chunk-bytes';
        ProgressStyle='--progress-style'
    }
    foreach ($key in $values.Keys) {
        if ((@($Bound.Keys) -contains $key)) {
            $value = [string]$Bound[$key]
            if ($key -in @('ArchiveBackend', 'ProgressStyle')) { $value = $value.ToLowerInvariant() }
            $items.Add($values[$key]); $items.Add($value)
        }
    }
    return $items.ToArray()
}
function Invoke-CodexArchiveWrapper {
    param([string[]]$SessionId, [string]$Mode, [string[]]$Options,
          [System.Collections.IDictionary]$Bound)
    $a = [System.Collections.Generic.List[string]]::new()
    foreach ($id in $SessionId) { $a.Add('--session-id'); $a.Add($id) }
    $a.Add('--mode'); $a.Add($Mode)
    foreach ($x in $Options) { $a.Add([string]$x) }
    foreach ($x in (Get-CodexArchiveCliArgs -Bound $Bound)) { $a.Add([string]$x) }
    Invoke-CodexExporterRaw -ArgumentList $a.ToArray()
}

function cdx-thread {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [switch]$Clipboard, [switch]$Redact, [switch]$FenceTurns,
        [switch]$NoMap, [switch]$ChooseOutDir, [switch]$SaveAs,
        [string]$OutDir, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @()
    if ($Clipboard) { $o += '--clipboard' }; if ($Redact) { $o += '--redact' }
    if ($FenceTurns) { $o += '--fence-turns' }; if ($NoMap) { $o += '--no-map' }
    if ($ChooseOutDir) { $o += '--choose-out-dir' }; if ($SaveAs) { $o += '--save-as' }
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-response {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId, [switch]$NoFile, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--wrap-md', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode last-response -Options $o -Bound $PSBoundParameters
}

function cdx-project {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$Project, [string]$OutDir,
        [switch]$NoProgress, [switch]$UnknownSidecar, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--project', $Project)
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    if (-not $NoProgress) { $o += '--progress' }
    if ($UnknownSidecar) { $o += @('--unknown-events', 'sidecar') }
    Invoke-CodexArchiveWrapper -SessionId @() -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-range {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [Parameter(Mandatory, Position=1)][int]$FromMessage,
        [Parameter(Mandatory, Position=2)][int]$ToMessage, [switch]$NoFile, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--from-message', [string]$FromMessage, '--to-message', [string]$ToMessage, '--plain', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode range -Options $o -Bound $PSBoundParameters
}

function cdx-batch {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string[]]$SessionId,
        [int]$LastNTurns, [switch]$Redact, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--include-session-short-id')
    if ($PSBoundParameters.ContainsKey('LastNTurns')) { $o += @('--last-n-turns', [string]$LastNTurns) }
    if ($Redact) { $o += '--redact' }
    Invoke-CodexArchiveWrapper -SessionId $SessionId -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-turn {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [Parameter(Mandatory, Position=1)][int]$Response,
        [switch]$Clipboard, [switch]$FenceTurns, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--response', [string]$Response, '--no-ui-style')
    if ($Clipboard) { $o += '--clipboard' }
    if ($FenceTurns) { $o += '--fence-turns' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode turn -Options $o -Bound $PSBoundParameters
}

function cdx-last {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [Parameter(Mandatory, Position=1)][ValidateRange(1,2147483647)][int]$Turns,
        [switch]$Clipboard, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--last-n-turns', [string]$Turns)
    if ($Clipboard) { $o += '--clipboard' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-final {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [int]$MinChars = 1000, [switch]$NoFile, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$NoProgress,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--min-chars', [string]$MinChars, '--wrap-md', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode last-substantial -Options $o -Bound $PSBoundParameters
}

function cdx-fast {
    [CmdletBinding()]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [string]$OutDir, [switch]$NoProgress, 
        [switch]$NoArchive,
        [switch]$NoSourceCopy,
        [switch]$NoArchiveCleanup,
        [switch]$Archive,
        [switch]$SourceCopy,
        [switch]$ArchiveCleanup,
        [switch]$Review,
        [switch]$NoReview,
        [ValidateRange(4096,1500000)][int]$ReviewChunkBytes,
        [ValidateSet('auto','rich','plain','json','off')][string]$ProgressStyle,
        [ValidateSet('auto','python','7zip')][string]$ArchiveBackend,
        [string]$ArchiverPath,
        [ValidateRange(1,9)][int]$ArchiveLevel,
        [ValidateSet('LZMA','Deflate')][string]$ArchiveMethod,
        [string]$ArchiveDictionary,
        [ValidateRange(5,273)][int]$ArchiveWordSize,
        [ValidateRange(1,128)][int]$ArchiveThreads)
    $o = @('--fast')
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    if (-not $NoProgress) { $o += '--progress' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-archive-config {
    param([string[]]$Set)
    if ($Set) {
        $a = @(); foreach ($kv in $Set) { $a += @('--set-archive-default', $kv) }
        Invoke-CodexExporterRaw -ArgumentList $a
    } else { Invoke-CodexExporterRaw -ArgumentList @('--show-archive-config') }
}
