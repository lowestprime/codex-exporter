# Codex Exporter 0.3.11 — live exports, one LZMA ZIP, readable Windows files

Export a running Codex thread into **one verified ZIP containing the original
three files**: rendered Markdown, its JSON manifest, and the captured native
JSONL. Ordinary exports leave no `.review.zip` or `.review` directory. Maximum
configured LZMA settings are restored: level 9, a 256 MiB dictionary, word size
64, and 19 requested threads, using a compatible external 7-Zip CLI.

```powershell
cdx-thread '11111111-2222-4333-8444-555555555555'
# Direct equivalent:
codex-export --session-id '11111111-2222-4333-8444-555555555555' --mode thread
```

The printed path and `--open-after` select the final ZIP. Exporting continues to
work while Codex appends new events: each export captures a verified, fixed
prefix, and later events appear in the next export. The app-owned rollout is
never moved, deleted, or modified.

## Default output and optional review text

| Selection | Generated output after successful cleanup |
|---|---|
| Default export | One `<stem>.zip` with `<stem>.md`, `<stem>.manifest.json`, and `<stem>.jsonl` |
| `--review` | The same ZIP, additionally containing bounded review TXT files, index, and guide |
| `--no-review` | The original three-file ZIP; this is the default |
| `--no-archive` | Loose export files; explicitly requested review adds a text directory, never a ZIP |
| `--no-archive-cleanup` | Verified ZIP plus retained loose export files |
| `--no-source-copy` | Omit native JSONL, for example when using rendered-content redaction |
| `--review-from PATH` | Explicit standalone conversion to a `.review.zip` and retained `.review` directory |

Additional review content is opt-in and goes inside the existing archive during
an ordinary archived export. `--review-from` remains available when a receiving
application needs individual TXT files or a Deflate review ZIP. Choosing that
standalone converter explicitly requests its separate outputs. With
`--no-archive`, an explicit review request creates verified text in one `.review`
directory and no ZIP. `--no-archive` alone creates only the loose ordinary export.

Review parts preserve every byte of the rendered Markdown, including code, long
lines, and embedded payload text. Each numbered TXT part is at most **1,000,000
UTF-8 bytes including its header** by default. `--review-chunk-bytes` accepts
4,096 through 1,500,000 bytes. Unicode characters remain intact; long lines may
span parts. The index records original byte/line ranges and hashes. `RECENT.txt`
is a labeled excerpt, not the complete thread. These are byte bounds, not exact
token counts or a promise that a receiving model has read the complete export.

```powershell
# Add review text inside the ordinary archive for this export.
codex-export --session-id UUID --mode thread --review --review-chunk-bytes 1000000
# Persist the opt-in setting, or restore the three-file default.
codex-export --set-archive-default review=true
codex-export --set-archive-default review=false
```

Review creation preserves the selected mode and rendering/redaction choices; it
does not summarize or truncate the rendered Markdown again. Native JSONL remains
the authority for records omitted, transformed, or unrecognized by a renderer.
See [integrity semantics](docs/INTEGRITY-AND-MANIFEST.md).

## Upgrade and restore the requested compression settings

Use the interpreter that runs your existing exporter. Installing a release does
not erase explicitly saved preferences, so installations that selected Deflate
in 0.3.10 should restore LZMA explicitly:

```powershell
$ErrorActionPreference = 'Stop'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
$archiver = 'C:\Program Files\7-Zip-Zstandard\7z.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'Select the Python interpreter used by your existing exporter.'
}
if (-not (Test-Path -LiteralPath $archiver -PathType Leaf)) {
    throw 'Select the installed compatible 7-Zip CLI.'
}
& $py -m pip install --upgrade --no-deps 'https://github.com/lowestprime/codex-exporter/releases/download/v0.3.11/codex_exporter-0.3.11-py3-none-any.whl'
if ($LASTEXITCODE -ne 0) { throw 'Exporter upgrade failed.' }
& $py -m codex_exporter --version
if ($LASTEXITCODE -ne 0) { throw 'Exporter version check failed.' }
& $py -m codex_exporter `
    --set-archive-default archive=true `
    --set-archive-default "archiver=$archiver" `
    --set-archive-default method=LZMA `
    --set-archive-default level=9 `
    --set-archive-default backend=auto `
    --set-archive-default dictionary=256m `
    --set-archive-default word_size=64 `
    --set-archive-default threads=19 `
    --set-archive-default copy_source=true `
    --set-archive-default cleanup=true `
    --set-archive-default review=false
if ($LASTEXITCODE -ne 0) { throw 'Archive configuration update failed.' }
& $py -m codex_exporter --show-archive-config
```

The example selects the quoted installed archiver path explicitly. Other saved
paths remain supported. When no path is saved, the
exporter searches for a compatible CLI. `backend=auto` selects an external
backend for LZMA; `backend=python` supports Deflate and must be changed when
selecting LZMA. A configured executable is checked before a large snapshot is
captured. Existing output locations, filenames, and other saved preferences
remain available. Package-routed `cdx-*` helpers use the updated package; copied
standalone scripts must also be updated. See the [Windows upgrade guide](docs/WINDOWS-UPGRADE.md).

The release assets include checksums. Compare the actual downloaded wheel with
its published SHA-256 before installation when using a pinned deployment; a
checksum is tied to those exact built bytes, not to the version string alone.

## Archive compression, verification, and performance

The default settings are `method=LZMA`, `level=9`, `dictionary=256m`,
`word_size=64`, `threads=19`, `backend=auto`, `archive=true`, `copy_source=true`,
`cleanup=true`, and `review=false`. The thread value is the requested backend
limit, not a guarantee that every compression phase uses 19 CPU threads. Ratio,
memory consumption, and runtime depend on the source, encoder, and machine.
These settings preserve the requested compression policy; no universal best
ratio or fixed speedup is claimed.

```powershell
# One-export overrides do not change persisted preferences.
codex-export --mode thread --session-id UUID --archive-method LZMA --archive-level 9
# Optional compatibility choice, only when explicitly requested:
codex-export --mode thread --session-id UUID --archive-backend python --archive-method Deflate --archive-level 6
codex-export --mode thread --session-id UUID --no-archive-cleanup
codex-export --show-archive-config
```

The exporter independently decompresses the final member inventory and compares
SHA-256 hashes and byte lengths before removing loose exports. Existing archives
are never overwritten. Detected changes to loose members stop cleanup. Failure
preserves recoverable loose outputs; cleanup applies only after successful
verification and publication. Export output paths are checked for source and
output aliases, including existing hard links, before writing. An explicit
manifest path cannot overwrite the live source or the rendered Markdown.
Explicit unknown-event sidecars remain separate
and are retained; they are not part of the default three-file layout.

An external archiver may use Store for a short or incompressible member. The
validator accepts that method-0 fallback while rejecting unrequested compression
methods and a substantive entirely uncompressed archive. LZMA is ZIP method 14;
Python supports decoding it, but [some other extractors do not](https://docs.python.org/3.13/library/zipfile.html#zipfile.ZIP_LZMA).
Windows file access, successful ZIP decoding, and receiving-model ingestion are
separate checks. Restoring access permissions does not alter the compression
method or imply that a model has consumed every archived token.

## Windows access permissions and existing exports

Version 0.3.11 publishes files on Windows by creating fresh destination files and
streaming verified bytes into them. The new file inherits the selected output
folder's ACL. It does not retain private staging permissions, change source
permissions, grant access to Everyone, or change ownership. Private staging
remains private. POSIX systems retain inexpensive hard-link publication when
available, with a verified-copy fallback.

This addresses a code path matching [CPython issue #157876](https://github.com/python/cpython/issues/157876):
`mkdir(mode=0o700)` and temporary directories can receive a protected OWNER
RIGHTS ACL that excludes an ordinary interactive token when created by an
elevated process. Microsoft's documentation confirms that [newly created files
inherit parent permissions, while moves do not](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights),
and that [hard links share the underlying file's security descriptor](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw).
This mechanism explains a possible access-denied regression; a screenshot alone
does not establish a particular file's owner, ACL, or corruption. Diagnose the
actual file and parent ACL before attributing a local failure.

```powershell
& $py -m codex_exporter --diagnose-access 'C:\Exports\codex_example.zip'
& $py -m codex_exporter --diagnose-access 'C:\Exports\codex_example.review'
# Explicit repair of selected previous exporter artifacts:
& $py -m codex_exporter --repair-access 'C:\Exports\codex_example.zip'
& $py -m codex_exporter --repair-access 'C:\Exports\codex_example.review'
if ($LASTEXITCODE -ne 0) { throw 'Review the reported access-repair failures.' }
```

Both options are repeatable and return JSON. Diagnostics attempt actual reading
and report owner SID, protected/inherited/explicit DACL entries, and the parent
ACL. Repair uses the Windows system `icacls` to [reset an explicit artifact to
its parent-inherited ACL](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls).
Only recognized `codex_` export files and a validated flat `codex_*.review` tree
are accepted. Custom-named exports can be diagnosed but are deliberately outside
this repair scope. Broad output roots, app-owned `.codex` paths, wildcards,
symlinks, junctions, multiply linked files, and unexpected review entries are
rejected before repair starts.

Repair needs existing permission to change the selected DACL. It never takes
ownership or elevates itself. A protected export may therefore require the same
account/token that created it. The output parent must already grant access to
the intended user; resetting a child cannot repair a restrictive parent. Before
and after diagnostics and each result are returned, with a failing exit status
if a repair remains unsuccessful.

## Measured progress for each stage

Progress is sent to **stderr** so stdout remains the final path or `--json`
result. Interactive terminals receive a compact colored bar, icons, elapsed
time, measured throughput, and ETA when the stage has a meaningful denominator.
Compression displays the percentages actually reported by 7-Zip; it does not
turn those percentages into invented byte throughput or a guessed overall job
percentage. Unknown-duration work displays activity and elapsed time.

```powershell
codex-export --session-id UUID --mode thread --progress-style auto
codex-export --session-id UUID --mode thread --progress-style rich
codex-export --session-id UUID --mode thread --progress-style plain
codex-export --session-id UUID --mode thread --progress-style json 2> progress.jsonl
codex-export --session-id UUID --mode thread --no-progress
```

`auto` uses the terminal display when supported and sparse plain events when
redirected. `rich` requests the built-in terminal display, with no extra package
dependency. `NO_COLOR`, narrow terminals, non-Unicode encodings, redirection, and
`TERM=dumb` are handled. `json` emits machine-readable stage events; `off` and
`--no-progress` disable progress output. Updates are throttled to limit overhead.

Snapshot copying, prefix verification, source-copy checks, rendering, optional
review generation, compression, independent member verification,
publication, and cleanup receive distinct stages. Windows publication separates
its source checksum, copy, and readback verification, so a completed copy is not
presented as the end of archive verification. The 7-Zip runner drains output
continuously, retains bounded diagnostic text, and cancels its child process if
interrupted.

## How a live export is defined

1. Open the rollout and record its initial byte length from that handle.
2. Copy exactly that prefix in bounded chunks and SHA-256-verify it against a
   second bounded read through the same handle. Appends are allowed; detected
   truncation, replacement, or prefix rewriting stops capture.
3. Retain complete records. Valid and narrowly repairable EOF records without
   newlines are retained. Invalid or unfinished unterminated tails are deferred
   with byte/hash metadata. Completed malformed records remain in the native copy
   and parse audit.
4. Render and archive that capture. Later events appear in the next export;
   each source in a batch has its own boundary.

The 0.3.9 live snapshot contract is preserved. Both renderers retain original-path
provenance, record `source_snapshot`, and hash the captured decoded JSONL.
Compressed rollouts are decoded once with complete-frame verification. This is a
verified append-only prefix, not an atomic filesystem snapshot against arbitrary
in-place writers. Codex is not paused.

## Make an existing export reviewable

No live Codex installation or session lookup is needed:

```powershell
codex-export --review-from 'C:\Exports\previous-export.zip'
codex-export --review-from 'C:\Exports\previous-export.md' --out-dir 'C:\Exports\Review'
```

The explicit standalone converter creates a Deflate `.review.zip` and retained
TXT directory. Add `--no-archive` to request only the verified TXT directory. A legacy LZMA ZIP is decoded with Python's ZIP reader; only its
Markdown and matching manifest are read and CRC-checked. The native JSONL member
is not opened. The original remains intact, and the index distinguishes checked
members from inherited source-hash provenance. Ambiguous, duplicate, encrypted,
or unsafe member inventories are rejected.

If a receiving chat cannot access a ZIP, attach the guide, recent excerpt, and
relevant numbered TXT parts directly. The index identifies additional history.
An upload's success does not establish complete model ingestion.

## Validation scope

Regression coverage includes live-source growth, snapshot boundaries, default
three-member archives, optional embedded review text, exact review reconstruction,
Unicode and size caps, LZMA/Deflate decoding, CRC/hash failures, collisions, copy
failures, progress framing, and child-process cancellation. Windows-specific
tests construct distinct private staging and destination DACLs, verify inherited
permissions on published files, read back a LZMA ZIP, and exercise scoped repair
without changing original or parent ACLs. Those tests run on Windows rather than
being simulated with POSIX mode bits.

The CI matrix targets Windows/macOS/Linux on Python 3.10 and 3.13, plus package,
PowerShell, and mirrored-script validation. Consult the release's Actions runs
for actual results. No private archive is required by or included in the tests.

[0.3.11 release notes](docs/releases/v0.3.11.md) · [Changelog](CHANGELOG.md)

---

## Earlier features and detailed usage

Fidelity-oriented Markdown exports of local OpenAI Codex app/CLI session JSONL. The exporter remains noninteractive and automation-first by default, with optional native dialogs and a terminal browser when they are useful.

## Source-event coverage fix in 0.3.2

Streaming export now recognizes current capitalized desktop `item_completed` records for user/agent messages, file changes, extensions, image paths and MCP completions. Known reasoning-status and token-usage records are no longer misreported as unknown schemas; only explicit reasoning summaries are opt-in. Original JSONL remains authoritative.

## Windows hotfix in 0.3.1

The streaming filename builder now sanitizes Windows-reserved characters in session
names, including Codex `/goal` titles that contain `C:\Users\...` paths.
This resolves `WinError 123` before the first JSONL event is read. Filename
substitutions are bounded and always create a single `.md` file in the selected
output directory, never unintended nested directories. Large multiline titles
are condensed to one listing row, at most 140 characters by default:

```powershell
cdx-sessions                                    # compact, valid Markdown table
codex-export --list-sessions --full-session-titles  # expanded one-line titles
cdx-project 'Full-Time' -UnknownSidecar
cdx-fast 11111111-2222-4333-8444-555555555555
```

Missing rollout JSONL files listed by `cdx-audit` are **not** repaired or
fabricated; the SQLite catalog alone cannot reconstruct transcript content.

## What changed in 0.3

- SQLite thread catalog reconciliation for newer Windows Codex desktop sessions, including
  missing-rollout diagnostics and Windows long-path normalization;
- `codex-export --project Full-Time --mode thread` and PowerShell `cdx-project`, `cdx-audit`, and `cdx-fast`;
- bounded-memory chronological Markdown writing for full threads >=128 MiB by default,
  with byte-accurate streaming SHA-256 and progress on stderr;
- upstream source-truncation locations, source-event audit, and optional raw unknown-event sidecars;
- opt-in ingestion of official ChatGPT account export ZIP/`conversations.json` records
  through `--chatgpt-export`, including recorded alternate branches;
- fixes for standalone script/skill distribution, with new module dependencies bundled;
- regression fixtures for the catalog, streaming, project selection, and account archive adapter.

Full instructions and limitations: [docs/MODERN-WINDOWS-2026.md](docs/MODERN-WINDOWS-2026.md). The user-specific input audit is distributed privately with the downloadable review package, not committed to this public repository.

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

Where enabled by the selected renderer, overlong-line, binary/blob, repeated-line, ANSI/OSC, and transport-header cleanup are independently counted in the manifest. Review companions preserve the resulting Markdown exactly and add no such cleanup.

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

### Windows PATH conflict repair (opt-in)

After updating the package, `codex-export --version` may still invoke an old
`%LOCALAPPDATA%\codex-exporter\venvs\0.2.0\Scripts\codex-export.exe`
when that directory precedes the selected Python `Scripts` on PATH.
Run `Get-Command codex-export -All` to confirm. The installer now ships
`Repair-CodexExporterPath.ps1` alongside the installed exporter; run:

```powershell
$py = (& py -3.13 -c 'import sys; print(sys.executable)').Trim()
& 'C:\projects\CodexTools\Repair-CodexExporterPath.ps1' -PythonPath $py
codex-export --version
```

Or add `-RepairCommandPath` to a future installer invocation that also uses
`-InstallPackage`. This is intentionally opt-in: the repair backs up your
PATH and modifies only User and current-process PATH values, without deleting
the older venv or changing Machine PATH. See
[Windows usage notes](docs/MODERN-WINDOWS-2026.md).

### Large ChatGPT account exports (numbered shards, v0.3.3)

ChatGPT data backups may contain `conversations-000.json` ... `conversations-NNN.json` instead of a single `conversations.json`. The exporter accepts either the **original ZIP** or an **extracted directory**, selects shards in numeric order, and refuses missing/duplicate shards. It never extracts the ZIP or touches the original backup. For the largest shards, install the optional bounded-memory parser first: `py -3.13 -m pip install "codex-exporter[chatgpt]"`.

```powershell
$dir = Join-Path $HOME '.mychatarchive\imports\<name-of-your-extracted-backup>'
codex-export --chatgpt-export $dir --chatgpt-audit --chatgpt-title-regex 'Codex|Full-Time|Career Application' > "$HOME\Documents\chatgpt-backup-audit.json"
codex-export --chatgpt-export $dir --chatgpt-title-regex 'Codex|Full-Time|Career Application' --out-dir "$HOME\Documents\ChatGPT Archive Exports"
```

For `--chatgpt-audit` without filters, `matched` includes up to 1,000 inventory entries, but the aggregate count includes all records; the scan is read-only. For a targeted scan, filter by a title regex or exact exported conversation ID. Source member provenance is attached to each exported conversation. A ChatGPT backup **does not replace local Codex rollout files**; IDs are not interchangeable and there is no supported mechanism to restore cloud ChatGPT JSON as an original Codex Desktop thread.


### Compressed Codex rollouts (`.jsonl.zst`)

New in v0.3.4: Codex cold-storage rollouts are discoverable and readable without modifying originals. Install the optional decoder with `py -3.13 -m pip install "codex-exporter[zstd]"` (or `py -3.13 -m pip install zstandard`), then run `cdx-audit Full-Time` and `cdx-project Full-Time -UnknownSidecar`. Plain `.jsonl` wins when both forms exist. SQLite catalogs may retain a `.jsonl` path while the compressed sibling remains available. Only a complete JSONL/codec validation establishes content integrity.


### v0.3.5 title and streaming behavior

The latest valid `thread_name` in `session_index.jsonl` takes precedence over stale SQLite thread titles; titles and filenames therefore reflect durable renames when available. The original SQLite/JSONL files are never changed. Stream-mode exports now include YAML frontmatter and a compact export map; full turn grouping and exact output token/line counts remain exclusive to classic mode. Filename templates are honored where the fields are computable before a stream; `{counts}` is left blank and the short session UUID is always appended if absent. Use `Update-CodexExporter-ProfileRouting.ps1` (supplied separately) to route `cdx-*` via the currently installed Python package rather than a potentially stale copied script.

### v0.3.6 streaming provenance

Full-thread streaming now records the title source in its Markdown frontmatter and manifest. An end-of-document summary contains the source event count, rendered/ignored/unknown counts, truncation-marker occurrences, decoded byte count, and a SHA-256 of **decoded JSONL** (not compressed `.zst` file bytes). Explicit `response_item/additional_tools` tool-catalog metadata is a recognized non-rendered event, not an unknown message. The original JSONL remains the preservation-quality record.
