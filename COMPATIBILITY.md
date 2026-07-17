# Compatibility matrix

| Surface | Tested/targeted | Notes |
|---|---|---|
| Python | 3.10–3.13 | Standard library only; `tiktoken` optional |
| Windows | Windows 11, PowerShell 7+ | Native dialogs use `tkinter`; clipboard uses verified `CF_UNICODETEXT`; CLI stdout/stderr are normalized to UTF-8 |
| macOS/Linux | Python 3.10+ | Clipboard requires `pbcopy`, `wl-copy`, `xclip`, or `xsel` |
| Codex storage | `sessions/`, `archived_sessions/` | Direct `--jsonl` also supported |
| Session schemas | synthetic legacy/current fixtures plus anonymized schema corpus | Unknown schemas are reported, not silently erased |
| Compaction/rollback | `compacted.replacement_history`, `thread_rolled_back` | Reconstruction is explicitly approximate |
| Tokenization | regex fallback; optional `tiktoken` encoding | Exact only for selected encoding and output text |

Update this table for each tagged release with the newest Codex app/CLI versions exercised by CI or manual regression runs.
