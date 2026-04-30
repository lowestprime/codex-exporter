# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-29

Initial public release. Hardened over multiple iterations against real Codex
app/CLI session JSONL files. Verified against `cdx-verify-latest`:
clipboard matches file, no mojibake, no `Chunk ID` transport metadata, no
`Action ` noise, edited-file cards intact.

### Added
- Full thread/chat export with YAML frontmatter, prompt/response index, per-turn
  metadata, and a top-of-file thread export map (`--mode thread`).
- Latest-response, selected-response, and selected-turn modes
  (`--mode last-response | response | turn | last-substantial`).
- Range and message-level selection (`--mode range | message`).
- Apply-patch reconstruction into `Edited file` / `Created file` /
  `Deleted file` cards directly from `*** Begin Patch` ... `*** End Patch`
  payloads.
- Verified Win32 `CF_UNICODETEXT` clipboard with read-back verification —
  fixes the `clip.exe` mojibake (`I’ll` -> `IÆll`) class of bugs.
- Cross-platform clipboard fallbacks (`pbcopy`, `wl-copy`, `xclip`, `xsel`).
- ANSI/OSC stripping, repeated-line compaction, base64/data-URI/hex blob
  summarization to avoid bloating exports with binary payloads.
- Secret pattern redaction for OpenAI keys, GitHub PATs, bearer tokens, and
  generic `password=`/`token=`/`api_key=` lines (`--redact`).
- `--list-sessions` enumerates all local Codex session JSONLs (id, size,
  mtime, title) — no session UUID required to start.
- `--latest-session` shorthand for the most recently modified session.
- `--json` machine-readable output line for skill/script chaining
  (file, lines, tokens, models, source SHA256).
- `--version` flag.
- `CODEX_EXPORT_TZ` environment variable to control filename timestamp tz.
- `CODEX_THREAD_EXPORT_DIR` environment variable for the default export dir.
- Codex skill at `skills/codex-thread-export/` matching the current Agent
  Skills schema (frontmatter + `agents/openai.yaml`), self-contained with a
  bundled copy of the script in `scripts/`.
- PowerShell helper module with `cdx-thread`, `cdx-response`, `cdx-turn`,
  `cdx-responses`, `cdx-msg`, `cdx-final`, `cdx-verify-latest`, etc.
- Bash/zsh helper module mirroring the same `cdx-*` API
  (`bash/codex-thread-export.sh`).
- Cross-platform install scripts (`Install-CodexThreadExporter.ps1`,
  `bash/install.sh`).
- Synthetic JSONL fixture and 7 smoke tests covering every export mode.
- GitHub Actions CI on Ubuntu / macOS / Windows × Python 3.10 + 3.13.

### Changed
- Filename timestamp now respects the system local timezone (or the
  `CODEX_EXPORT_TZ` env var) instead of being hardcoded to
  `America/Los_Angeles`.
- Long Windows tz names like `Pacific Daylight Time` are abbreviated to `PDT`
  in filenames.

### Deprecated
- `--fix-mojibake` is now a silent no-op (mojibake repair is on by default).
  Pass `--keep-mojibake` to disable.

[Unreleased]: https://github.com/lowestprime/codex-exporter/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lowestprime/codex-exporter/releases/tag/v0.1.0
