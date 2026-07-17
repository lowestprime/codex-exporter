# Installs Codex Exporter 0.2+ and safely replaces its marked PowerShell profile block.
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\projects\CodexTools',
    [string]$PythonPath = '',
    [string]$ExportDir = '',
    [string]$ProfilePath = '',
    [switch]$AppendProfile,
    [switch]$InstallSkill,
    [switch]$InstallPackage,
    [switch]$InstallTiktoken,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceScript = Join-Path $repoRoot 'Export-CodexThread.py'
$sourcePackage = Join-Path $repoRoot 'codex_exporter'
$sourceHelper = Join-Path $repoRoot 'powershell\CodexThreadExport-profile-block.v8.ps1'

foreach ($required in @($sourceScript, $sourcePackage, $sourceHelper)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required source missing: $required" }
}

if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
$PythonPath = [System.IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Python executable not found: $PythonPath" }

$installPath = [System.IO.Path]::GetFullPath($InstallDir)
$installedScript = Join-Path $installPath 'Export-CodexThread.py'
$installedPackage = Join-Path $installPath 'codex_exporter'
$installedHelper = Join-Path $installPath 'CodexThreadExport-profile-block.v8.ps1'
New-Item -ItemType Directory -Force -Path $installPath | Out-Null
Copy-Item -LiteralPath $sourceScript -Destination $installedScript -Force
if (Test-Path -LiteralPath $installedPackage) {
    Remove-Item -LiteralPath $installedPackage -Recurse -Force
}
Copy-Item -LiteralPath $sourcePackage -Destination $installedPackage -Recurse -Force

$scriptLiteral = "'" + $installedScript.Replace("'", "''") + "'"
$pythonLiteral = $PythonPath.Replace("'", "''")
$helper = (Get-Content -LiteralPath $sourceHelper -Raw).Trim()
$helper = $helper.Replace("'C:\projects\CodexTools\Export-CodexThread.py'", $scriptLiteral)
$helper = $helper.Replace('__CODEX_EXPORT_PYTHON__', $pythonLiteral)
Set-Content -LiteralPath $installedHelper -Value $helper -Encoding utf8NoBOM

& $PythonPath -m py_compile $installedScript

if ($InstallPackage) {
    & $PythonPath -m pip install --upgrade $repoRoot
}
if ($InstallTiktoken) {
    & $PythonPath -m pip install --upgrade --only-binary=:all: 'tiktoken>=0.12,<1'
}

if ($ExportDir) {
    $resolvedExportDir = [System.IO.Path]::GetFullPath($ExportDir)
    New-Item -ItemType Directory -Force -Path $resolvedExportDir | Out-Null
    & $PythonPath $installedScript --set-default-out-dir $resolvedExportDir | Out-Null
}

if ($AppendProfile) {
    $profilePath = if ($ProfilePath) { [System.IO.Path]::GetFullPath($ProfilePath) } else { $PROFILE }
    $profileDirectory = Split-Path -Parent $profilePath
    if ($profileDirectory) { New-Item -ItemType Directory -Force -Path $profileDirectory | Out-Null }
    if (Test-Path -LiteralPath $profilePath) {
        $existing = Get-Content -LiteralPath $profilePath -Raw
    } else {
        $existing = ''
    }

    $begin = '# >>> Codex Thread Exporter helpers >>>'
    $end = '# <<< Codex Thread Exporter helpers <<<'
    $pattern = '(?s)' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end)

    if ([regex]::IsMatch($existing, $pattern)) {
        $updated = [regex]::Replace($existing, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $helper }, 1)
    } elseif ($existing.Contains('# Codex Thread Exporter PowerShell helper functions')) {
        $legacyPattern = '(?s)# Codex Thread Exporter PowerShell helper functions.*?(?=# List all functions defined in this profile script|\z)'
        $updated = [regex]::Replace($existing, $legacyPattern, $helper + "`r`n`r`n", 1)
    } else {
        $updated = $existing.TrimEnd() + "`r`n`r`n" + $helper.Trim() + "`r`n"
    }
    if ($updated -cne $existing) {
        if (Test-Path -LiteralPath $profilePath) {
            Copy-Item -LiteralPath $profilePath -Destination "$profilePath.codex-exporter-backup-$(Get-Date -Format yyyyMMdd-HHmmss-fff)" -Force
        }
        Set-Content -LiteralPath $profilePath -Value $updated -Encoding utf8NoBOM
    }
}

if ($InstallSkill) {
    $skillSource = Join-Path $repoRoot 'skills\codex-thread-export'
    $skillTarget = Join-Path $HOME '.agents\skills\codex-thread-export'
    if (Test-Path -LiteralPath $skillTarget) {
        if (-not $Force) {
            throw "Skill already exists: $skillTarget. Re-run with -Force to replace it."
        }
        Remove-Item -LiteralPath $skillTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $skillTarget) | Out-Null
    Copy-Item -LiteralPath $skillSource -Destination $skillTarget -Recurse -Force
}

[pscustomobject]@{
    Version = (& $PythonPath $installedScript --version | Out-String).Trim()
    Python = $PythonPath
    InstalledScript = $installedScript
    ProfileUpdated = [bool]$AppendProfile
    SkillInstalled = [bool]$InstallSkill
    PackageInstalled = [bool]$InstallPackage
    Tokenizer = (& $PythonPath $installedScript --tokenizer-info --tokenizer auto | Out-String).Trim()
}
