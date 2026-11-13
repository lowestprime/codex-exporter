# No full-profile replacement. Backs up profile and installs a dot-sourced module.
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$ProfilePath = $PROFILE.CurrentUserCurrentHost)
$ErrorActionPreference='Stop'
$source = Join-Path $PSScriptRoot 'powershell\CodexExporter-ArchiveOverrides.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing $source" }
$destination = Join-Path (Split-Path -Parent $ProfilePath) 'CodexExporter-ArchiveOverrides.ps1'
$marker = '# CodexExporter-ArchiveOverrides (v0.4.0)'
$line = ". '$($destination.Replace("'", "''"))'"
if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { throw "PowerShell profile not found: $ProfilePath" }
$content = Get-Content -LiteralPath $ProfilePath -Raw -ErrorAction Stop
if ($content -notmatch 'function\s+Invoke-CodexExporterRaw') { throw 'Existing common exporter function not found; aborting.' }
if (-not $PSCmdlet.ShouldProcess($ProfilePath, 'Back up profile and install archive helper overrides')) { return }
$backup = "$ProfilePath.bak.codex-archive.$(Get-Date -Format yyyyMMdd-HHmmss-fff)"
Copy-Item -LiteralPath $ProfilePath -Destination $backup -ErrorAction Stop
Copy-Item -LiteralPath $source -Destination $destination -Force -ErrorAction Stop
$loaderPattern = "(?m)^# CodexExporter-ArchiveOverrides[^`r`n]*`r?`n[ `t]*\.[ `t]+'[^`r`n]*CodexExporter-ArchiveOverrides\.ps1'[ `t]*`r?`n?"
$clean = [regex]::Replace($content, $loaderPattern, '')
$updated = $clean.TrimEnd() + "`r`n`r`n$marker`r`n$line`r`n"
if ($updated -cne $content) {
    [IO.File]::WriteAllText($ProfilePath, $updated, [Text.UTF8Encoding]::new($false))
}
Write-Host "Profile backed up: $backup"
Write-Host "Archive wrapper installed: $destination"
Write-Host 'Open a NEW PowerShell 7 session, or dot-source the installed overrides file now.'
