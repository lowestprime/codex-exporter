# Codex Thread Exporter PowerShell helper functions
# Safe to paste at the bottom of $PROFILE. Avoid literal smart quotes in this file.

$script:CodexThreadExporter = if ($env:CODEX_THREAD_EXPORTER) { $env:CODEX_THREAD_EXPORTER } else { 'C:\projects\CodexTools\Export-CodexThread.py' }
$script:CodexThreadExportDir = if ($env:CODEX_THREAD_EXPORT_DIR) { $env:CODEX_THREAD_EXPORT_DIR } else { 'C:\projects\CodexTools\codex-thread-exports' }

function Invoke-CodexThreadExport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,

        [ValidateSet('thread','response','turn','last-response','last-assistant','last-substantial','message','range','chat','chat-actions','actions')]
        [string]$Mode = 'last-response',

        [int]$Response,
        [int]$Message,
        [int]$FromMessage,
        [int]$ToMessage,
        [int]$MinChars = 1000,

        [switch]$List,
        [switch]$ListResponses,

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

        [ValidateSet('none','summary','tail','full')]
        [string]$ToolOutputs = 'full',

        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$Rest = @()
    )

    if (-not [System.IO.File]::Exists($script:CodexThreadExporter)) {
        throw "Codex thread exporter not found: $script:CodexThreadExporter"
    }

    [System.IO.Directory]::CreateDirectory($script:CodexThreadExportDir) | Out-Null

    $args = @(
        $script:CodexThreadExporter,
        '--session-id', $SessionId,
        '--out-dir', $script:CodexThreadExportDir
    )

    if ($List) {
        $args += '--list'
    } elseif ($ListResponses) {
        $args += '--list-responses'
    } else {
        $args += @('--mode', $Mode)
    }

    if ($PSBoundParameters.ContainsKey('Response'))    { $args += @('--response', $Response) }
    if ($PSBoundParameters.ContainsKey('Message'))     { $args += @('--message', $Message) }
    if ($PSBoundParameters.ContainsKey('FromMessage')) { $args += @('--from-message', $FromMessage) }
    if ($PSBoundParameters.ContainsKey('ToMessage'))   { $args += @('--to-message', $ToMessage) }
    if ($PSBoundParameters.ContainsKey('MinChars'))    { $args += @('--min-chars', $MinChars) }

    if ($Plain)            { $args += '--plain' }
    if ($Clipboard)        { $args += '--clipboard' }
    if ($NoFile)           { $args += '--no-file' }
    if ($Stdout)           { $args += '--stdout' }
    if ($Redact)           { $args += '--redact' }
    if ($WrapMd)           { $args += '--wrap-md' }
    if ($NoUiStyle)        { $args += '--no-ui-style' }
    if ($WrapTitle)        { $args += @('--wrap-title', $WrapTitle) }
    if ($NoFilenameCounts) { $args += '--no-filename-counts' }
    if ($NoFrontmatter)    { $args += '--no-frontmatter' }
    if ($NoMap)            { $args += '--no-map' }
    if ($FenceTurns)       { $args += '--fence-turns' }
    if ($ToolOutputs)      { $args += @('--tool-outputs', $ToolOutputs) }
    if ($Rest)             { $args += $Rest }

    python @args
}

function cdx-list {
    param([Parameter(Mandatory, Position=0)] [string]$SessionId)
    Invoke-CodexThreadExport -SessionId $SessionId -List
}

function cdx-responses {
    param([Parameter(Mandatory, Position=0)] [string]$SessionId)
    Invoke-CodexThreadExport -SessionId $SessionId -ListResponses
}

function cdx-thread {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [switch]$Clipboard,
        [switch]$Redact,
        [switch]$FenceTurns,
        [switch]$NoMap
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard:$Clipboard -Redact:$Redact -FenceTurns:$FenceTurns -NoMap:$NoMap -ToolOutputs full
}

function cdx-thread-clip {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [switch]$Redact,
        [switch]$FenceTurns
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode thread -Clipboard -Redact:$Redact -FenceTurns:$FenceTurns -ToolOutputs full
}

function cdx-response {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [switch]$NoFile
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-response -WrapMd -Clipboard -NoFile:$NoFile -ToolOutputs full
}

function cdx-response-block {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [Parameter(Mandatory, Position=1)] [int]$Response,
        [switch]$NoFile
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode response -Response $Response -WrapMd -Clipboard -NoFile:$NoFile -ToolOutputs full
}

function cdx-turn {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [Parameter(Mandatory, Position=1)] [int]$Response,
        [switch]$Clipboard,
        [switch]$FenceTurns
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode turn -Response $Response -NoUiStyle -Clipboard:$Clipboard -FenceTurns:$FenceTurns -ToolOutputs full
}

function cdx-final {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [int]$MinChars = 1000,
        [switch]$NoFile
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode last-substantial -MinChars $MinChars -WrapMd -Clipboard -NoFile:$NoFile -ToolOutputs full
}

function cdx-msg {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [Parameter(Mandatory, Position=1)] [int]$Message,
        [switch]$NoFile
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode message -Message $Message -Plain -Clipboard -NoFile:$NoFile
}

function cdx-range {
    param(
        [Parameter(Mandatory, Position=0)] [string]$SessionId,
        [Parameter(Mandatory, Position=1)] [int]$FromMessage,
        [Parameter(Mandatory, Position=2)] [int]$ToMessage,
        [switch]$NoFile
    )
    Invoke-CodexThreadExport -SessionId $SessionId -Mode range -FromMessage $FromMessage -ToMessage $ToMessage -Plain -Clipboard -NoFile:$NoFile
}

function cdx-open {
    if (-not (Test-Path $script:CodexThreadExportDir)) {
        [System.IO.Directory]::CreateDirectory($script:CodexThreadExportDir) | Out-Null
    }
    code $script:CodexThreadExportDir
}

function cdx-verify-latest {
    $latest = Get-ChildItem $script:CodexThreadExportDir -Filter '*.md' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "No Markdown exports found in $script:CodexThreadExportDir" }

    $txt = Get-Content $latest.FullName -Raw
    $clip = Get-Clipboard -Raw

    # Parser-safe Unicode test strings; do not place literal smart quotes here.
    $smartApos = [string][char]0x2019
    $aeMojibake = [string][char]0x00C6
    $aCircumflex = [string][char]0x00E2
    $oCircumflex = [string][char]0x00F4
    $correctIll = 'I' + $smartApos + 'll'
    $mojibakeIll = 'I' + $aeMojibake + 'll'
    $mojibakeProxy = $aCircumflex + ' Proxy'

    [pscustomobject]@{
        File = $latest.FullName
        FileBytes = $latest.Length
        File_Has_Frontmatter = $txt.StartsWith("---")
        File_Has_ThreadMap = $txt.Contains("## Thread export map")
        File_Has_Turns = $txt.Contains("## Turn 001")
        File_Has_EditedFile = $txt.Contains("Edited file")
        File_Has_CreatedFile = $txt.Contains("Created file")
        File_Has_RawPatchNoise = $txt.Contains("Action ``patch_apply_end``") -or $txt.Contains("Action ``apply_patch``") -or $txt.Contains("*** End Patch")
        File_Has_ChunkMetadata = $txt.Contains("Chunk ID:") -or $txt.Contains("Original token count:")
        File_Has_ActionNoise = $txt.Contains("Action `")
        File_Has_TerminalTitleResidue = [regex]::IsMatch($txt, '(?m)^0;')
        File_Has_CorrectUnicode = $txt.Contains($correctIll)
        File_Has_Mojibake = $txt.Contains($mojibakeIll) -or $txt.Contains($mojibakeProxy) -or $txt.Contains($oCircumflex)
        Clipboard_MatchesFile = ($clip -eq $txt)
        Clipboard_Has_EditedFile = $clip.Contains('Edited file')
        Clipboard_Has_ChunkMetadata = $clip.Contains('Chunk ID:') -or $clip.Contains('Original token count:')
        Clipboard_Has_CorrectUnicode = $clip.Contains($correctIll)
        Clipboard_Has_Mojibake = $clip.Contains($mojibakeIll) -or $clip.Contains($mojibakeProxy) -or $clip.Contains($oCircumflex)
    }
}

function Install-CodexThreadExportSkill {
    [CmdletBinding()]
    param(
        [string]$SkillRoot = (Join-Path $HOME '.agents\skills\codex-thread-export')
    )

    New-Item -ItemType Directory -Force -Path $SkillRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $SkillRoot 'agents') | Out-Null

    @'
---
name: codex-thread-export
description: Export Codex app local threads to Markdown. Use for full-thread exports, current-response copies, selected response/turn exports, transcript maps, YAML metadata, and Unicode-safe clipboard copying.
---

Use the local exporter at `C:\projects\CodexTools\Export-CodexThread.py`.

Workflow:
1. If the user did not provide a session/thread UUID, ask them to run `/status` in the Codex app thread and paste the Session value.
2. For a full thread/chat export with frontmatter, map, prompts, responses, commands, outputs, and edited-file cards, run:
   `python "C:\projects\CodexTools\Export-CodexThread.py" --session-id "<SESSION_ID>" --mode thread --out-dir "C:\projects\CodexTools\codex-thread-exports"`
3. For the current uncollapsed response only, run:
   `python "C:\projects\CodexTools\Export-CodexThread.py" --session-id "<SESSION_ID>" --mode last-response --wrap-md --clipboard --out-dir "C:\projects\CodexTools\codex-thread-exports"`
4. For response selection, first run:
   `python "C:\projects\CodexTools\Export-CodexThread.py" --session-id "<SESSION_ID>" --list-responses`
   Then export with `--mode response --response N` or `--mode turn --response N`.
5. Prefer redaction for public sharing: add `--redact`.
6. Do not commit raw `.codex` session files or unredacted exports containing secrets.
'@ | Set-Content -Path (Join-Path $SkillRoot 'SKILL.md') -Encoding utf8NoBOM

    @'
interface:
  display_name: "Codex Thread Export"
  short_description: "Export local Codex app threads to Markdown with maps and metadata."
  brand_color: "#2563EB"
  default_prompt: "Export this Codex thread. If I did not provide a session ID, ask me to run /status and paste the Session value."
policy:
  allow_implicit_invocation: false
'@ | Set-Content -Path (Join-Path $SkillRoot 'agents\openai.yaml') -Encoding utf8NoBOM

    "Installed Codex thread export skill at $SkillRoot. Restart Codex if it does not appear."
}
