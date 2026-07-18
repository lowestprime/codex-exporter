#!/usr/bin/env python3
"""Keep the standalone, package, and Codex-skill CLI copies identical."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Export-CodexThread.py"
TARGETS = (
    ROOT / "codex_exporter" / "cli.py",
    ROOT / "skills" / "codex-thread-export" / "scripts" / "Export-CodexThread.py",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail instead of rewriting drifted mirrors")
    args = parser.parse_args()

    payload = SOURCE.read_bytes()
    drifted = [target for target in TARGETS if not target.is_file() or target.read_bytes() != payload]
    if args.check:
        if drifted:
            for target in drifted:
                print(f"CLI mirror drift: {target.relative_to(ROOT)}")
            return 1
        print("CLI mirrors are byte-identical.")
        return 0

    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
