# >>> Codex Thread Exporter helpers >>>
# Codex Thread Exporter PowerShell 7+ helpers (v8 / exporter 0.2+).
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
    [CmdletBinding()]
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$ArgumentList = @()
    )

    if ([System.IO.File]::Exists($script:CodexThreadExporter)) {
        if (-not [System.IO.File]::Exists($script:CodexExportPython)) {
            throw "Configured Python interpreter not found: $script:CodexExportPython"
        }
        & $script:CodexExportPython $script:CodexThreadExporter @ArgumentList
        return
    }

    $console = Get-Command codex-export -CommandType Application -ErrorAction SilentlyContinue
    if ($console) {
        & $console.Source @ArgumentList
        return
    }
    throw "Codex exporter not found at '$script:CodexThreadExporter', and no codex-export console command is installed."
}

function Invoke-CodexThreadExport {
    [CmdletBinding()]
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

        [switch]$LiveContext,
        [switch]$ReasoningSummaries,
        [switch]$List,
        [switch]$ListResponses,
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

    $arguments = @()
    foreach ($id in $SessionId) {
        $arguments += @('--session-id', $id)
    }

    if ($List) {
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

    if ($Rest) { $arguments += $Rest }
    Invoke-CodexExporterRaw -ArgumentList $arguments
}

function cdx-sessions { Invoke-CodexExporterRaw -ArgumentList (@('--list-sessions') + $args) }
function cdx-browse { Invoke-CodexExporterRaw -ArgumentList (@('--browse', '--mode', 'thread') + $args) }
function cdx-config { Invoke-CodexExporterRaw -ArgumentList @('--show-config') }
function cdx-tokenizer {
    param(
        [ValidateSet('auto','tiktoken','regex')] [string]$Tokenizer = 'auto',
        [string]$Encoding = 'cl100k_base',
        [switch]$Require
    )
    $arguments = @('--tokenizer-info', '--tokenizer', $Tokenizer, '--token-encoding', $Encoding)
    if ($Require) { $arguments += '--require-tiktoken' }
    Invoke-CodexExporterRaw -ArgumentList $arguments
}

function cdx-set-template {
    param([Parameter(Position = 0)] [string]$Template)
    if ($Template) { Invoke-CodexExporterRaw -ArgumentList @('--set-filename-template', $Template) }
    else { Invoke-CodexExporterRaw -ArgumentList @('--print-filename-template') }
}

function cdx-set-dir {
    param([Parameter(Position = 0)] [string]$Path)
    if ($Path) { Invoke-CodexExporterRaw -ArgumentList @('--set-default-out-dir', $Path) }
    else { Invoke-CodexExporterRaw -ArgumentList @('--choose-default-out-dir') }
}

function cdx-open {
    $directory = (Invoke-CodexExporterRaw -ArgumentList @('--print-out-dir') | Select-Object -Last 1).Trim()
    if (-not $directory) { throw 'Exporter returned no output directory.' }
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    Start-Process explorer.exe -ArgumentList @($directory)
}

function cdx-list {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId)
    Invoke-CodexThreadExport -SessionId $SessionId -List
}
function cdx-responses {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId)
    Invoke-CodexThreadExport -SessionId $SessionId -ListResponses
}
function cdx-thread {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Clipboard, [switch]$Redact, [switch]$FenceTurns, [switch]$NoMap, [switch]$ChooseOutDir, [switch]$SaveAs)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard:$Clipboard -Redact:$Redact -FenceTurns:$FenceTurns -NoMap:$NoMap -ChooseOutDir:$ChooseOutDir -SaveAs:$SaveAs
}
function cdx-thread-clip {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Redact, [switch]$FenceTurns)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard -Redact:$Redact -FenceTurns:$FenceTurns
}
function cdx-response {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$NoFile)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-response -WrapMd -Clipboard -NoFile:$NoFile
}
function cdx-response-block {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Response, [switch]$NoFile)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode response -Response $Response -WrapMd -Clipboard -NoFile:$NoFile
}
function cdx-turn {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Response, [switch]$Clipboard, [switch]$FenceTurns)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode turn -Response $Response -NoUiStyle -Clipboard:$Clipboard -FenceTurns:$FenceTurns
}
function cdx-last {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [ValidateRange(1, 2147483647)] [int]$Turns, [switch]$Clipboard)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -LastNTurns $Turns -Clipboard:$Clipboard
}
function cdx-live {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [switch]$Clipboard)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -LiveContext -Clipboard:$Clipboard
}
function cdx-batch {
    param([Parameter(Mandatory, Position = 0)] [string[]]$SessionId, [int]$LastNTurns, [switch]$Redact)
    $params = @{ SessionId = $SessionId; Mode = 'thread'; IncludeSessionShortId = $true; Redact = $Redact }
    if ($PSBoundParameters.ContainsKey('LastNTurns')) { $params.LastNTurns = $LastNTurns }
    Invoke-CodexThreadExport @params
}
function cdx-final {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [int]$MinChars = 1000, [switch]$NoFile)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-substantial -MinChars $MinChars -WrapMd -Clipboard -NoFile:$NoFile
}
function cdx-msg {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$Message, [switch]$NoFile)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode message -Message $Message -Plain -Clipboard -NoFile:$NoFile
}
function cdx-range {
    param([Parameter(Mandatory, Position = 0)] [string]$SessionId, [Parameter(Mandatory, Position = 1)] [int]$FromMessage, [Parameter(Mandatory, Position = 2)] [int]$ToMessage, [switch]$NoFile)
    Invoke-CodexThreadExport -SessionId $SessionId -Mode range -FromMessage $FromMessage -ToMessage $ToMessage -Plain -Clipboard -NoFile:$NoFile
}
# <<< Codex Thread Exporter helpers <<<
