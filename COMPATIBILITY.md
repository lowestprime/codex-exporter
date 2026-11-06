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

| Windows ChatGPT/Codex 26.1013.40934 | User-reported target, not end-to-end validated | Local Codex `state_5.sqlite` catalog and JSONL; cloud ChatGPT/Work requires separately supplied eligible data export |
| Large full-thread streaming | Synthetic regression verified | One JSONL event in memory at a time; runtime and peak RSS vary by event size |
| Official ChatGPT data export ZIP/JSON | Synthetic fixture | Supports recorded conversation mapping branches; `ijson` optional/required beyond 64 MiB |


## 0.3.9 validation scope

- Local: Linux, CPython 3.12.14, 102 tests passed; one optional OS clipboard test skipped.
- CI matrix: Windows/macOS/Linux, CPython 3.10 and 3.13, plus wheel/sdist validation. Consult the corresponding Actions run for its status.
- Real archiver: official 7-Zip 26.05 Linux x64, default ZIP/LZMA settings, 701 MiB synthetic growing rollout; archive/source hashes and cleanup verified.
- Windows source-copy reuse: NTFS hard links are used when supported; a verified copy fallback handles unsupported or cross-volume links. Windows behavior is also covered by the CI matrix.
- Original input: append-only plain JSONL, or complete Zstandard frames (including concatenated frames). The captured prefix is verified, not an atomic OS snapshot.
- Memory: capture and hashing are chunked; streaming rendering retains individual records. Large individual records and the archiver dictionary/thread settings can still consume substantial RAM. The 701 MiB archive test reached approximately 5.29 GiB of child-process peak RSS, including the configured archive operation; this is not a bound on all inputs.
