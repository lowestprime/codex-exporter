# Security policy

## Reporting a vulnerability

Please report vulnerabilities through GitHub's private security-advisory flow
for this repository rather than a public issue. Include a minimal reproducer and
avoid attaching a real Codex session unless it has been fully anonymized.

## Sensitive local data

The exporter reads local rollout JSONL and may encounter source code, local
paths, shell history, environment-variable names, credentials, or private
repository metadata. It does not upload session data, but generated Markdown and
manifest files inherit the sensitivity of their source.

Before sharing an export:

1. use `--redact --no-source-copy` so raw native JSONL is not included;
2. inspect both Markdown and sibling manifest;
3. remove private paths, names, and repository identifiers not covered by
   pattern-based redaction;
4. do not publish raw rollout JSONL.

The project intentionally excludes personal profiles, audit exports, generated
manifests, and private rollout files through `.gitignore`.

## v0.3 local catalogs and account-export archives

`--audit-sessions` reads state SQLite metadata read-only. `--unknown-events sidecar`
retains raw source events, which may include private content and are not safe for
public release. The supplied `--chatgpt-export` file can contain personal messages,
Work information, links, and file metadata; keep exported Markdown and manifests
private. Never commit your exported conversations, source JSONL, SQLite databases,
account ZIP archives, or authentication material.

## Windows staging and published-file access (0.3.11)

Private temporary directories retain their restrictive ACL. Published files on
Windows are newly and exclusively created in the chosen destination, then filled
and SHA-256/readback verified. This uses ordinary parent inheritance rather than
carrying a private staging security descriptor through a hard link or rename.
The original rollout and parent ACL are never changed. No Everyone grant or
ownership change is used, and POSIX publication retains its existing umask and
hard-link behavior.

`--diagnose-access` reads actual access/owner/DACL state. `--repair-access` is an
explicit operation on recognized `codex_` exports or a validated flat review
directory. It rejects app-owned `.codex` paths, wildcards, broad directories,
links/reparse points, multiply linked files, and unexpected review contents.
The helper uses the Windows system `icacls` executable with an exact filename,
`/reset /L /Q`, and no recursive switch, grants, ownership override, or elevation.
It reports failures and before/after security data; it does not bypass a lack of
WRITE_DAC. A selected review directory and its validated children are the entire
repair scope. The existing output parent must already grant intended access.

Diagnostics include local paths and owner SIDs. Treat a diagnostic report as
local system information and remove those identifiers before sharing it publicly.
The [upstream CPython issue](https://github.com/python/cpython/issues/157876) and
[Microsoft's inheritance documentation](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)
explain the relevant mechanism, but do not establish the ACL of an uninspected
user file. The repair does not diagnose arbitrary corruption, malware, encrypted
files, or application-specific access restrictions.

## Output/source separation

Generated output paths are checked against every protected input and against one
another before writing. The checks resolve path aliases and compare existing
filesystem identities, including hard links. An explicit manifest cannot replace
the live rollout, overwrite rendered Markdown, or collide with another generated
artifact. The packaging stage also checks core path separation. These are
read-only preflight checks, not a claim of atomicity against concurrent hostile
path replacement.
