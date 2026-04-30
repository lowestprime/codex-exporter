# Contributing

Thanks for taking a look! This tool was hardened against many real-world
Codex JSONL shapes. The bar for behavioral changes to the **extraction core**
is higher than for additive features — please read the relevant section before
opening a PR.

## Quick start

```bash
git clone https://github.com/lowestprime/codex-exporter.git
cd codex-exporter
python -m py_compile Export-CodexThread.py
python tests/test_smoke.py
```

No third-party Python dependencies are required. `tiktoken` is used opportunistically
if installed.

## What's safe to change

**Additive features** (new flags, new modes, new output fields, new helpers,
new docs, new tests) are welcome.

**Behavioral changes to the extraction core** (`classify_record`,
`extract_command`, `extract_output_and_meta`, `parse_apply_patch`, the
`SECRET_PATTERNS`, `strip_hogs`, `merge_command_outputs`,
`set_windows_clipboard_unicode`) need a real failing case attached, plus a
test fixture that exercises the regression. The core was iterated against
many real session shapes, so changes there can quietly regress old fixes for
mojibake, transport-header noise, base64 hogs, and Unicode clipboard fidelity.

Please run `cdx-verify-latest` from the PowerShell helper against a real
local export before/after your change. The expected healthy values are listed
in the README.

## Sync the skill copy

The Codex skill ships with a bundled copy of `Export-CodexThread.py` at
`skills/codex-thread-export/scripts/Export-CodexThread.py` so the skill folder
is self-contained for direct copy installs. After every change to the root
script, run:

```bash
python tools/sync_skill_script.py
```

CI verifies the two are byte-identical via `python tools/sync_skill_script.py --check`.

## Tests

Smoke tests live in `tests/test_smoke.py` and exercise the synthetic fixture at
`tests/fixtures/sample-session.jsonl`. They invoke the script as a subprocess
so they cover the same surface a Codex skill or shell user would invoke. To
add coverage, append to the fixture and write a new `test_*` function.

## Don't commit secrets

`codex-thread-exports/` is gitignored. Don't commit raw `.codex` session JSONL
files or unredacted exports. Use `--redact` for anything you intend to share.
