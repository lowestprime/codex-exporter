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

1. use `--redact`;
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
