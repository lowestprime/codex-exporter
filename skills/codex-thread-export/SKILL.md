---
name: codex-thread-export
description: Use this skill when the user asks to export, save, or copy a Codex app/CLI thread, transcript, response, or turn to Markdown. Works for full-thread exports with frontmatter and an export map, latest-response copies, selected response/turn exports, transcript maps, YAML metadata, and Unicode-safe clipboard copying. Always works on local Codex JSONL session files; never uploads anything.
---

# Codex Thread Export skill

This skill drives a local Python exporter that converts Codex app/CLI session JSONL transcripts into clean, GUI-fidelity Markdown.

## Resolving the exporter script

Use the first one of these that resolves successfully:

1. The `CODEX_THREAD_EXPORTER` environment variable, if set.
2. The script bundled with this skill at `scripts/Export-CodexThread.py` (relative to this `SKILL.md`).
3. `C:\projects\CodexTools\Export-CodexThread.py` on Windows, or `~/.codex-tools/Export-CodexThread.py` elsewhere.

Throughout this skill, use `python` on Windows and `python3` on macOS/Linux when running the script.

## Workflow

1. **Find the session.** If the user did not provide a session UUID:
   - Either run `/status` in the Codex app composer and ask the user to paste the `Session` value, **or**
   - Run `python <exporter> --list-sessions` to enumerate all local sessions, **or**
   - Use `--latest-session` if the user means the most recent one.

2. **Pick the export mode** that matches the request:

   | User intent | Flag |
   |---|---|
   | Full thread/chat with frontmatter, map, all turns | `--mode thread` |
   | Latest/current uncollapsed response only | `--mode last-response` |
   | A specific response by number | `--mode response --response N` |
   | A specific prompt+response turn with metadata | `--mode turn --response N` |
   | A specific message by number | `--mode message --message N` |
   | A range of messages | `--mode range --from-message A --to-message B` |
   | Just user/assistant messages, no actions | `--mode chat` |

3. **Always include `--out-dir`** so the file lands in the user's export directory. The default is `$CODEX_THREAD_EXPORT_DIR` if set, otherwise `<exporter_dir>/codex-thread-exports`.

4. **For copying to clipboard** (a common request), add `--clipboard`. For wrapped Markdown blocks (so the result pastes cleanly into another chat), add `--wrap-md`.

5. **Before sharing publicly**, add `--redact` to scrub OpenAI/GitHub tokens, bearer tokens, and `password=...` style lines. The exporter strips embedded base64 images, data URIs, and oversize hex blobs by default.

6. **For machine-readable output** (when chaining in scripts), add `--json` to print a single JSON line with `file`, `lines`, `approx_tokens`, `models`, `source_sha256`.

## Canonical commands

```powershell
# List all local sessions and pick one
python "<exporter>" --list-sessions

# Full thread export
python "<exporter>" --session-id "<SESSION_ID>" --mode thread

# Latest response, wrapped + clipboard
python "<exporter>" --session-id "<SESSION_ID>" --mode last-response --wrap-md --clipboard

# A specific response block
python "<exporter>" --session-id "<SESSION_ID>" --list-responses
python "<exporter>" --session-id "<SESSION_ID>" --mode response --response N --wrap-md --clipboard

# Whole most recent session, no UUID needed
python "<exporter>" --latest-session --mode thread
```

## Constraints

- Never upload session files anywhere. The exporter is local-only and reads `~/.codex/sessions/**/*.jsonl`.
- Always offer `--redact` before any public sharing.
- Do not commit raw `.codex` session files or unredacted exports containing secrets to git.
- If `--clipboard` is requested on Windows, the exporter writes via the verified Win32 Unicode clipboard. On macOS it uses `pbcopy`, on Linux it tries `wl-copy`, `xclip`, then `xsel`.
