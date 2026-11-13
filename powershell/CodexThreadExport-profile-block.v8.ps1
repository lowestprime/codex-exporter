# >>> Codex Thread Exporter helpers >>>
# Codex Thread Exporter PowerShell 7+ helpers (exporter 0.4+).
# The Python exporter owns persistent output-directory and filename-template state.

$script:CodexThreadExporter = if ($env:CODEX_THREAD_EXPORTER) {
    [System.IO.Path]::GetFullPath($env:CODEX_THREAD_EXPORTER)
} else {
    'C:\projects\CodexTools\Export-CodexThread.py'
}

$script:CodexExportPythonHint = '__CODEX_EXPORT_PYTHON__'
$script:CodexExportPython = if ($env:CODEX_EXPORT_PYTHON) {
    [System.IO.Path]::GetFullPath($env:CODEX_EXPORT_PYTHON)
} elseif ([System.IO.File]::Exists($script:CodexExportPythonHint)) {
    [System.IO.Path]::GetFullPath($script:CodexExportPythonHint)
} else {
    (Get-Command python -ErrorAction Stop).Source
}

function Invoke-CodexExporterRaw {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$ArgumentList = @())

    if (-not [System.IO.File]::Exists($script:CodexExportPython)) {
        throw "Configured Python interpreter not found: $script:CodexExportPython"
    }
    # An explicit standalone override is intentional; otherwise prefer the newest code.
    if ($env:CODEX_THREAD_EXPORTER) {
        $target = [System.IO.Path]::GetFullPath($env:CODEX_THREAD_EXPORTER)
        if (-not [System.IO.File]::Exists($target)) { throw "Configured exporter not found: $target" }
        & $script:CodexExportPython $target @ArgumentList
    } else {
        $moduleVersion = (& $script:CodexExportPython -c 'import codex_exporter; print(codex_exporter.__version__)' 2>$null | Out-String).Trim()
        $hasModule = $LASTEXITCODE -eq 0
        $useModule = $hasModule
        if ($hasModule -and [System.IO.File]::Exists($script:CodexThreadExporter)) {
            $scriptVersion = [regex]::Match([System.IO.File]::ReadAllText($script:CodexThreadExporter), '(?m)^__version__\s*=\s*["'']([^"'']+)').Groups[1].Value
            $parsedModule = $null; $parsedScript = $null
            if ([version]::TryParse($moduleVersion, [ref]$parsedModule) -and
                [version]::TryParse($scriptVersion, [ref]$parsedScript)) {
                $useModule = $parsedModule -ge $parsedScript
            }
        }
        if ($useModule) {
            & $script:CodexExportPython -m codex_exporter @ArgumentList
        } elseif ([System.IO.File]::Exists($script:CodexThreadExporter)) {
            & $script:CodexExportPython $script:CodexThreadExporter @ArgumentList
        } else {
            $console = Get-Command codex-export -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $console) { throw 'Codex exporter is not installed in the configured Python and no fallback is available.' }
            & $console.Source @ArgumentList
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "codex_exporter returned exit code $LASTEXITCODE" }
}

function Invoke-CodexThreadExport {
    [CmdletBinding(PositionalBinding=$false)]
    param(
        [Parameter(Mandatory, Position = 0)]
        [string[]]$SessionId,

        [ValidateSet('thread','response','turn','last-response','last-assistant','last-substantial','message','range','chat','chat-actions','actions')]
        [string]$Mode = 'last-response',

        [int]$Response,
        [int]$Message,
        [int]$FromMessage,
        [int]$ToMessage,
        [int]$MinChars = 1000,
        [int]$LastNTurns,
        [string]$Since,
        [ValidateRange(1, 2147483647)][int]$SinceTurn,
        [string]$SinceId,
        [string]$SinceText,
        [string]$SinceTime,
        [ValidateRange(1, 2147483647)][int]$SinceOccurrence,
        [ValidateRange(16384, 512000000)][long]$ArchiveMaxBytes,

        [switch]$LiveContext,
        [switch]$ReasoningSummaries,
        [switch]$List,
        [switch]$ListResponses,
        [switch]$ListTurns,
        [switch]$ReportEvents,
        [switch]$StrictEvents,

        [string]$Name,
        [string]$OutDir,
        [switch]$ChooseOutDir,
        [switch]$SaveAs,
        [string]$FilenameTemplate,
        [switch]$SaveFilenameTemplate,
        [switch]$StableFilenames,
        [switch]$IncludeSessionShortId,
        [ValidateSet('rename','overwrite','skip','error')]
        [string]$Collision = 'rename',
        [switch]$OpenAfter,

        [switch]$Plain,
        [switch]$Clipboard,
        [switch]$NoFile,
        [switch]$Stdout,
        [switch]$Redact,
        [switch]$WrapMd,
        [switch]$NoUiStyle,
        [string]$WrapTitle = '',
        [switch]$NoFilenameCounts,
        [switch]$NoFrontmatter,
        [switch]$NoMap,
        [switch]$FenceTurns,
        [switch]$NoManifest,

        [ValidateSet('auto','tiktoken','regex')]
        [string]$Tokenizer = 'auto',
        [string]$TokenEncoding = 'cl100k_base',
        [switch]$RequireTiktoken,
        [ValidateSet('annotate','preserve','error')]
        [string]$SourceTruncation = 'annotate',
        [ValidateSet('none','summary','tail','full')]
        [string]$ToolOutputs = 'full',

        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$Rest = @()
    )

    $hasSince = $false
    foreach ($key in @('Since','SinceTurn','SinceId','SinceText','SinceTime')) {
        if ($PSBoundParameters.ContainsKey($key)) { $hasSince = $true }
    }
    if (@($Rest | Where-Object { [string]$_ -match '^--since(?:-turn|-id|-text|-time)?(?:=|$)' }).Count) { $hasSince = $true }
    if ($hasSince -and -not $PSBoundParameters.ContainsKey('Mode')) { $Mode = 'thread' }
    $arguments = @()
    foreach ($id in $SessionId) {
        $arguments += @('--session-id', $id)
    }

    if (@($List, $ListResponses, $ListTurns).Where({ $_ }).Count -gt 1) { throw 'Choose one list mode.' }
    if ($ListTurns) {
        $arguments += '--list-turns'
    } elseif ($List) {
        $arguments += '--list'
    } elseif ($ListResponses) {
        $arguments += '--list-responses'
    } else {
        $arguments += @('--mode', $Mode)
    }

    foreach ($entry in @(
        @{ Name = 'Response'; Flag = '--response' },
        @{ Name = 'Message'; Flag = '--message' },
        @{ Name = 'FromMessage'; Flag = '--from-message' },
        @{ Name = 'ToMessage'; Flag = '--to-message' },
        @{ Name = 'MinChars'; Flag = '--min-chars' },
        @{ Name = 'LastNTurns'; Flag = '--last-n-turns' },
        @{ Name = 'Since'; Flag = '--since' },
        @{ Name = 'SinceTurn'; Flag = '--since-turn' },
        @{ Name = 'SinceId'; Flag = '--since-id' },
        @{ Name = 'SinceText'; Flag = '--since-text' },
        @{ Name = 'SinceTime'; Flag = '--since-time' },
        @{ Name = 'SinceOccurrence'; Flag = '--since-occurrence' },
        @{ Name = 'ArchiveMaxBytes'; Flag = '--archive-max-bytes' },
        @{ Name = 'Name'; Flag = '--name' },
        @{ Name = 'OutDir'; Flag = '--out-dir' },
        @{ Name = 'FilenameTemplate'; Flag = '--filename-template' },
        @{ Name = 'WrapTitle'; Flag = '--wrap-title' }
    )) {
        if ($PSBoundParameters.ContainsKey($entry.Name)) {
            $arguments += @($entry.Flag, $PSBoundParameters[$entry.Name])
        }
    }

    $arguments += @('--collision', $Collision, '--tokenizer', $Tokenizer, '--token-encoding', $TokenEncoding,
        '--source-truncation', $SourceTruncation, '--tool-outputs', $ToolOutputs)

    foreach ($entry in @(
        @{ Value = $LiveContext; Flag = '--live-context' },
        @{ Value = $ReasoningSummaries; Flag = '--reasoning-summaries' },
        @{ Value = $ReportEvents; Flag = '--report-events' },
        @{ Value = $StrictEvents; Flag = '--strict-events' },
        @{ Value = $ChooseOutDir; Flag = '--choose-out-dir' },
        @{ Value = $SaveAs; Flag = '--save-as' },
        @{ Value = $SaveFilenameTemplate; Flag = '--save-filename-template' },
        @{ Value = $StableFilenames; Flag = '--stable-filenames' },
        @{ Value = $IncludeSessionShortId; Flag = '--include-session-short-id' },
        @{ Value = $OpenAfter; Flag = '--open-after' },
        @{ Value = $Plain; Flag = '--plain' },
        @{ Value = $Clipboard; Flag = '--clipboard' },
        @{ Value = $NoFile; Flag = '--no-file' },
        @{ Value = $Stdout; Flag = '--stdout' },
        @{ Value = $Redact; Flag = '--redact' },
        @{ Value = $WrapMd; Flag = '--wrap-md' },
        @{ Value = $NoUiStyle; Flag = '--no-ui-style' },
        @{ Value = $NoFilenameCounts; Flag = '--no-filename-counts' },
        @{ Value = $NoFrontmatter; Flag = '--no-frontmatter' },
        @{ Value = $NoMap; Flag = '--no-map' },
        @{ Value = $FenceTurns; Flag = '--fence-turns' },
        @{ Value = $NoManifest; Flag = '--no-manifest' },
        @{ Value = $RequireTiktoken; Flag = '--require-tiktoken' }
    )) {
        if ($entry.Value) { $arguments += $entry.Flag }
    }

    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}

function cdx-sessions { Invoke-CodexExporterRaw -ArgumentList (@('--list-sessions') + $args) }
function cdx-browse { Invoke-CodexExporterRaw -ArgumentList (@('--browse', '--mode', 'thread') + $args) }
function cdx-config { Invoke-CodexExporterRaw -ArgumentList (@('--show-config') + $args) }
function cdx-tokenizer {
    [CmdletBinding(PositionalBinding=$false)]
    param(
        [ValidateSet('auto','tiktoken','regex')] [string]$Tokenizer = 'auto',
        [string]$Encoding = 'cl100k_base',
        [switch]$Require,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @()
    )
    $arguments = @('--tokenizer-info', '--tokenizer', $Tokenizer, '--token-encoding', $Encoding)
    if ($Require) { $arguments += '--require-tiktoken' }
    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}

function cdx-set-template {
    [CmdletBinding(PositionalBinding=$false)]
    param([string]$Template,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    if (-not $PSBoundParameters.ContainsKey('Template') -and $Rest.Count -gt 0 -and -not ([string]$Rest[0]).StartsWith('-')) {
        $Template = [string]$Rest[0]; $Rest = @($Rest | Select-Object -Skip 1)
    }
    if ($Template) { Invoke-CodexExporterRaw -ArgumentList (@('--set-filename-template', $Template) + $Rest) }
    else { Invoke-CodexExporterRaw -ArgumentList (@('--print-filename-template') + $Rest) }
}

function cdx-set-dir {
    [CmdletBinding(PositionalBinding=$false)]
    param([string]$Path,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    if (-not $PSBoundParameters.ContainsKey('Path') -and $Rest.Count -gt 0 -and -not ([string]$Rest[0]).StartsWith('-')) {
        $Path = [string]$Rest[0]; $Rest = @($Rest | Select-Object -Skip 1)
    }
    if ($Path) { Invoke-CodexExporterRaw -ArgumentList (@('--set-default-out-dir', $Path) + $Rest) }
    else { Invoke-CodexExporterRaw -ArgumentList (@('--choose-default-out-dir') + $Rest) }
}

function cdx-open {
    $directory = (Invoke-CodexExporterRaw -ArgumentList (@('--print-out-dir') + $args) | Select-Object -Last 1).Trim()
    if (-not $directory) { throw 'Exporter returned no output directory.' }
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    Start-Process explorer.exe -ArgumentList @($directory)
}

function cdx-list {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -List -Rest $Rest
}
function cdx-responses {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -ListResponses -Rest $Rest
}
function cdx-thread {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Clipboard, [switch]$Redact, [switch]$FenceTurns, [switch]$NoMap, [switch]$ChooseOutDir, [switch]$SaveAs,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard:$Clipboard -Redact:$Redact -FenceTurns:$FenceTurns -NoMap:$NoMap -ChooseOutDir:$ChooseOutDir -SaveAs:$SaveAs -Rest $Rest
}
function cdx-thread-clip {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Redact, [switch]$FenceTurns,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard -Redact:$Redact -FenceTurns:$FenceTurns -Rest $Rest
}
function cdx-response {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$NoFile,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-response -WrapMd -Clipboard -NoFile:$NoFile -Rest $Rest
}
function cdx-response-block {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Response, [switch]$NoFile,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode response -Response $Response -WrapMd -Clipboard -NoFile:$NoFile -Rest $Rest
}
function cdx-turn {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Response, [switch]$Clipboard, [switch]$FenceTurns,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode turn -Response $Response -NoUiStyle -Clipboard:$Clipboard -FenceTurns:$FenceTurns -Rest $Rest
}
function cdx-last {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [ValidateRange(1, 2147483647)] [int]$Turns, [switch]$Clipboard,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -LastNTurns $Turns -Clipboard:$Clipboard -Rest $Rest
}
function cdx-live {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Clipboard,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -LiveContext -Clipboard:$Clipboard -Rest $Rest
}
function cdx-batch {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string[]]$SessionId, [int]$LastNTurns, [switch]$Redact,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    $params = @{ SessionId = $SessionId; Mode = 'thread'; IncludeSessionShortId = $true; Redact = $Redact }
    if ($PSBoundParameters.ContainsKey('LastNTurns')) { $params.LastNTurns = $LastNTurns }
    Invoke-CodexThreadExport @params -Rest $Rest
}
function cdx-final {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [int]$MinChars = 1000, [switch]$NoFile,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-substantial -MinChars $MinChars -WrapMd -Clipboard -NoFile:$NoFile -Rest $Rest
}
function cdx-msg {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Message, [switch]$NoFile,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode message -Message $Message -Plain -Clipboard -NoFile:$NoFile -Rest $Rest
}
function cdx-range {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$FromMessage, [Parameter(Mandatory, Position = 2)] [int]$ToMessage, [switch]$NoFile,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -Mode range -FromMessage $FromMessage -ToMessage $ToMessage -Plain -Clipboard -NoFile:$NoFile -Rest $Rest
}
# Project and import convenience functions preserve all additional CLI options.
function cdx-project {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position=0)][string]$Project, [string]$OutDir,
          [switch]$NoProgress, [switch]$UnknownSidecar,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    $arguments = @('--project', $Project, '--mode', 'thread')
    if ($OutDir) { $arguments += @('--out-dir', $OutDir) }
    $arguments += $(if ($NoProgress) { '--no-progress' } else { '--progress' })
    if ($UnknownSidecar) { $arguments += @('--unknown-events', 'sidecar') }
    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}
function cdx-audit {
    [CmdletBinding(PositionalBinding=$false)]
    param([string]$Project,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    if (-not $PSBoundParameters.ContainsKey('Project') -and $Rest.Count -gt 0 -and -not ([string]$Rest[0]).StartsWith('-')) {
        $Project = [string]$Rest[0]; $Rest = @($Rest | Select-Object -Skip 1)
    }
    $arguments = @('--audit-sessions')
    if ($Project) { $arguments += @('--project', $Project) }
    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}
function cdx-fast {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position=0)][string]$SessionId, [string]$OutDir,
          [switch]$NoProgress,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    $arguments = @('--session-id', $SessionId, '--mode', 'thread', '--fast')
    if ($OutDir) { $arguments += @('--out-dir', $OutDir) }
    $arguments += $(if ($NoProgress) { '--no-progress' } else { '--progress' })
    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}
function cdx-chatgpt {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position=0)][string]$Archive, [string]$TitleRegex,
          [string]$ConversationId, [string]$ProjectId, [string]$OutDir,
        [Parameter(ValueFromRemainingArguments = $true)][object[]]$Rest = @())
    $arguments = @('--chatgpt-export', $Archive)
    if ($TitleRegex) { $arguments += @('--chatgpt-title-regex', $TitleRegex) }
    if ($ConversationId) { $arguments += @('--chatgpt-id', $ConversationId) }
    if ($ProjectId) { $arguments += @('--chatgpt-project-id', $ProjectId) }
    if ($OutDir) { $arguments += @('--out-dir', $OutDir) }
    Invoke-CodexExporterRaw -ArgumentList ($arguments + $Rest)
}
function cdx-since {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [Parameter(Mandatory, Position=1)][string]$Selector,
        [Alias('SinceOccurrence')][ValidateRange(1,2147483647)][int]$Occurrence,
        [ValidateRange(16384,512000000)][long]$ArchiveMaxBytes,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    $parameters = @{ SessionId=$SessionId; Mode='thread'; Since=$Selector; Rest=$Rest }
    if ($PSBoundParameters.ContainsKey('Occurrence')) { $parameters.SinceOccurrence = $Occurrence }
    if ($PSBoundParameters.ContainsKey('ArchiveMaxBytes')) { $parameters.ArchiveMaxBytes = $ArchiveMaxBytes }
    Invoke-CodexThreadExport @parameters
}
function cdx-turns {
    [CmdletBinding(PositionalBinding=$false)]
    param([Parameter(Mandatory, Position=0)][string]$SessionId,
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    Invoke-CodexThreadExport -SessionId $SessionId -ListTurns -Rest $Rest
}
function cdx-latest {
    [CmdletBinding(PositionalBinding=$false)]
    param([ValidateSet('thread','response','turn','last-response','last-assistant','last-substantial','message','range','chat','chat-actions','actions')][string]$Mode='thread',
        [Parameter(ValueFromRemainingArguments=$true)][object[]]$Rest = @())
    if (-not $PSBoundParameters.ContainsKey('Mode') -and $Rest.Count -gt 0 -and -not ([string]$Rest[0]).StartsWith('-')) {
        $Mode = [string]$Rest[0]; $Rest = @($Rest | Select-Object -Skip 1)
    }
    Invoke-CodexExporterRaw -ArgumentList (@('--latest-session','--mode',$Mode) + $Rest)
}
# <<< Codex Thread Exporter helpers <<<

