# Codex Thread Exporter bash/zsh helper functions.
# Source this from your ~/.bashrc, ~/.zshrc, or your shell profile of choice.
# Mirrors the PowerShell helper API: cdx-thread, cdx-response, cdx-turn, etc.

# Resolve the exporter script. Order:
#   1. $CODEX_THREAD_EXPORTER if set and pointing at a real file
#   2. ~/.codex-tools/Export-CodexThread.py
#   3. ~/.agents/skills/codex-thread-export/scripts/Export-CodexThread.py
_codex_export_resolve() {
    if [ -n "$CODEX_THREAD_EXPORTER" ] && [ -f "$CODEX_THREAD_EXPORTER" ]; then
        printf '%s' "$CODEX_THREAD_EXPORTER"; return 0
    fi
    for cand in \
        "$HOME/.codex-tools/Export-CodexThread.py" \
        "$HOME/.agents/skills/codex-thread-export/scripts/Export-CodexThread.py"
    do
        if [ -f "$cand" ]; then printf '%s' "$cand"; return 0; fi
    done
    return 1
}

_codex_export_python() {
    if command -v python3 >/dev/null 2>&1; then printf '%s' python3
    elif command -v python  >/dev/null 2>&1; then printf '%s' python
    else return 1; fi
}

_codex_export_dir() {
    printf '%s' "${CODEX_THREAD_EXPORT_DIR:-$HOME/codex-thread-exports}"
}

_codex_export_run() {
    local script
    script=$(_codex_export_resolve) || { echo "codex-export: exporter script not found. Set CODEX_THREAD_EXPORTER or install to ~/.codex-tools/" >&2; return 2; }
    local py
    py=$(_codex_export_python) || { echo "codex-export: python3 (or python) not found in PATH" >&2; return 2; }
    local out
    out=$(_codex_export_dir)
    mkdir -p "$out"
    "$py" "$script" --out-dir "$out" "$@"
}

cdx-list()           { _codex_export_run --session-id "$1" --list; }
cdx-responses()      { _codex_export_run --session-id "$1" --list-responses; }
cdx-sessions()       { _codex_export_run --list-sessions; }
cdx-latest()         { _codex_export_run --latest-session --mode "${1:-thread}"; }

cdx-thread() {
    local sid="$1"; shift || true
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full "$@"
}

cdx-thread-clip() {
    local sid="$1"; shift || true
    _codex_export_run --session-id "$sid" --mode thread --tool-outputs full --clipboard "$@"
}

cdx-response() {
    local sid="$1"; shift || true
    _codex_export_run --session-id "$sid" --mode last-response --wrap-md --clipboard --tool-outputs full "$@"
}

cdx-response-block() {
    local sid="$1"; local n="$2"; shift 2 || true
    _codex_export_run --session-id "$sid" --mode response --response "$n" --wrap-md --clipboard --tool-outputs full "$@"
}

cdx-turn() {
    local sid="$1"; local n="$2"; shift 2 || true
    _codex_export_run --session-id "$sid" --mode turn --response "$n" --no-ui-style --tool-outputs full "$@"
}

cdx-final() {
    local sid="$1"; shift || true
    _codex_export_run --session-id "$sid" --mode last-substantial --wrap-md --clipboard --tool-outputs full "$@"
}

cdx-msg() {
    local sid="$1"; local n="$2"; shift 2 || true
    _codex_export_run --session-id "$sid" --mode message --message "$n" --plain --clipboard "$@"
}

cdx-range() {
    local sid="$1"; local a="$2"; local b="$3"; shift 3 || true
    _codex_export_run --session-id "$sid" --mode range --from-message "$a" --to-message "$b" --plain --clipboard "$@"
}

cdx-open() {
    local out
    out=$(_codex_export_dir)
    mkdir -p "$out"
    if   command -v open    >/dev/null 2>&1; then open    "$out"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$out"
    else echo "$out"
    fi
}
