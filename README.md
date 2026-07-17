# Codex Exporter 0.2

Fidelity-oriented Markdown exports of local OpenAI Codex app/CLI session JSONL. The exporter remains noninteractive and automation-first by default, with optional native dialogs and a terminal browser when they are useful.

## What changed in 0.2

- persistent export-directory and filename-template configuration;
- Windows folder picker and Save As dialog through the Python standard library;
- mode-aware filenames (`thread`, `turn-0012`, `response-0012`, `last-response`, and so on);
- complete PowerShell helpers, including `cdx-sessions`;
- method-specific token metadata instead of an always-approximate label;
- deterministic batch export, last-N-turn extraction, collision policies, and stable short-ID filenames;
- optional project-aware session browser;
- compaction/rollback-aware reconstructed live-context mode;
- explicit reasoning-summary opt-in without exporting raw internal reasoning;
- recognized/ignored/unknown event accounting and sibling JSON manifests;
- narrow repair of otherwise-valid JSON lines containing unescaped Windows-path backslashes;
- explicit distinction among source-runtime loss, exporter truncation, and harmless display ellipses;
- package metadata, `codex-export` console entry point, expanded regression tests, release automation, and a compatibility matrix.
- deterministic UTF-8 standard output/error for Windows redirects and machine-readable JSON;

## Windows 11 installation

Use the same CPython interpreter that already has, or will receive, `tiktoken`:

```powershell
$ErrorActionPreference = 'Stop'
$py = (Get-Command python -ErrorAction Stop).Source

Set-Location '<CLONED_OR_EXTRACTED_CODEX_EXPORTER_DIRECTORY>'
.\Install-CodexThreadExporter.ps1 `
    -InstallDir 'C:\projects\CodexTools' `
    -PythonPath $py `
    -AppendProfile `
    -InstallSkill `
    -InstallTiktoken

. $PROFILE
cdx-tokenizer -Tokenizer tiktoken -Encoding cl100k_base -Require
cdx-sessions
```

The installer accepts custom paths, replaces exactly one marked profile block, preserves unrelated profile content, and is idempotent. It only creates a timestamped profile backup when the generated profile content actually changes.

To install the package/console entry point as well:

```powershell
.\Install-CodexThreadExporter.ps1 `
    -InstallDir 'D:\Tools\Codex Exporter' `
    -PythonPath $py `
    -AppendProfile `
    -InstallPackage `
    -InstallTiktoken
```

## Persistent output directory

The resolver precedence is:

1. `CODEX_THREAD_EXPORT_DIR` environment variable;
2. the persisted `last_out_dir` in the exporter config;
3. `<exporter-script-directory>\codex-thread-exports`.

On Windows, the persisted config is normally:

```text
%LOCALAPPDATA%\codex-exporter\config.json
```

Override its location with `CODEX_EXPORT_CONFIG` when testing or maintaining separate profiles.

```powershell
cdx-set-dir                         # native folder picker; remembers selection
cdx-set-dir 'D:\Codex Exports'     # explicit persistent default
cdx-open                            # open current default in File Explorer
cdx-config                          # inspect persisted settings
```

One-off controls:

```powershell
cdx-thread <SESSION_ID> -ChooseOutDir   # select and remember a folder
cdx-thread <SESSION_ID> -SaveAs         # native Save As dialog; remembers parent
```

At the direct CLI level, `--out-dir` and `--choose-out-dir` are remembered by default. Add `--no-remember-out-dir` for a truly one-off destination.

## PowerShell commands

| Command | Purpose |
|---|---|
| `cdx-sessions` | List active and archived local Codex sessions |
| `cdx-browse` | Optional paginated/project-aware multi-select browser |
| `cdx-thread <ID>` | Full chronological thread export |
| `cdx-thread-clip <ID>` | Full thread plus verified Unicode clipboard |
| `cdx-responses <ID>` | List prompt/response block indices |
| `cdx-turn <ID> N` | Prompt plus response block `N`, with metadata |
| `cdx-response-block <ID> N` | Response block `N` |
| `cdx-response <ID>` | Latest response, wrapped Markdown, clipboard |
| `cdx-last <ID> N` | Last `N` turns |
| `cdx-live <ID>` | Reconstructed compaction/rollback-aware live context |
| `cdx-batch <ID1>,<ID2>` | Deterministic batch export with short IDs |
| `cdx-final <ID>` | Last substantial assistant message |
| `cdx-msg <ID> N` | One message |
| `cdx-range <ID> A B` | Inclusive message range |
| `cdx-set-dir [PATH]` | Select or set persistent export directory |
| `cdx-set-template [TEMPLATE]` | Set or display persistent filename template |
| `cdx-tokenizer` | Display active tokenizer integration |
| `cdx-config` | Display persisted exporter configuration |
| `cdx-open` | Open the current export folder |

The lower-level `Invoke-CodexThreadExport` wrapper exposes every advanced option, including `-ReasoningSummaries`, `-ReportEvents`, `-StrictEvents`, `-StableFilenames`, `-Collision`, `-OpenAfter`, and tokenizer controls.

## Export modes and mode-aware names

Default filenames use:

```text
codex_{title}_{mode}_{stamp}{session_short_part}{counts}.md
```

Examples:

```text
codex_example_project_thread_FRI_07172026_015336_AM-PDT_552296lines_approx7867634tokens.md
codex_example_project_turn-0012_FRI_07172026_020000_AM-PDT_1234lines_18543cl100k_base_tokens.md
codex_example_project_response-0012_FRI_07172026_020100_AM-PDT_233lines_2981cl100k_base_tokens.md
```

Available template placeholders:

```text
{title} {mode} {stamp} {session_id} {session_short} {session_short_part}
{lines} {tokens} {token_method} {token_encoding} {counts}
```

Configure them persistently:

```powershell
cdx-set-template 'codex_{title}_{mode}_{stamp}{session_short_part}{counts}.md'
cdx-set-template   # print current template
```

Direct CLI configuration:

```powershell
codex-export --set-filename-template 'archive_{title}_{mode}{session_short_part}.md'
codex-export --print-filename-template
codex-export --reset-filename-template
```

`--stable-filenames` removes the timestamp and forces a short session ID. Batch exports include short IDs automatically. Existing-file policy is selected with `--collision rename|overwrite|skip|error`; `rename` is the safe default.

## Batch, last-N, and browsing

Repeat source options or use a line-oriented input file:

```powershell
codex-export --session-id <ID1> --session-id <ID2> --mode thread
codex-export --jsonl C:\a.jsonl --jsonl C:\b.jsonl --mode thread
codex-export --sessions-file .\sessions.txt --mode thread --stable-filenames
codex-export --session-id <ID> --mode thread --last-n-turns 5
```

`--browse` is optional and does not change the noninteractive architecture. Browser controls are:

```text
number/range  select rows       a  select current page
n / p         next/previous     m  projects/sessions view
/ text        filter            s  find ID/rollout filename
b             back              q  quit
```

## Token counting

The default is `--tokenizer auto --token-encoding cl100k_base`.

When `tiktoken` succeeds, the export records:

```yaml
token_count: 12345
token_count_method: "tiktoken"
token_encoding: "cl100k_base"
tokenizer_library: "tiktoken"
tokenizer_version: "0.12.0"
token_count_exact_for_encoding: true
token_special_text_policy: "ordinary_text; disallowed_special=()"
```

When unavailable or intentionally disabled:

```yaml
token_count_method: "regex_estimate"
token_encoding: "none"
token_count_exact_for_encoding: false
```

“Exact” means exact for the selected encoding and the exact exported Unicode text. It does **not** claim to reproduce a model API’s hidden role/tool framing, server-side accounting, or another model’s native tokenizer.

Install and validate in the exact interpreter used by the wrapper:

```powershell
$ErrorActionPreference = 'Stop'
$py = (Get-Command python -ErrorAction Stop).Source
& $py -m pip install --upgrade --only-binary=:all: 'tiktoken>=0.12,<1'

cdx-tokenizer -Tokenizer tiktoken -Encoding cl100k_base -Require

@'
from pathlib import Path
import importlib.metadata as md
import runpy
import sys
import tiktoken

script = Path(r"C:\projects\CodexTools\Export-CodexThread.py")
enc = tiktoken.get_encoding("cl100k_base")
text = "Codex exporter validation: I'll, I’ll, em dash —, emoji 🚀, 中文, code: `Get-ChildItem`"

print(f"python={sys.executable}")
print(f"script={script.resolve()}")
print(f"tiktoken={md.version('tiktoken')}")
print(f"encoding={enc.name}")
print(f"tokens={len(enc.encode(text, disallowed_special=()))}")
print(f"roundtrip={enc.decode(enc.encode(text, disallowed_special=())) == text}")
print(f"script_exists={script.is_file()}")
'@ | & $py -
```

Do not validate with `Path("Export-CodexThread.py")` from an unrelated working directory; use the installed absolute path or `$script:CodexThreadExporter`.

## Parse integrity

Every nonblank source line is attempted with strict `json.loads` first. On failure, the repair path only escapes invalid backslashes **inside JSON strings**, preserving valid JSON escapes and structural characters. It does not concatenate lines, invent braces, or guess arbitrary malformed records.

Metadata distinguishes:

```yaml
parse_error_count: 0
repaired_json_line_count: 3
```

The manifest records each repaired line number, original parser error, repair class, and repair count. Unrecoverable lines remain `parse_error_count` entries and are never silently discarded.

## Source truncation versus exporter truncation

Codex can store markers such as:

```text
…1234 tokens truncated…
```

That means the missing material was already absent from the rollout JSONL. No exporter can reconstruct it. The default `--source-truncation annotate` replaces the ambiguous marker with:

```text
[SOURCE TOOL OUTPUT TRUNCATED BY CODEX BEFORE EXPORT: 1234 tokens omitted; not recoverable from this rollout record]
```

Other policies:

```powershell
--source-truncation preserve   # keep original marker verbatim
--source-truncation error      # abort instead of exporting lossy source
```

The manifest separately counts:

- raw markers in all JSONL lines;
- markers encountered in extracted record types;
- markers remaining in the selected rendered record set.

Exporter-created tail trimming uses an unmistakable independent sentinel:

```text
[EXPORTER TOOL OUTPUT TRUNCATED: kept final N of M characters]
```

Overlong-line, binary/blob, repeated-line, ANSI/OSC, and transport-header cleanup are independently counted in the manifest.

## Event auditing and manifest

Every written export receives a sibling `.manifest.json` unless `--no-manifest` is used. It reports:

- source path, SHA-256, session ID, and source-event count;
- output path, mode, lines, tokenizer method, and exactness;
- recognized event schemas;
- known ignored schemas and explicit reasons;
- unknown schemas;
- cleaning, summarization, and redaction counts;
- repaired and unrecovered JSON lines;
- source/extracted/selected truncation counts;
- compaction and rollback reconstruction history.

Use:

```powershell
codex-export --session-id <ID> --mode thread --report-events
codex-export --session-id <ID> --mode thread --strict-events
```

`--strict-events` is suitable for regression/CI workflows where a new Codex schema should fail loudly.

## Reconstructed live context

```powershell
cdx-live <SESSION_ID>
```

The exporter applies recorded `compacted.payload.replacement_history` and `thread_rolled_back.num_turns` events and labels the result:

```yaml
history_semantics: "reconstructed_live_context"
```

This is an evidence-based reconstruction, not a claim to reproduce the server’s exact hidden context prompt. Chronological `cdx-thread` remains the authoritative source-history export.

## Reasoning-summary privacy boundary

Raw internal reasoning is not exported. `--reasoning-summaries` includes only explicit summary text already stored in Codex `response_item/reasoning.summary` records, with an opt-in label.

```powershell
Invoke-CodexThreadExport -SessionId <ID> -Mode thread -ReasoningSummaries
```

## Development and release checks

```powershell
python -m pip install -e '.[test,exact]'
python tools\sync_cli.py
python -m pytest
python -m build
```

CI covers Python 3.10 and 3.13 on Windows, macOS, and Linux; mirrored-script identity; exact `cl100k_base` smoke tests; PowerShell parsing; custom installation paths; idempotent profile replacement; archived-session discovery; every export mode; batch collisions; compaction/rollback; unknown schemas; and OS clipboard integration where a runner exposes it.

Tagged `v*` pushes build distributions and create a GitHub release. See [COMPATIBILITY.md](COMPATIBILITY.md) and [CHANGELOG.md](CHANGELOG.md).

## Security

Rollouts can contain source code, local paths, credentials, and private repository names. Use `--redact` before sharing and inspect the result. Redaction and binary/blob suppression are counted in the manifest, but pattern-based redaction is not a substitute for human review.
