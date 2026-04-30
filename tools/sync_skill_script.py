#!/usr/bin/env python3
"""Keep ``skills/codex-thread-export/scripts/Export-CodexThread.py`` in sync with
the canonical root ``Export-CodexThread.py``.

Run ``python tools/sync_skill_script.py`` to copy. Run with ``--check`` to assert
they are byte-identical (used by CI).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "Export-CodexThread.py"
SKILL = REPO / "skills" / "codex-thread-export" / "scripts" / "Export-CodexThread.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit non-zero if files differ")
    args = ap.parse_args()

    if not ROOT.exists():
        print(f"missing: {ROOT}", file=sys.stderr)
        return 2

    if args.check:
        if not SKILL.exists():
            print(f"missing: {SKILL}", file=sys.stderr)
            return 1
        if ROOT.read_bytes() != SKILL.read_bytes():
            print("FAIL: skill-bundled script is out of sync with root.", file=sys.stderr)
            print(f"  root:  {ROOT}", file=sys.stderr)
            print(f"  skill: {SKILL}", file=sys.stderr)
            print("Run: python tools/sync_skill_script.py", file=sys.stderr)
            return 1
        print("OK: skill-bundled script matches root.")
        return 0

    SKILL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT, SKILL)
    print(f"synced: {ROOT} -> {SKILL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
