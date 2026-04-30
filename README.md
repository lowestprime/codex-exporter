# Codex Thread Exporter

[![ci](https://github.com/lowestprime/codex-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/lowestprime/codex-exporter/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

GUI-fidelity Markdown exports of local **Codex app/CLI** thread/session JSONL — full threads, turns, single responses, ranges — with frontmatter, an export map, per-turn metadata, redaction, **verified** Win32 Unicode clipboard, and a one-command Codex skill for `$`-invocation in the Codex composer.

**Why this exists.** Copying responses from Codex into another tool drops Unicode (`I'll` → `IÆll`), bloats your output with `Chunk ID` / `Wall time` / `Process exited` transport headers, hides edited-file cards behind raw patch noise, and never gives you a clean full-thread Markdown with usable metadata. This tool fixes all of that, and stays dependency-light: zero required Python packages.

---

## Quickstart

### macOS / Linux

```bash
git clone https://github.com/lowestprime/codex-exporter.git
cd codex-exporter
./bash/install.sh --skill           # installs script + helpers + Codex skill
exec $SHELL                         # reload shell to pick up cdx-* helpers
cdx-sessions                        # list local sessions
cdx-thread <SESSION_ID>             # full export to ~/codex-thread-exports/
```

### Windows (PowerShell 7+)

```powershell
git clone https://github.com/lowestprime/codex-exporter.git
cd codex-exporter
.\Install-CodexThreadExporter.ps1 -AppendProfile -InstallSkill
. $PROFILE
cdx-thread <SESSION_ID>
```

### Direct Python (any platform)

```bash
python Export-CodexThread.py --list-sessions
python Export-CodexThread.py --latest-session --mode thread
python Export-CodexThread.py --session-id <UUID> --mode last-response --wrap-md --clipboard
```

---

## Get the session ID

In a Codex app/CLI thread composer, type `/status`. Copy the `Session` UUID. Or skip this step entirely and use `--list-sessions` / `--latest-session`.

---

## What it exports

For `--mode thread`, the Markdown contains:

- YAML frontmatter (title, session id, source SHA256, models detected, prompt/response counts, action counts, edited/created/deleted file counts, total lines, approximate tokens, exported timestamp)
- Top-of-file thread export map with a per-turn index table
- Per-turn prompt and response metadata (timestamps, line counts, token counts, action counts, file-card counts)
- Assistant messages, command blocks, command output blocks, `Success` markers
- Reconstructed `Edited file` / `Created file` / `Deleted file` cards parsed from `*** Begin Patch` ... `*** End Patch` payloads — **not** raw `apply_patch` noise
- ANSI/OSC stripping, base64/data-URI/hex blob summarization, repeated-line compaction
- Optional secret redaction (`--redact`)

Filenames are stamped:

```text
codex_<thread_name>_WED_04292026_060940_PM-PDT_12345lines_67890tokens.md
```

The timezone follows your system local tz, or `CODEX_EXPORT_TZ=America/Los_Angeles` if set.

---

## Modes

| Mode | What it does |
|---|---|
| `thread` | Full thread/chat with frontmatter, map, prompts, responses, actions |
| `last-response` | Latest response after the most recent user prompt |
| `last-substantial` | Latest assistant message ≥ `--min-chars` (default 1000) |
| `response` | Selected response block by `--response N` |
| `turn` | Selected prompt+response block by `--response N`, with metadata |
| `message` | Selected user/assistant message by `--message N` |
| `range` | Message range by `--from-message N --to-message M` |
| `chat` | User/assistant messages only, no actions |
| `chat-actions` | Everything: messages + actions |
| `actions` | Extracted actions only |

Use `--list-responses` to see indices for `response` / `turn` mode, or `--list` for indices for `message` mode.

## Common commands

### PowerShell (after `.\Install-CodexThreadExporter.ps1 -AppendProfile`)

| Command | What it does |
|---|---|
| `cdx-sessions` | List all local Codex session JSONLs |
| `cdx-thread <UUID>` | Full thread export |
| `cdx-thread-clip <UUID>` | Full thread export + clipboard copy |
| `cdx-response <UUID>` | Latest response → wrapped Markdown + verified clipboard |
| `cdx-response <UUID> -NoFile` | Clipboard only, skip writing a file |
| `cdx-responses <UUID>` | List prompt/response blocks |
| `cdx-response-block <UUID> N` | Export response block N |
| `cdx-turn <UUID> N` | Export prompt+response turn N with metadata |
| `cdx-final <UUID>` | Last substantial assistant answer |
| `cdx-msg <UUID> N` | Export single message N |
| `cdx-range <UUID> A B` | Export message range A..B |
| `cdx-open` | Open the export folder |
| `cdx-verify-latest` | Health-check the most recent export (clipboard match, no mojibake, no transport noise) |

### Bash / zsh (after `./bash/install.sh`)

Same names, same args. `cdx-sessions`, `cdx-thread`, `cdx-thread-clip`, `cdx-response`, `cdx-responses`, `cdx-response-block`, `cdx-turn`, `cdx-final`, `cdx-msg`, `cdx-range`, `cdx-open`, `cdx-latest`.

---

## Codex app integration (skill)

A first-class [Codex Agent Skill](https://developers.openai.com/codex/skills) is included so you can invoke the exporter from the Codex composer:

```text
$codex-thread-export export this full thread
$codex-thread-export copy the latest response as fenced Markdown
$codex-thread-export list local sessions
```

The skill is self-contained — its bundled `scripts/Export-CodexThread.py` mirrors the canonical root script, so the skill folder works on its own.

### Install the skill

PowerShell:
```powershell
.\Install-CodexThreadExporter.ps1 -InstallSkill
```

Bash:
```bash
./bash/install.sh --skill
```

Or copy manually:
```bash
cp -R skills/codex-thread-export "$HOME/.agents/skills/codex-thread-export"
```

Restart Codex if the skill doesn't appear in `/skills`.

---

## Verifying a clean export (Windows)

After running an export, `cdx-verify-latest` should report:

```text
Clipboard_MatchesFile         True
File_Has_Mojibake             False
Clipboard_Has_Mojibake        False
File_Has_ChunkMetadata        False
Clipboard_Has_ChunkMetadata   False
File_Has_ActionNoise          False
File_Has_EditedFile           True
Clipboard_Has_EditedFile      True
```

These are the regression boundaries the tool was iterated against. If any of these flip, file an issue with the failing fixture.

---

## Security and publishing guidance

Codex transcripts can contain prompts, command output, file paths, branch names, `.env` filenames, API keys, and private repo names. **Always inspect before sharing.**

For public sharing:

```bash
python Export-CodexThread.py --session-id <UUID> --mode thread --redact
```

`--redact` scrubs:
- OpenAI API keys (`sk-...`)
- GitHub PATs and tokens (`github_pat_...`, `ghp_...`, etc.)
- Bearer tokens
- Generic `password=` / `token=` / `secret=` / `api_key=` lines
- Cloudflare tunnel tokens

The exporter strips embedded base64 images, data URIs, and oversize hex blobs by default. Pass `--keep-hogs` to disable (forensic local-only).

Add this to `.gitignore` in projects that use the tool:

```text
codex-thread-exports/
*.raw.jsonl
*.private.*.md
```

---

## Environment variables

| Variable | Purpose |
|---|---|
| `CODEX_THREAD_EXPORTER` | Override the path the bash/zsh and skill resolvers use to find the script |
| `CODEX_THREAD_EXPORT_DIR` | Override the default `--out-dir` |
| `CODEX_EXPORT_TZ` | IANA timezone (e.g. `America/Los_Angeles`) used in filename stamps |
| `CODEX_HOME` | Override `~/.codex` for session discovery |

---

## Limitations

- The exporter reads local JSONL session files; it does not call any API or upload anything.
- Token counts are approximate. Install `tiktoken` for `cl100k_base` accuracy; otherwise a dependency-free regex estimate is used.
- Model detection is best-effort and depends on whether the local session JSONL records the model name.
- Verified Win32 Unicode clipboard is Windows-only. Other platforms use `pbcopy` / `wl-copy` / `xclip` / `xsel`.

---

## Repository layout

```text
Export-CodexThread.py                 canonical Python script
Install-CodexThreadExporter.ps1       Windows installer
bash/
  codex-thread-export.sh              POSIX helper functions
  install.sh                          POSIX installer
powershell/
  CodexThreadExport-profile-block.v7.ps1   profile block to source
skills/
  codex-thread-export/                Codex Agent Skill
    SKILL.md
    scripts/Export-CodexThread.py     bundled copy (kept in sync via tools/sync_skill_script.py)
    agents/openai.yaml
tests/
  test_smoke.py                       7 cross-platform smoke tests
  fixtures/sample-session.jsonl       synthetic Codex session for tests
tools/
  sync_skill_script.py                keeps skill copy in lockstep with root
.github/workflows/ci.yml              CI on Linux/macOS/Windows × py3.10/3.13
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The bar for behavioral changes to the extraction core is high — please attach a real failing case before changing `classify_record`, `extract_command`, `parse_apply_patch`, the secret patterns, `strip_hogs`, or the Win32 clipboard implementation.

## License

[MIT](LICENSE) © 2026 Cooper Beaman
