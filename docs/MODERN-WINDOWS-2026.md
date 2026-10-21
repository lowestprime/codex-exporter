# Codex Exporter 0.3 — Windows 11, ChatGPT/Codex desktop, and account-export ingestion

## Scope and trust boundaries

The local Codex app and CLI record rollouts under `%USERPROFILE%\.codex\sessions` and
`archived_sessions`, with thread metadata normally under `%USERPROFILE%\.codex\state_5.sqlite`.
The desktop sidebar, `session_index.jsonl`, and rollout storage can disagree. `--audit-sessions`
reconciles the **SQLite catalog** and **files actually present**; it does not alter either.

The ChatGPT desktop app has Chat, Work and Codex views. Local Codex rollouts and cloud-synced
ChatGPT/Work chats are distinct. Cloud-only conversations are **not** discoverable by reading
`.codex`. The independent `--chatgpt-export` adapter converts conversations provided in an
**official, user-requested ChatGPT account export** when eligible records are present there.
It does not retrieve account data from the cloud or bypass workspace restrictions.

## Install in PowerShell 7

Extract the source ZIP to a permanent directory and launch PowerShell 7:

```powershell
$repo = "$HOME\Downloads\codex-exporter-0.3"
$py = py -3.13 -c 'import sys;print(sys.executable)'
& "$repo\Install-CodexThreadExporter.ps1" `
    -InstallDir 'C:\projects\CodexTools' `
    -PythonPath $py -AppendProfile -InstallPackage
. $PROFILE
cdx-audit 'Full-Time'
cdx-project 'Full-Time'
```

`-InstallPackage` ensures the **`codex-export` command** and the directly invoked script
both use the upgraded version under the selected Python interpreter. If the existing
`codex-export.exe` belongs to a different Python virtual environment, install into that
specific environment instead, or invoke `& $py C:\projects\CodexTools\Export-CodexThread.py`.
Do not assume a `python` on PATH is the executable used by your current `codex-export`.
The installer modifies only a marked PowerShell profile block and makes a backup if changed.
Use `-AppendProfile` only after inspecting the installer and helper for your environment.

## Useful commands

```powershell
# Reconcile every local SQLite thread and physical rollout; identify missing payloads.
cdx-audit
cdx-audit 'Full-Time'

# Every available full thread from the project (including archived sessions).
cdx-project 'Full-Time'

# Restrict by exact normalized workspace path rather than ambiguous basename.
cdx-project "$HOME\Desktop\job\Full-Time"

# One very large thread; progress is shown on stderr in PowerShell.
cdx-fast 01a0fecf-02ee-77d2-99a2-59e86e27cc9e

# Exact lower-level CLI usage. Over 128 MiB auto-selects streaming for full threads.
codex-export --project Full-Time --mode thread --progress --json
codex-export --session-id 01a0fecf-02ee-77d2-99a2-59e86e27cc9e --mode thread --fast --progress

# Convert conversations already available in an authorized ChatGPT account export ZIP.
cdx-chatgpt "$HOME\Downloads\chatgpt-data-export.zip"
# Restrict to a known conversation ID or title pattern.
codex-export --chatgpt-export "$HOME\Downloads\chatgpt-data-export.zip" `
    --chatgpt-title-regex 'Career|Work' --out-dir "$HOME\Documents\Codex Exports"
```

If `conversations.json` is larger than 64 MiB, install streaming JSON reader into
**the same interpreter**: `& $py -m pip install ijson`. Small account archives work
without third-party dependencies. Project IDs are filtered only when the source record
contains an explicit `project_id`; absence is never guessed to be a match.

## Streaming mode versus legacy mode

| Capability | `--mode thread` in-memory | `--mode thread --fast` |
|---|---|---|
| Record memory | Whole source record list | One JSONL line/record at a time |
| Rendered text memory | Entire Markdown string | Writes to disk incrementally |
| Source hash | One or more entire-file reads | Calculated during the input pass |
| Full per-turn Markdown tables and token counts | Yes | No; chronological event headings and provenance instead |
| Last N turns, live reconstructed context, clipboard | Supported | Unsupported (rejects incompatible combinations) |
| Progress | No standard progress | Bytes, percentage, and throughput to stderr |
| Truncation | Legacy markers and manifest | Inline upstream markers, locations and manifest audit |
| Unknown records | Manifest classification | Manifest classification; optional raw `--unknown-events sidecar` |
| CPU/memory bounds | May become prohibitive on multi-GB rollouts | Still bounded by size of **largest individual JSONL event** |

For a comprehensive local archive, keep the original rollout JSONL and the export manifest.
Unrecognized schemas may contain usable content and should be reviewed. To capture their
raw bytes separately, enable `--unknown-events sidecar`. The sidecar can include sensitive
internal or personal data; do not publish it. Do not assume that omitted upstream tool output
is recoverable, even if its marker appears multiple times in a source transcript.

## Consistency limitations

- The exporter reads active rollouts without modifying the application. When an active thread
  grows during export, it records `source_changed_during_export` and exports the initial size
  snapshot. Re-run once the thread is idle for a stable archive.
- The SQLite thread catalog may include missing rollouts. Metadata is not conversation content;
  the exporter lists missing IDs but cannot reconstruct absent JSONL.
- Truncation performed by Codex at source recording cannot be reversed.
- Non-text images/audio and other multimodal assets may not be contained as readable Markdown.
- The user-supplied ChatGPT archive converter preserves recorded branches and message text,
  not app-private hidden context. The original archive is the fidelity authority.
- Large single JSONL events still require memory proportional to that *individual* event; this
  code does not claim an absolute constant-memory guarantee for adversarial files.
- Streaming mode currently does not calculate token counts or produce the legacy turn index.
- No machine-independent benchmark should be inferred from the synthetic unit tests.

## Verification

```powershell
& $py "$repo\Export-CodexThread.py" --version
& $py "$repo\Export-CodexThread.py" --audit-sessions --project Full-Time
& $py "$repo\Export-CodexThread.py" --project Full-Time --mode thread --progress
```

For developer regression tests, run `& $py -m pytest -q` from the extracted source directory
with pytest installed. Test fixtures are synthetic: they do not verify the specific
5.4 GB rollout or newer cloud-only desktop data, neither of which was supplied in this archive.

## Repair a stale 0.2.0 console command after installing 0.3.0

If `& $py -m codex_exporter --version` reports 0.3.0 but `codex-export --version`
still reports 0.2.0, run `Get-Command codex-export -All`. A common Windows cause
is an obsolete `%LOCALAPPDATA%\codex-exporter\venvs\0.2.0\Scripts` PATH entry
preceding the selected Python `Scripts` directory.

The dedicated repair is opt-in and makes no changes to packages or older venv files:

```powershell
$py = (& py -3.13 -c 'import sys; print(sys.executable)').Trim()
& 'C:\projects\CodexTools\Repair-CodexExporterPath.ps1' -PythonPath $py -WhatIf
& 'C:\projects\CodexTools\Repair-CodexExporterPath.ps1' -PythonPath $py
Get-Command codex-export -All
codex-export --version
```

On a subsequent source installation, supply `-InstallPackage -RepairCommandPath`
to perform the same narrowly scoped reconciliation *after* pip installs the
package. Without `-RepairCommandPath`, the installer never modifies PATH.

The repair validates that the targeted Python module and executable agree,
backups original User/Process PATH values under
`%LOCALAPPDATA%\codex-exporter\backups`, removes only recognized old
`codex-exporter\venvs\*\Scripts` entries with an actual `codex-export.exe`,
and prioritizes the selected Python Scripts directory in User and current
Process PATH. It does not remove the venv itself or change Machine PATH.
If Machine PATH or shell startup scripts also inject the obsolete entry, a
warning identifies that additional source; check a newly started terminal.
Use `-SessionOnly` for a non-persistent test and `-WhatIf` to preview changes.