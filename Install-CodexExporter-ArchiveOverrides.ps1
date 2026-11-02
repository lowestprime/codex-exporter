# No full-profile replacement. Backs up profile and installs a dot-sourced module.
[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$ProfilePath = $PROFILE.CurrentUserCurrentHost)
$ErrorActionPreference='Stop'
$source = Join-Path $PSScriptRoot 'powershell\CodexExporter-ArchiveOverrides.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing $source" }
$destination = Join-Path (Split-Path -Parent $ProfilePath) 'CodexExporter-ArchiveOverrides.ps1'
$marker = '# CodexExporter-ArchiveOverrides (v0.3.7)'
$line = ". '$($destination.Replace("'", "''"))'"
if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { throw "PowerShell profile not found: $ProfilePath" }
$content = Get-Content -LiteralPath $ProfilePath -Raw -ErrorAction Stop
if ($content -notmatch 'function\s+Invoke-CodexExporterRaw') { throw 'Existing common exporter function not found; aborting.' }
if (-not $PSCmdlet.ShouldProcess($ProfilePath, 'Back up profile and install archive helper overrides')) { return }
$backup = "$ProfilePath.bak.codex-archive.$(Get-Date -Format yyyyMMdd-HHmmss-fff)"
Copy-Item -LiteralPath $ProfilePath -Destination $backup -ErrorAction Stop
Copy-Item -LiteralPath $source -Destination $destination -Force -ErrorAction Stop
if ($content -notmatch [regex]::Escape($marker)) {
    Add-Content -LiteralPath $ProfilePath -Value "`n$marker`n$line" -Encoding utf8
} else {
    # Idempotent: don't duplicate existing dot-source entry.
    if (-not $content.Contains($line)) { throw 'Found archive marker but unexpected loader path; inspect manually.' }
}
Write-Host "Profile backed up: $backup"
Write-Host "Archive wrapper installed: $destination"
Write-Host 'Open a NEW PowerShell 7 session, or dot-source the installed overrides file now.'
