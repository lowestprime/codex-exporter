# Compatibility matrix

| Surface | Tested or targeted | Notes |
|---|---|---|
| Python | 3.10–3.13 | Base package uses the standard library; `tiktoken`, `zstandard`, and `ijson` are optional for their respective features |
| Windows | Windows 11, PowerShell 7+ | Native dialogs use `tkinter`; clipboard uses verified `CF_UNICODETEXT`; CLI stdout/stderr use UTF-8 |
| macOS/Linux | Python 3.10+ | Clipboard requires `pbcopy`, `wl-copy`, `xclip`, or `xsel` |
| Codex storage | `sessions/`, `archived_sessions/`, direct `--jsonl` | Plain JSONL and `.jsonl.zst`; compressed inputs require `zstandard` |
| Session schemas | Synthetic legacy/current fixtures and schema regressions | Unknown schemas are reported; rendered Markdown is not native JSONL serialization |
| Compaction/rollback | `compacted.replacement_history`, `thread_rolled_back` | Requested reconstruction is explicitly approximate |
| Tokenization | Regex fallback or optional `tiktoken` | Exact only for the selected encoding and output text; review part sizing uses UTF-8 bytes |
| Full archival ZIP | Deflate level 6 by default in new configurations | Built-in Python backend, or compatible 7-Zip CLI; saved LZMA preferences remain until changed |
| Review ZIP and files | Deflate method 8 plus retained UTF-8 TXT files | Standard-library creation and independent CRC/SHA-256 checks; direct TXT upload is available when ZIP ingestion is unavailable |
| Legacy review conversion | Flat, unencrypted export ZIP, `.md`, or `.txt` | Python reads LZMA Markdown/manifest members; no external archiver or Codex session is needed; native JSONL is not opened |
| Windows ChatGPT/Codex 26.1013.40934 | Earlier user-reported target, not a current end-to-end certification | Local catalog/rollouts and cloud account exports remain separate sources |
| Large full-thread streaming | Synthetic regression coverage | One JSONL event in memory at a time; large individual records can still consume memory |
| Official ChatGPT account export ZIP/JSON | Synthetic fixture coverage | Recorded mapping branches and numbered shards; `ijson` optional/required beyond 64 MiB |

## 0.3.10 validation scope

The supported CI matrix targets Windows, macOS, and Linux with CPython 3.10 and
3.13, plus distribution and mirrored-script validation. The corresponding
Actions runs establish the result for a specific release.

Review regressions independently reconstruct original Markdown bytes, validate
Unicode/CRLF boundaries, check whole-file and body hashes, enforce size limits,
read every Deflate member, and test legacy LZMA conversion without opening the
native JSONL. They also cover invalid UTF-8, ambiguous/unsafe ZIP inventories,
encryption and symlink flags, and output collisions. The live snapshot suite
continues to exercise both renderers while the original source changes.

With `backend=auto`, a configured archiver path selects the external backend;
without one, Deflate uses Python. `backend=python` forces built-in Deflate while
preserving that saved path. LZMA requires `auto` or `7zip` and a compatible external
CLI. LZMA dictionary/word-size settings do not apply to Deflate, and the built-in
writer uses one compression worker rather than the configured 7-Zip thread count.
Review ZIPs always use built-in Deflate level 6.

Full-archive publication supports a verified copy fallback when hard links are
unavailable. Snapshot reuse also falls back to copying; app-owned inputs are never
hard-linked as exported native copies. Existing archives are not overwritten.

A portable file format cannot ensure that a chat environment exposes an uploaded
file to its tools or fits the entire history in context. TXT parts and the recent
excerpt provide a direct-upload fallback. Their byte bounds are not exact token
counts, and the review process does not claim a semantic audit of unread content.
Consult the receiving service's current upload limits.

## Historical 0.3.9 validation

- Local Linux / CPython 3.12.14: 102 tests passed, with one optional OS clipboard test skipped.
- Real archiver: official 7-Zip 26.05 Linux x64; a 701 MiB synthetic growing rollout passed archive/source-hash and cleanup checks using the then-default ZIP/LZMA settings.
- That synthetic archive run reached approximately 5.29 GiB of child-process peak RSS, including the archiver. It is not a memory or performance prediction for other inputs or the new Deflate defaults.

The live source contract remains a verified append-only prefix, not an atomic
filesystem snapshot against arbitrary in-place writers. Compressed inputs require
complete Zstandard frames, including checksums where present.
