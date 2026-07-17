#!/usr/bin/env bash
# Codex Exporter bash/zsh helper functions (v0.2+).
# Source from ~/.bashrc, ~/.zshrc, or another shell profile.

_codex_export_resolve_script() {
    if [ -n "${CODEX_THREAD_EXPORTER:-}" ] && [ -f "$CODEX_THREAD_EXPORTER" ]; then
        printf '%s\n' "$CODEX_THREAD_EXPORTER"
        return 0
    fi
    local candidate
    for candidate in \
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
    if [ -n "${CODEX_EXPORT_PYTHON:-}" ] && [ -x "$CODEX_EXPORT_PYTHON" ]; then
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
    if command -v codex-export >/dev/null 2>&1; then
        command codex-export "$@"
        return
    fi

    local script python
    script=$(_codex_export_resolve_script) || {
        printf '%s\n' 'codex-export: exporter script not found; set CODEX_THREAD_EXPORTER or install to ~/.codex-tools' >&2
        return 2
    }
    python=$(_codex_export_python) || {
        printf '%s\n' 'codex-export: Python not found; set CODEX_EXPORT_PYTHON' >&2
        return 2
    }
    "$python" "$script" "$@"
}

cdx-sessions() { _codex_export_run --list-sessions "$@"; }
cdx-browse() { _codex_export_run --browse --mode thread "$@"; }
cdx-config() { _codex_export_run --show-config; }
cdx-tokenizer() { _codex_export_run --tokenizer-info "$@"; }
cdx-set-dir() {
    if [ "$#" -gt 0 ]; then _codex_export_run --set-default-out-dir "$1"
    else _codex_export_run --choose-default-out-dir
    fi
}
cdx-set-template() {
    if [ "$#" -gt 0 ]; then _codex_export_run --set-filename-template "$1"
    else _codex_export_run --print-filename-template
    fi
}
cdx-latest() { _codex_export_run --latest-session --mode "${1:-thread}"; }
cdx-list() { local sid=$1; shift; _codex_export_run --session-id "$sid" --list "$@"; }
cdx-responses() { local sid=$1; shift; _codex_export_run --session-id "$sid" --list-responses "$@"; }

cdx-thread() {
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full "$@"
}
cdx-thread-clip() {
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full --clipboard "$@"
}
cdx-response() {
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode last-response --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-response-block() {
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode response --response "$n" --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-turn() {
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode turn --response "$n" --no-ui-style --tool-outputs full "$@"
}
cdx-last() {
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode thread --last-n-turns "$n" "$@"
}
cdx-live() {
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode thread --live-context "$@"
}
cdx-batch() {
    local args=() id
    while [ "$#" -gt 0 ] && [ "$1" != '--' ]; do
        id=$1; shift
        args+=(--session-id "$id")
    done
    if [ "${1:-}" = '--' ]; then shift; fi
    _codex_export_run "${args[@]}" --mode thread --include-session-short-id "$@"
}
cdx-final() {
    local sid=$1; shift
    _codex_export_run --session-id "$sid" --mode last-substantial --wrap-md --clipboard --tool-outputs full "$@"
}
cdx-msg() {
    local sid=$1 n=$2; shift 2
    _codex_export_run --session-id "$sid" --mode message --message "$n" --plain --clipboard "$@"
}
cdx-range() {
    local sid=$1 first=$2 last=$3; shift 3
    _codex_export_run --session-id "$sid" --mode range --from-message "$first" --to-message "$last" --plain --clipboard "$@"
}
cdx-open() {
    local directory
    directory=$(_codex_export_run --print-out-dir | tail -n 1) || return
    mkdir -p "$directory"
    if command -v open >/dev/null 2>&1; then open "$directory"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$directory"
    else printf '%s\n' "$directory"
    fi
}
