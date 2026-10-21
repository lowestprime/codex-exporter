# Reconcile the codex-export command's PATH after installation into a selected Python.
# Only removes shadowing PATH entries for legacy codex-exporter-specific virtual environments.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$PythonPath,
    [switch]$SessionOnly
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'PATH repair is supported on Windows only.' }

$python = [System.IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python interpreter not found: $python"
}
$scripts = Join-Path (Split-Path -Parent $python) 'Scripts'
$desiredExe = Join-Path $scripts 'codex-export.exe'
if (-not (Test-Path -LiteralPath $desiredExe -PathType Leaf)) {
    throw "Console entry point not installed in selected Python: $desiredExe"
}
$expected = (& $python -m codex_exporter --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Selected Python failed to import codex_exporter: $python" }
$direct = (& $desiredExe --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $direct -cne $expected) {
    throw "Package and console executable disagree: module='$expected', executable='$direct'"
}

# This root is dedicated to old codex-exporter venvs. Never delete the environments.
$legacyRoot = Join-Path $env:LOCALAPPDATA 'codex-exporter\venvs'
function Normalize-PathEntry {
    param([string]$Entry)
    $value = [Environment]::ExpandEnvironmentVariables($Entry.Trim().Trim('"'))
    if (-not $value) { return '' }
    try { return [System.IO.Path]::GetFullPath($value).TrimEnd('\') }
    catch { return $value.TrimEnd('\') }
}
function Test-LegacyExporterScripts {
    param([string]$Entry)
    $normalized = Normalize-PathEntry $Entry
    $root = Normalize-PathEntry $legacyRoot
    if (-not $normalized.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $relative = $normalized.Substring($root.Length + 1)
    if ($relative -notmatch '^([^\\]+)\\Scripts$') { return $false }
    # Avoid removing paths that do not contain this command.
    return (Test-Path -LiteralPath (Join-Path $normalized 'codex-export.exe') -PathType Leaf)
}
function Reconcile-Path {
    param([string]$PathValue)
    $kept = [System.Collections.Generic.List[string]]::new()
    $removed = [System.Collections.Generic.List[string]]::new()
    $target = Normalize-PathEntry $scripts
    foreach ($raw in ($PathValue -split ';')) {
        if (-not $raw.Trim()) { continue }
        if (Test-LegacyExporterScripts $raw) {
            $removed.Add($raw)
        } elseif ((Normalize-PathEntry $raw) -ine $target) {
            # Preserve every unrelated entry, including its spelling and order.
            $kept.Add($raw)
        }
    }
    return [pscustomobject]@{
        Value   = (@($scripts) + @($kept)) -join ';'
        Removed = @($removed)
    }
}

$beforeProcess = [Environment]::GetEnvironmentVariable('Path', 'Process')
$beforeUser = [Environment]::GetEnvironmentVariable('Path', 'User')
$beforeMachine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$processFix = Reconcile-Path $beforeProcess
$userFix = Reconcile-Path $beforeUser
$machineLegacy = @($beforeMachine -split ';' | Where-Object { Test-LegacyExporterScripts $_ })
$backupPath = $null
$userChanged = $false
$processChanged = $false

if (-not $SessionOnly -and $userFix.Value -cne $beforeUser) {
    if ($PSCmdlet.ShouldProcess('User PATH', 'Back up PATH and prioritize selected codex-export.exe')) {
        $backupDir = Join-Path $env:LOCALAPPDATA 'codex-exporter\backups'
        New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
        $backupPath = Join-Path $backupDir ('path-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.json')
        [pscustomobject]@{
            original_user_path = $beforeUser
            original_process_path = $beforeProcess
            desired_executable = $desiredExe
            removed_user_entries = $userFix.Removed
            removed_process_entries = $processFix.Removed
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backupPath -Encoding utf8NoBOM
        [Environment]::SetEnvironmentVariable('Path', $userFix.Value, 'User')
        $userChanged = $true
    }
}
if ($processFix.Value -cne $beforeProcess) {
    if ($PSCmdlet.ShouldProcess('Process PATH', 'Prioritize selected codex-export.exe')) {
        [Environment]::SetEnvironmentVariable('Path', $processFix.Value, 'Process')
        $processChanged = $true
    }
}


# Ask Explorer and other shell hosts to reload persistent environment values.
# Existing unrelated processes retain their inherited environment until restarted.
if ($userChanged) {
    try {
        if (-not ('CodexExporter.EnvironmentNotifier' -as [type])) {
            Add-Type -Namespace CodexExporter -Name EnvironmentNotifier -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hwnd, uint msg, System.UIntPtr wparam, string lparam, uint flags, uint timeout, out System.UIntPtr result);
'@
        }
        $sent = [UIntPtr]::Zero
        [void][CodexExporter.EnvironmentNotifier]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 0x2, 3000, [ref]$sent)
    } catch {
        Write-Warning "Persistent User PATH was updated, but notifying running shell hosts failed: $_"
    }
}

$resolved = $null
$actual = $null
if (-not $WhatIfPreference) {
    $resolved = (Get-Command codex-export -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $actual = (& $resolved --version | Out-String).Trim()
    if ((Normalize-PathEntry $resolved) -ine (Normalize-PathEntry $desiredExe) -or $actual -cne $expected) {
        throw "PATH repair did not resolve to the selected executable: resolved='$resolved', version='$actual'; expected='$desiredExe', version='$expected'. Inspect Machine PATH or PowerShell startup files."
    }
}
if ($machineLegacy.Count) {
    Write-Warning ('An older codex-exporter venv is also present in Machine PATH; it may return in new sessions: ' + ($machineLegacy -join '; '))
}
if (-not $SessionOnly -and $processFix.Removed.Count -gt 0 -and $userFix.Removed.Count -eq 0 -and $machineLegacy.Count -eq 0) {
    Write-Warning 'Legacy PATH entry was not present in User or Machine PATH. A shell startup script or parent process may be injecting it; inspect your PowerShell profiles if it returns.'
}
[pscustomobject]@{
    ExpectedVersion    = $expected
    ConsoleExecutable  = $desiredExe
    ResolvedExecutable = $resolved
    ResolvedVersion    = $actual
    UserPathUpdated    = $userChanged
    ProcessPathUpdated = $processChanged
    RemovedUserEntries = $userFix.Removed
    RemovedSessionEntries = $processFix.Removed
    Backup             = $backupPath
    SessionOnly        = [bool]$SessionOnly
}