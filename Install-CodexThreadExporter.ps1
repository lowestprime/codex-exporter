# Install-CodexThreadExporter.ps1
# Installs the Codex Thread Exporter and (optionally) the Codex skill on Windows.
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\projects\CodexTools',
    [switch]$AppendProfile,
    [switch]$InstallSkill,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootScript = Join-Path $repoRoot 'Export-CodexThread.py'
if (-not (Test-Path $rootScript)) {
    throw "Source script not found: $rootScript"
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'codex-thread-exports') | Out-Null

$installedScript = Join-Path $InstallDir 'Export-CodexThread.py'
$installedHelper = Join-Path $InstallDir 'CodexThreadExport-profile-block.ps1'
Copy-Item $rootScript $installedScript -Force
Copy-Item (Join-Path $repoRoot 'powershell\CodexThreadExport-profile-block.v7.ps1') $installedHelper -Force

python -m py_compile $installedScript

if ($AppendProfile) {
    $profilePath = $PROFILE
    $profileDir = Split-Path -Parent $profilePath
    if ($profileDir -and -not (Test-Path $profileDir)) {
        New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    }
    $marker = '# >>> Codex Thread Exporter helpers >>>'
    $existing = if (Test-Path $profilePath) { Get-Content $profilePath -Raw } else { '' }
    if (-not $existing.Contains($marker) -or $Force) {
        $block = Get-Content $installedHelper -Raw
        Add-Content -Path $profilePath -Value "`r`n$marker`r`n$block`r`n# <<< Codex Thread Exporter helpers <<<`r`n"
    }
}

if ($InstallSkill) {
    $skillSource = Join-Path $repoRoot 'skills\codex-thread-export'
    $skillTarget = Join-Path $HOME '.agents\skills\codex-thread-export'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $skillTarget) | Out-Null

    if (Test-Path $skillTarget) {
        if (-not $Force) {
            Write-Warning "Skill target already exists: $skillTarget. Re-run with -Force to overwrite."
        } else {
            Remove-Item -Recurse -Force $skillTarget
            Copy-Item $skillSource $skillTarget -Recurse -Force
        }
    } else {
        Copy-Item $skillSource $skillTarget -Recurse -Force
    }

    # Always sync the bundled script copy with the canonical root script so the
    # skill remains self-contained even if the repo's bundled copy drifts.
    $skillScriptDir = Join-Path $skillTarget 'scripts'
    New-Item -ItemType Directory -Force -Path $skillScriptDir | Out-Null
    Copy-Item $rootScript (Join-Path $skillScriptDir 'Export-CodexThread.py') -Force
}

[pscustomobject]@{
    InstalledScript = $installedScript
    HelperBlock     = $installedHelper
    ExportDir       = Join-Path $InstallDir 'codex-thread-exports'
    ProfileUpdated  = [bool]$AppendProfile
    SkillInstalled  = [bool]$InstallSkill
    Version         = (& python $installedScript --version) 2>&1 | Out-String
}
