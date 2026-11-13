# Codex Exporter 0.4.0 archive, review and progress controls for existing PowerShell helpers.
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
        Review='--review'; NoReview='--no-review'; NoProgress='--no-progress'; ListTurns='--list-turns'
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
        ProgressStyle='--progress-style'; ArchiveMaxBytes='--archive-max-bytes';
        Since='--since'; SinceTurn='--since-turn'; SinceId='--since-id';
        SinceText='--since-text'; SinceTime='--since-time'; SinceOccurrence='--since-occurrence'
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
    if ((@($Bound.Keys) -contains 'Rest')) { foreach ($x in $Bound['Rest']) { $a.Add([string]$x) } }
    Invoke-CodexExporterRaw -ArgumentList $a.ToArray()
}

function cdx-thread {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @()
    if ($Clipboard) { $o += '--clipboard' }; if ($Redact) { $o += '--redact' }
    if ($FenceTurns) { $o += '--fence-turns' }; if ($NoMap) { $o += '--no-map' }
    if ($ChooseOutDir) { $o += '--choose-out-dir' }; if ($SaveAs) { $o += '--save-as' }
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-response {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--wrap-md', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode last-response -Options $o -Bound $PSBoundParameters
}

function cdx-project {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--project', $Project)
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    if (-not $NoProgress) { $o += '--progress' }
    if ($UnknownSidecar) { $o += @('--unknown-events', 'sidecar') }
    Invoke-CodexArchiveWrapper -SessionId @() -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-range {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--from-message', [string]$FromMessage, '--to-message', [string]$ToMessage, '--plain', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode range -Options $o -Bound $PSBoundParameters
}

function cdx-batch {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--include-session-short-id')
    if ($PSBoundParameters.ContainsKey('LastNTurns')) { $o += @('--last-n-turns', [string]$LastNTurns) }
    if ($Redact) { $o += '--redact' }
    Invoke-CodexArchiveWrapper -SessionId $SessionId -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-turn {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--response', [string]$Response, '--no-ui-style')
    if ($Clipboard) { $o += '--clipboard' }
    if ($FenceTurns) { $o += '--fence-turns' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode turn -Options $o -Bound $PSBoundParameters
}

function cdx-last {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--last-n-turns', [string]$Turns)
    if ($Clipboard) { $o += '--clipboard' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-final {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--min-chars', [string]$MinChars, '--wrap-md', '--clipboard')
    if ($NoFile) { $o += '--no-file' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode last-substantial -Options $o -Bound $PSBoundParameters
}

function cdx-fast {
    [CmdletBinding(PositionalBinding=$false)]
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--fast')
    if ($OutDir) { $o += @('--out-dir', $OutDir) }
    if (-not $NoProgress) { $o += '--progress' }
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}

function cdx-since {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory,Position=0)][string]$SessionId,
        [Parameter(Mandatory,Position=1)][string]$Selector,
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
        [ValidateRange(1,128)][int]$ArchiveThreads,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [string]$Since,
        [ValidateRange(1,2147483647)][int]$SinceTurn,
        [string]$SinceId, [string]$SinceText, [string]$SinceTime,
        [Alias('Occurrence')][ValidateRange(1,2147483647)][int]$SinceOccurrence,
        [switch]$ListTurns,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $o = @('--since', $Selector)
    Invoke-CodexArchiveWrapper -SessionId @($SessionId) -Mode thread -Options $o -Bound $PSBoundParameters
}
function cdx-turns {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory,Position=0)][string]$SessionId,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    Invoke-CodexExporterRaw -ArgumentList (@('--session-id', $SessionId, '--list-turns') + $Rest)
}

function cdx-archive-config {
    [CmdletBinding(PositionalBinding=$false)]
    param([string[]]$Set, [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    if ($Set) {
        $a = @(); foreach ($kv in $Set) { $a += @('--set-archive-default', $kv) }
        Invoke-CodexExporterRaw -ArgumentList ($a + $Rest)
    } else { Invoke-CodexExporterRaw -ArgumentList (@('--show-archive-config') + $Rest) }
}
