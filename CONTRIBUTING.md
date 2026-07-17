# Contributing

Codex session schemas evolve, and small parser changes can silently alter large
exports. Additive features are welcome, but extraction changes must include a
minimal anonymized fixture and a regression test.

## Development setup

```bash
git clone https://github.com/lowestprime/codex-exporter.git
cd codex-exporter
python -m pip install -e '.[test,exact]'
python tools/sync_cli.py
python -m pytest
python -m build
```

Python 3.10 through 3.13 are supported. `tiktoken` is optional at runtime and
installed by the `exact` extra for tests.

## Canonical and mirrored CLI files

`Export-CodexThread.py` is the canonical standalone implementation. It is
mirrored byte-for-byte to:

- `codex_exporter/cli.py`, used by the `codex-export` console entry point;
- `skills/codex-thread-export/scripts/Export-CodexThread.py`, used by the
  self-contained Codex skill.

After every canonical CLI change, run:

```bash
python tools/sync_cli.py
```

CI rejects drift among the three copies.

## Parser and renderer changes

Changes to event classification, text extraction, command/output merging,
patch reconstruction, JSON repair, redaction, source-truncation handling,
clipboard code, or live-context reconstruction must include:

1. a minimal anonymized fixture under `tests/fixtures/regression/`;
2. a test demonstrating the prior failure and expected behavior;
3. manifest assertions when the change affects integrity/accounting fields;
4. an entry in `CHANGELOG.md` when user-visible behavior changes.

Do not add raw private rollout files. Preserve only the minimum schema shape
needed to reproduce a bug.

## Public compatibility claims

Update `COMPATIBILITY.md` when a tagged release is manually exercised against a
new Codex app/CLI version or storage source. Do not infer exact compatibility
from a synthetic fixture alone.

## PowerShell and installer changes

Windows changes must keep these guarantees:

- custom installation paths;
- explicit Python-interpreter pinning;
- exactly one marked profile block after repeated installs;
- preservation of unrelated profile content;
- no user-specific paths in committed files;
- no profile backup unless the generated content changes.

The Windows CI job parses all committed `.ps1` files and runs the idempotent
custom-path installation test.

## Security and privacy

Never commit raw `.codex` sessions, unredacted exports, personal PowerShell
profiles, API keys, or machine-specific audit output. `--redact` is a helpful
filter, not a guarantee; inspect any export before publishing it.
