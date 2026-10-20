# Changelog

## 0.3.0 (proposed)

- Reconcile SQLite threads against active and archived rollouts; normalize Windows extended paths.
- Export all available local rollouts for a named project in one command.
- Add efficient streaming full-thread export with progress, provenance, and schema audit.
- Convert user-supplied official ChatGPT account export JSON/ZIP, including branches.
- Bundle new components in the Windows standalone and Codex skill distributions.

## 0.2.0

- Persist and natively select export directories and Save As filenames.
- Add mode-aware filenames, templates, collision policies, and batch-safe short IDs.
- Add missing PowerShell helpers including `cdx-sessions`, `cdx-last`, `cdx-live`, `cdx-batch`, `cdx-browse`, `cdx-set-dir`, and `cdx-tokenizer`.
- Add last-N-turn, multi-session batch, project-aware browser, event reports, live-context reconstruction, and reasoning-summary opt-in.
- Repair narrowly recoverable invalid JSON backslashes and distinguish repaired from unrecovered lines.
- Replace misleading approximate-token metadata with method/encoding-specific fields.
- Add export manifests, package metadata, console entry point, expanded tests, compatibility matrix, and release workflow.
- Force CLI standard output and error to UTF-8 so browser and JSON output remain deterministic through Windows legacy-code-page pipes.
