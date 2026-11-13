#!/usr/bin/env bash
# Codex Exporter bash/zsh helper functions (v0.4+).
# Source from ~/.bashrc, ~/.zshrc, or another shell profile.

_codex_export_resolve_script() {
    if [ -n "${CODEX_THREAD_EXPORTER:-}" ] && [ -f "$CODEX_THREAD_EXPORTER" ]; then
        printf '%s\n' "$CODEX_THREAD_EXPORTER"
        return 0
    fi
    local candidate
    for candidate in \
        "${CODEX_EXPORT_FALLBACK_SCRIPT:-}" \
        "$HOME/.codex-tools/Export-CodexThread.py" \
        "$HOME/.agents/skills/codex-thread-export/scripts/Export-CodexThread.py"
    do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

_codex_export_python() {
    if [ -n "${CODEX_EXPORT_PYTHON:-}" ]; then
        if [ ! -x "$CODEX_EXPORT_PYTHON" ]; then return 1; fi
        printf '%s\n' "$CODEX_EXPORT_PYTHON"
    elif command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif command -v python >/dev/null 2>&1; then
        command -v python
    else
        return 1
    fi
}

_codex_export_run() {
    local script python route
    python=$(_codex_export_python) || {
        printf '%s\n' 'codex-export: Python not found; set CODEX_EXPORT_PYTHON to its executable' >&2
        return 2
    }
    if [ -n "${CODEX_THREAD_EXPORTER:-}" ]; then
        if [ ! -f "$CODEX_THREAD_EXPORTER" ]; then
            printf 'codex-export: configured exporter not found: %s\n' "$CODEX_THREAD_EXPORTER" >&2
            return 2
        fi
        "$python" "$CODEX_THREAD_EXPORTER" "$@"
        return
    fi
    script=$(_codex_export_resolve_script) || script=''
    # Prefer pip upgrades, while a newer standalone copy remains usable without pip.
    route=$("$python" -c '
import importlib.util, pathlib, re, sys
spec = importlib.util.find_spec("codex_exporter")
def version(path):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")[:65536]
        match = re.search(r"(?m)^__version__\s*=\s*[^0-9]*([0-9]+(?:\.[0-9]+)*)", text)
        return tuple(map(int, match.group(1).split("."))) if match else ()
    except (OSError, UnicodeError):
        return ()
module = version(pathlib.Path(spec.origin).with_name("cli.py")) if spec and spec.origin else ()
print("module" if spec and module >= version(sys.argv[1]) else "script" if sys.argv[1] else "console")
' "$script") || return
    if [ "$route" = 'module' ]; then
        "$python" -m codex_exporter "$@"
    elif [ "$route" = 'script' ]; then
        "$python" "$script" "$@"
    elif command -v codex-export >/dev/null 2>&1; then
        command codex-export "$@"
    else
        printf '%s\n' 'codex-export: package or standalone exporter not found; install codex-exporter or set CODEX_THREAD_EXPORTER' >&2
        return 2
    fi
}

_codex_export_require() {
    if [ "$3" -lt "$2" ]; then
        printf 'Usage: %s\n' "$1" >&2
        return 2
    fi
}

cdx-sessions() { _codex_export_run --list-sessions "$@"; }
cdx-browse() { _codex_export_run --browse --mode thread "$@"; }
cdx-config() { _codex_export_run --show-config "$@"; }
cdx-tokenizer() { _codex_export_run --tokenizer-info "$@"; }
cdx-set-dir() {
    if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then local directory=$1; shift; _codex_export_run --set-default-out-dir "$directory" "$@"
    else _codex_export_run --choose-default-out-dir "$@"
    fi
}
cdx-set-template() {
    if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then local template=$1; shift; _codex_export_run --set-filename-template "$template" "$@"
    else _codex_export_run --print-filename-template "$@"
    fi
}
cdx-latest() {
    local mode=thread
    if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then mode=$1; shift; fi
    _codex_export_run --latest-session --mode "$mode" "$@"
}
cdx-list() { _codex_export_require "cdx-list SESSION [options]" 1 "$#" || return; local sid=$1; shift; _codex_export_run --session-id "$sid" --list "$@"; }
cdx-responses() { _codex_export_require "cdx-responses SESSION [options]" 1 "$#" || return; local sid=$1; shift; _codex_export_run --session-id "$sid" --list-responses "$@"; }

cdx-thread() {
    _codex_export_require "cdx-thread SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full "$@"
}
cdx-thread-clip() {
    _codex_export_require "cdx-thread-clip SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full --clipboard "$@"
}
cdx-response() {
    _codex_export_require "cdx-response SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode last-response --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-response-block() {
    _codex_export_require "cdx-response-block SESSION RESPONSE [options]" 2 "$#" || return
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode response --response "$n" --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-turn() {
    _codex_export_require "cdx-turn SESSION RESPONSE [options]" 2 "$#" || return
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode turn --response "$n" --no-ui-style --tool-outputs full "$@"
}
cdx-last() {
    _codex_export_require "cdx-last SESSION TURNS [options]" 2 "$#" || return
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode thread --last-n-turns "$n" "$@"
}
cdx-live() {
    _codex_export_require "cdx-live SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --live-context "$@"
}
cdx-batch() {
    _codex_export_require "cdx-batch SESSION... [-- options]" 1 "$#" || return
    local args=() id
    while [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; do
        id=$1; shift
        args+=(--session-id "$id")
    done
    if [ "${1:-}" = '--' ]; then shift; fi
    _codex_export_run "${args[@]}" --mode thread --include-session-short-id "$@"
}
cdx-final() {
    _codex_export_require "cdx-final SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode last-substantial --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-msg() {
    _codex_export_require "cdx-msg SESSION MESSAGE [options]" 2 "$#" || return
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode message --message "$n" --plain --clipboard "$@"
}
cdx-range() {
    _codex_export_require "cdx-range SESSION FIRST LAST [options]" 3 "$#" || return
    local sid=$1 first=$2 last=$3; shift 3
    _codex_export_run --session-id "$sid" --mode range --from-message "$first" --to-message "$last" --plain --clipboard "$@"
}
cdx-open() {
    local directory
    directory=$(_codex_export_run --print-out-dir "$@") || return
    directory=${directory##*$'\n'}
    if [ -z "$directory" ]; then printf '%s\n' 'Exporter returned no output directory.' >&2; return 2; fi
    mkdir -p "$directory"
    if command -v open >/dev/null 2>&1; then open "$directory"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$directory"
    else printf '%s\n' "$directory"
    fi
}

cdx-since() {
    _codex_export_require "cdx-since SESSION SELECTOR [--since-occurrence N] [options]" 2 "$#" || return
    local sid=$1 selector=$2; shift 2
    _codex_export_run --session-id "$sid" --mode thread --since "$selector" "$@"
}
cdx-turns() {
    _codex_export_require "cdx-turns SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --list-turns "$@"
}
cdx-project() {
    _codex_export_require "cdx-project PROJECT [options]" 1 "$#" || return
    local project=$1; shift
    _codex_export_run --project "$project" --mode thread "$@"
}
cdx-fast() {
    _codex_export_require "cdx-fast SESSION [options]" 1 "$#" || return
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --fast "$@"
}
cdx-audit() { _codex_export_run --audit-sessions "$@"; }
cdx-chatgpt() {
    _codex_export_require "cdx-chatgpt ARCHIVE [options]" 1 "$#" || return
    local archive=$1; shift
    _codex_export_run --chatgpt-export "$archive" "$@"
}
cdx-archive-config() { _codex_export_run --show-archive-config "$@"; }
