# Windows 11 upgrade guide: 0.1.x to 0.2.0

This guide assumes the repository is cloned at
`C:\projects\CodexTools\CodexThreadExporter` and the standalone installed script
is `C:\projects\CodexTools\Export-CodexThread.py`.

## 1. Update the repository safely

```powershell
$ErrorActionPreference = 'Stop'
Set-Location 'C:\projects\CodexTools\CodexThreadExporter'

git status --short
git fetch --prune --tags origin
git switch main
git pull --ff-only origin main
```

Stop if `git status --short` is not empty; preserve or commit unrelated local
work before upgrading.

## 2. Install the new standalone script, package, helpers, and skill

```powershell
$ErrorActionPreference = 'Stop'
$py = (Get-Command python -ErrorAction Stop).Source

.\Install-CodexThreadExporter.ps1 `
    -InstallDir 'C:\projects\CodexTools' `
    -PythonPath $py `
    -AppendProfile `
    -InstallSkill `
    -InstallPackage `
    -InstallTiktoken `
    -Force
```

The installer replaces the legacy exporter helper region in `$PROFILE`, keeps
all unrelated profile content, pins the exact Python interpreter, and creates a
profile backup only when content changes.

Reload the profile:

```powershell
. $PROFILE
```

## 3. Validate the installation

```powershell
Get-Command cdx-sessions, cdx-thread, cdx-turn, cdx-response, cdx-last, cdx-live, cdx-batch
cdx-tokenizer -Tokenizer tiktoken -Encoding cl100k_base -Require
cdx-config
cdx-sessions

python 'C:\projects\CodexTools\Export-CodexThread.py' --version
codex-export --version
```

Expected version: `codex-export 0.2.0`.

Run a small functional export before a full large thread:

```powershell
cdx-responses <SESSION_ID>
cdx-turn <SESSION_ID> 1
```

Then verify the new full-thread path:

```powershell
cdx-thread <SESSION_ID>
```

The output filename should include `_thread_`, and a sibling
`.manifest.json` should be created.

## 4. Choose the persistent default output directory

```powershell
cdx-set-dir                         # native folder chooser
cdx-set-dir 'D:\Codex Exports'     # explicit path
cdx-open
```

The environment variable `CODEX_THREAD_EXPORT_DIR` overrides persisted state.
Remove or update that variable when testing `cdx-set-dir` behavior.

## 5. Optional cleanup after validation

The 0.2 installer no longer needs the old root-level helper copies. After all
commands above succeed:

```powershell
Remove-Item -LiteralPath 'C:\projects\CodexTools\CodexThreadExport-profile-block.ps1' -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'C:\projects\CodexTools\CodexThreadExport-profile-block.v7.ps1' -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'C:\projects\CodexTools\__pycache__' -Recurse -Force -ErrorAction SilentlyContinue
```

Keep existing export directories unless you have reviewed and intentionally
archived or deleted their contents.

## Testing a pull request before merge

```powershell
Set-Location 'C:\projects\CodexTools\CodexThreadExporter'
gh pr checkout <PR_NUMBER>
python -m pip install -e '.[test,exact]'
python tools\sync_cli.py
python -m pytest
```

Run the installer from that branch using the command in section 2. Return to
`main` after the PR is merged:

```powershell
git switch main
git pull --ff-only origin main
```
