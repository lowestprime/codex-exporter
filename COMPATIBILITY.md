# Compatibility matrix

| Surface | Tested or targeted | Notes |
|---|---|---|
| Python | 3.10–3.13 | Standard-library base; optional tiktoken, zstandard, and ijson for their respective features |
| Windows | Windows 11, PowerShell 7+ target; Windows CI | UTF-8 CLI streams, native dialogs/clipboard; fresh destination publication inherits parent permissions |
| macOS/Linux | Python 3.10+ | Platform clipboard helper required for clipboard use |
| Full archival ZIP | LZMA level 9, 256m, word size 64, 19 requested threads | Compatible external 7-Zip CLI; requested worker budget is not measured worker utilization |
| Default output | One verified ZIP, three original files | Markdown, manifest, captured JSONL; successful cleanup removes loose originals |
| Embedded review | Explicit --review | Review text/index/guide inside the same archive; default disabled |
| Standalone review conversion | Explicit --review-from | Deflate review ZIP plus retained text directory; --no-archive selects text only |
| ZIP diagnostics | Python zipfile with the relevant codec present | Metadata-only or full CRC/size/SHA; actual LZMA property inspection; no extraction or execution |
| Remote ChatGPT upload/analysis | Must be checked in the receiving conversation | Local decoder success does not establish attachment availability, remote codec configuration, or context capacity |
| Codex storage | sessions, archived_sessions, direct JSONL | Plain JSONL and optional .jsonl.zst decoding |
| Live snapshots | Fixed prefix with independent verification | Later appends deferred; incomplete tail recorded; detected replacement/rewrite/truncation fails |
| Full-thread rendering | Streaming or original buffered renderer | Large single records can still consume memory; rendered content is not native JSONL serialization |
| Token counts | Regex fallback or optional tiktoken | Exact only for the selected encoding/output; review part caps use bytes |
| Account-export conversion | ZIP/directory/JSON and numbered shards | Optional ijson for large shards; separate from local rollout access |

## Release validation

CI targets Windows, macOS and Linux with Python 3.10/3.13, plus packaging and
mirrored-script checks. Consult the Actions runs for the exact released commit.
The Windows jobs exercise ACL inheritance/repair and PowerShell wrappers.

ZIP diagnostics report the runtime where they actually execute. ZIP method 14
requires a reader with LZMA support; reducing its dictionary does not add that
codec to an unsupported reader. A missing attachment path is a pre-decode result.

The diagnostic's default 512 MiB dictionary guard is a policy threshold, not a
remote or local memory measurement. Python ZIP/LZMA retains dictionaries and
internal buffers beyond the requested 1 MiB reads. Sequential streaming avoids
explicit whole-member reads but does not promise a fixed total process footprint.

No automatic archive-format migration or compression fallback is performed.
Explicit saved Deflate preferences from 0.3.10 remain until the user changes them;
installing a new version does not erase configuration. LZMA needs auto/7zip and a
compatible external CLI; explicit Python-backend archives use Deflate.

See [archive diagnostics](docs/ARCHIVE-DIAGNOSTICS.md),
[integrity semantics](docs/INTEGRITY-AND-MANIFEST.md), and the
[Windows upgrade guide](docs/WINDOWS-UPGRADE.md).
