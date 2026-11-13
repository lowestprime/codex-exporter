#requires -Version 7.0
# Updates only the raw invoker, preserving helper definitions and all unrelated profile code.
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$ProfilePath = $PROFILE.CurrentUserCurrentHost, [string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { throw "Profile not found: $ProfilePath" }
$source = Join-Path $PSScriptRoot 'powershell/CodexThreadExport-profile-block.v8.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Current helper source missing: $source" }
$original = [IO.File]::ReadAllText($ProfilePath)
$startTag = 'function Invoke-CodexExporterRaw {'
$nextTag = 'function Invoke-CodexThreadExport {'
$start = $original.IndexOf($startTag, [StringComparison]::Ordinal)
if ($start -lt 0) { throw 'Exporter helper block not found; no changes made.' }
$end = $original.IndexOf($nextTag, $start, [StringComparison]::Ordinal)
if ($end -lt 0) { throw 'Exporter function boundary not found; no changes made.' }
if ($original.IndexOf($startTag, $start+1, [StringComparison]::Ordinal) -ge 0) { throw 'Ambiguous duplicate helper definitions; no changes made.' }
$current = [IO.File]::ReadAllText($source)
$currentStart = $current.IndexOf($startTag, [StringComparison]::Ordinal)
$currentEnd = $current.IndexOf($nextTag, $currentStart, [StringComparison]::Ordinal)
$replacement = $current.Substring($currentStart, $currentEnd - $currentStart)
if ($PythonPath) {
    $python = [IO.Path]::GetFullPath($PythonPath)
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python executable not found: $python" }
    $anchor = '    if (-not [System.IO.File]::Exists($script:CodexExportPython)) {'
    $replacement = $replacement.Replace($anchor, "    `$script:CodexExportPython = '$($python.Replace("'", "''"))'`r`n" + $anchor)
}
$updated=$original.Substring(0,$start)+$replacement+$original.Substring($end)
if ($updated -ceq $original) { Write-Host 'Profile routing already current.'; return }
if ($PSCmdlet.ShouldProcess($ProfilePath, 'Back up and replace ONLY Invoke-CodexExporterRaw')) {
    $backup="$ProfilePath.bak.codex-routing.$(Get-Date -Format yyyyMMdd-HHmmss-fff)"
    [IO.File]::Copy($ProfilePath,$backup,$false)
    $temp="$ProfilePath.codex-routing.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temp,$updated,[Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temp,$ProfilePath,$true)
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
    Write-Host "Profile updated. Backup: $backup"
}
