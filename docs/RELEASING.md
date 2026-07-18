# Release workflow

The repository uses semantic version tags and a GitHub Actions release workflow.
The workflow builds both sdist and wheel, validates their metadata, installs the
wheel, smoke-tests `codex-export`, uploads the distributions as a workflow
artifact, and attaches them to a GitHub release.

## Prepare a release

1. Update `__version__` in the canonical `Export-CodexThread.py`.
2. Update `[project].version` in `pyproject.toml`.
3. Run `python tools/sync_cli.py`.
4. Move relevant entries from `Unreleased` into a dated changelog section.
5. Update `COMPATIBILITY.md` with actually tested environments.
6. Run:

```bash
python -m pip install -e '.[test,exact]'
python tools/sync_cli.py
python -m pytest
python -m build
python -m twine check dist/*
```

## Tag and publish

After the release PR is merged and `main` is current:

```bash
git switch main
git pull --ff-only origin main
git tag -s v0.2.0 -m 'Codex Exporter 0.2.0'
git push origin v0.2.0
```

An unsigned annotated tag is acceptable when signing is not configured:

```bash
git tag -a v0.2.0 -m 'Codex Exporter 0.2.0'
git push origin v0.2.0
```

Do not move or recreate a published version tag. Correct a release with a new
patch version.

The workflow creates a GitHub release only. Publishing to PyPI is intentionally
not enabled until project ownership, trusted publishing, and package-name policy
are explicitly configured.
