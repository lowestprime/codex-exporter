#!/usr/bin/env python3
"""Keep standalone/package CLIs and every bundled skill module identical."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Export-CodexThread.py"
TARGETS = (
    ROOT / "codex_exporter" / "cli.py",
    ROOT / "skills" / "codex-thread-export" / "scripts" / "Export-CodexThread.py",
    ROOT / "skills" / "codex-thread-export" / "scripts" / "codex_exporter" / "cli.py",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail instead of rewriting drifted mirrors")
    args = parser.parse_args()

    copies = [(SOURCE, target) for target in TARGETS]
    skill_package = ROOT / "skills" / "codex-thread-export" / "scripts" / "codex_exporter"
    copies.extend((source, skill_package / source.name)
                  for source in sorted((ROOT / "codex_exporter").glob("*.py"))
                  if source.name != "cli.py")
    drifted = [(source, target) for source, target in copies
               if not target.is_file() or target.read_bytes() != source.read_bytes()]
    if args.check:
        if drifted:
            for _, target in drifted:
                print(f"Exporter mirror drift: {target.relative_to(ROOT)}")
            return 1
        print("CLI and bundled module mirrors are byte-identical.")
        return 0

    for source, target in drifted:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
