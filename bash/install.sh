#!/usr/bin/env bash
# install.sh - Install the Codex Thread Exporter on macOS/Linux.
# Usage:
#   ./bash/install.sh              # install script + helper functions
#   ./bash/install.sh --skill      # also install the Codex skill at ~/.agents/skills/
#   ./bash/install.sh --no-profile # skip appending helpers to ~/.bashrc / ~/.zshrc
set -euo pipefail

skill=0
no_profile=0
for arg in "$@"; do
    case "$arg" in
        --skill) skill=1 ;;
        --no-profile) no_profile=1 ;;
        -h|--help)
            grep -E '^#( |!)' "$0" | sed 's/^#//; s/^ //'
            exit 0 ;;
        *)
            echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

repo_root=$(cd "$(dirname "$0")/.." && pwd)
root_script="$repo_root/Export-CodexThread.py"
helper="$repo_root/bash/codex-thread-export.sh"

[ -f "$root_script" ] || { echo "missing: $root_script" >&2; exit 1; }
[ -f "$helper" ]      || { echo "missing: $helper" >&2; exit 1; }

install_dir="${HOME}/.codex-tools"
mkdir -p "$install_dir" "${HOME}/codex-thread-exports"
cp "$root_script" "$install_dir/Export-CodexThread.py"
cp "$helper"      "$install_dir/codex-thread-export.sh"

if command -v python3 >/dev/null 2>&1; then
    python3 -m py_compile "$install_dir/Export-CodexThread.py"
elif command -v python  >/dev/null 2>&1; then
    python  -m py_compile "$install_dir/Export-CodexThread.py"
else
    echo "warning: python3 not found in PATH; skipping py_compile check" >&2
fi

if [ "$no_profile" -eq 0 ]; then
    marker='# >>> Codex Thread Exporter helpers >>>'
    closer='# <<< Codex Thread Exporter helpers <<<'
    block="\n${marker}\n. \"\$HOME/.codex-tools/codex-thread-export.sh\"\n${closer}\n"
    case "${SHELL:-}" in
        */zsh)  rcfile="${ZDOTDIR:-$HOME}/.zshrc" ;;
        *)      rcfile="$HOME/.bashrc" ;;
    esac
    touch "$rcfile"
    if ! grep -F -q "$marker" "$rcfile"; then
        printf "%b" "$block" >> "$rcfile"
        echo "Appended helpers to $rcfile. Reload your shell or run: source \"$rcfile\""
    fi
fi

if [ "$skill" -eq 1 ]; then
    skill_target="${HOME}/.agents/skills/codex-thread-export"
    mkdir -p "$(dirname "$skill_target")"
    rm -rf "$skill_target"
    cp -R "$repo_root/skills/codex-thread-export" "$skill_target"
    mkdir -p "$skill_target/scripts"
    cp "$root_script" "$skill_target/scripts/Export-CodexThread.py"
    echo "Installed skill at $skill_target"
fi

echo "Installed exporter at $install_dir/Export-CodexThread.py"
"$install_dir/Export-CodexThread.py" --version 2>/dev/null || python3 "$install_dir/Export-CodexThread.py" --version || true
