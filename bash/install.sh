#!/usr/bin/env bash
# Install Codex Exporter 0.2+ on macOS/Linux.
set -euo pipefail

install_dir=${CODEX_EXPORT_INSTALL_DIR:-"$HOME/.codex-tools"}
profile_file=''
install_skill=0
install_package=0
install_tiktoken=0
no_profile=0
force=0

usage() {
    cat <<'USAGE'
Usage: ./bash/install.sh [options]
  --install-dir PATH   Standalone installation directory (default ~/.codex-tools)
  --profile PATH       Shell profile to update
  --skill              Install the self-contained Codex skill
  --package            Install the codex-export console entry point
  --tiktoken           Install/upgrade optional exact tokenizer support
  --no-profile         Do not update a shell profile
  --force              Replace an existing skill installation
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir) install_dir=$2; shift 2 ;;
        --profile) profile_file=$2; shift 2 ;;
        --skill) install_skill=1; shift ;;
        --package) install_package=1; shift ;;
        --tiktoken) install_tiktoken=1; shift ;;
        --no-profile) no_profile=1; shift ;;
        --force) force=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

repo_root=$(cd "$(dirname "$0")/.." && pwd)
python=${CODEX_EXPORT_PYTHON:-}
if [ -z "$python" ]; then
    if command -v python3 >/dev/null 2>&1; then python=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then python=$(command -v python)
    else echo 'Python 3.10+ is required.' >&2; exit 1
    fi
fi

mkdir -p "$install_dir"
cp "$repo_root/Export-CodexThread.py" "$install_dir/Export-CodexThread.py"
cp "$repo_root/bash/codex-thread-export.sh" "$install_dir/codex-thread-export.sh"
"$python" -m py_compile "$install_dir/Export-CodexThread.py"

if [ "$install_package" -eq 1 ]; then "$python" -m pip install --upgrade "$repo_root"; fi
if [ "$install_tiktoken" -eq 1 ]; then "$python" -m pip install --upgrade --only-binary=:all: 'tiktoken>=0.12,<1'; fi

if [ "$no_profile" -eq 0 ]; then
    if [ -z "$profile_file" ]; then
        case ${SHELL:-} in
            */zsh) profile_file=${ZDOTDIR:-$HOME}/.zshrc ;;
            *) profile_file=$HOME/.bashrc ;;
        esac
    fi
    mkdir -p "$(dirname "$profile_file")"
    touch "$profile_file"
    begin='# >>> Codex Thread Exporter helpers >>>'
    end='# <<< Codex Thread Exporter helpers <<<'
    source_line=". \"$install_dir/codex-thread-export.sh\""
    tmp=$(mktemp)
    awk -v begin="$begin" -v end="$end" '
        $0 == begin { skip=1; next }
        skip && $0 == end { skip=0; next }
        !skip { print }
    ' "$profile_file" > "$tmp"
    printf '\n%s\n%s\n%s\n' "$begin" "$source_line" "$end" >> "$tmp"
    if ! cmp -s "$profile_file" "$tmp"; then
        cp "$profile_file" "$profile_file.codex-exporter-backup-$(date +%Y%m%d-%H%M%S)"
        mv "$tmp" "$profile_file"
    else
        rm -f "$tmp"
    fi
fi

if [ "$install_skill" -eq 1 ]; then
    target=$HOME/.agents/skills/codex-thread-export
    if [ -e "$target" ] && [ "$force" -ne 1 ]; then
        echo "Skill already exists: $target (use --force to replace)" >&2
        exit 1
    fi
    rm -rf "$target"
    mkdir -p "$(dirname "$target")"
    cp -R "$repo_root/skills/codex-thread-export" "$target"
fi

printf 'Installed standalone exporter: %s\n' "$install_dir/Export-CodexThread.py"
"$python" "$install_dir/Export-CodexThread.py" --version
