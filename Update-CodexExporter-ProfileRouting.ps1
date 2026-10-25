#requires -Version 7.0
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$ProfilePath = (Join-Path $HOME 'Documents\PowerShell\Microsoft.PowerShell_profile.ps1'))
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { throw "Profile not found: $ProfilePath" }
$original = [IO.File]::ReadAllText($ProfilePath)
$startTag = 'function Invoke-CodexExporterRaw {'
$nextTag = 'function Invoke-CodexThreadExport {'
$start = $original.IndexOf($startTag, [StringComparison]::Ordinal)
if ($start -lt 0) { throw 'Exporter helper block not found; no changes made.' }
$end = $original.IndexOf($nextTag, $start, [StringComparison]::Ordinal)
if ($end -lt 0) { throw 'Exporter function boundary not found; no changes made.' }
if ($original.IndexOf($startTag, $start+1, [StringComparison]::Ordinal) -ge 0) { throw 'Ambiguous duplicate helper definitions; no changes made.' }
$replacement = @'
function Invoke-CodexExporterRaw {
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments=$true)][object[]]$ArgumentList=@())

    # Invoke module through the chosen interpreter, never an older copied script.
    # This automatically tracks pip upgrades for this interpreter without profile edits.
    $python = if ($env:CODEX_EXPORT_PYTHON) {
        [IO.Path]::GetFullPath($env:CODEX_EXPORT_PYTHON)
    } else {
        (& py -3.13 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1)
    }
    if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Python 3.13 interpreter for codex-exporter not found.'
    }
    & $python -c 'import codex_exporter' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "The codex_exporter package is not installed in $python. Run: & '$python' -m pip install --upgrade '<local exporter directory>'"
    }
    & $python -m codex_exporter @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "codex_exporter returned exit code $LASTEXITCODE"
    }
}

'@
$updated=$original.Substring(0,$start)+$replacement+$original.Substring($end)
if ($PSCmdlet.ShouldProcess($ProfilePath, 'Back up and replace ONLY Invoke-CodexExporterRaw')) {
    $backup="$ProfilePath.bak.codex-routing.$(Get-Date -Format yyyyMMdd-HHmmss-fff)"
    [IO.File]::Copy($ProfilePath,$backup,$false)
    $temp="$ProfilePath.codex-routing.tmp"
    try {
        [IO.File]::WriteAllText($temp,$updated,[Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temp,$ProfilePath,$true)
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
    Write-Host "Profile updated. Backup: $backup"
}
