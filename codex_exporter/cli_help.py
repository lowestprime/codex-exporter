"""Complete option explanations and effective-default-aware CLI help.

Descriptions live beside the CLI rather than duplicating runtime behavior. The
caller supplies resolved preference values, so asking for help does not create a
progress reporter, tokenizer, snapshot, or export directory.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import shutil
from typing import Any


HELP = {
    'help': 'Show this versioned help, accepted syntax, and current effective defaults; then exit.',
    'version': 'Print the installed exporter version and exit.',
    'session_ids': 'Session UUID(s) to export; equivalent to repeating --session-id UUID. Multiple sources form a batch.',
    'session-id': 'Select a Codex session by its UUID. Repeat --session-id UUID to select multiple sessions.',
    'jsonl': 'Select a source JSONL or JSONL.ZST file by path. Repeat for a batch; the live source remains unchanged.',
    'sessions-file': 'Read a UTF-8 file containing one session UUID or JSONL path per nonempty line.',
    'latest-session': 'Select the most recently modified available local session.',
    'list-sessions': 'List indexed sessions with stable IDs, size, modification time, project, status, and title; creates no export.',
    'full-session-titles': 'Show full single-line titles in session listings; the pager wraps long values to terminal width.',
    'browse': 'Interactively search/filter/sort sessions, inspect metadata, and select one or more sessions before exporting.',
    'browse-query': 'Start the browser with a literal, case-insensitive query. Space-separated words must all match.',
    'browse-page-size': 'Maximum session/project rows on a browser page (1..200); automatic sizing uses terminal height.',
    'browse-status': 'Start the browser with all, available, live (unarchived), archived, or missing rollouts.',
    'browse-sort': 'Initial browser sort: recent, oldest, or comma-separated fields with :asc/:desc, e.g. project:asc,size:desc. --sort takes precedence.',
    'project': 'Select all available local sessions for a project name or an exact workspace path; quote paths containing spaces.',
    'chatgpt-export': 'Convert a ChatGPT account-export ZIP, extracted directory, conversations.json, or numbered JSON shards.',
    'chatgpt-audit': 'Inventory account-export conversations/shards without writing Markdown; requires --chatgpt-export PATH.',
    'chatgpt-id': 'Include only this exact ChatGPT conversation ID; repeat to include additional IDs.',
    'chatgpt-title-regex': 'Include account-export conversations whose title matches this Python regular expression.',
    'chatgpt-project-id': 'Include only account-export records with this explicit project_id; no project membership is inferred.',
    'audit-sessions': 'Read-only reconciliation of the local session catalog and live/archived rollout paths; emits complete JSON.',
    'fast': 'Use bounded-memory streaming for supported full-thread exports. Incompatible render options require the in-memory path.',
    'no-fast': 'Disable the full-thread streaming renderer and use the in-memory renderer.',
    'fast-threshold-mib': 'Automatically stream supported full threads at or above this decoded-source size in MiB (nonnegative integer).',
    'progress': 'Enable measured stage progress on stderr; stdout remains available for results.',
    'no-progress': 'Disable stage progress output on stderr.',
    'progress-style': 'Progress renderer: auto selects terminal/plain output; rich draws bars; plain logs stages; json emits events; off disables it.',
    'unknown-events': 'report records unknown schemas in the manifest; sidecar also preserves their raw JSONL in a separate member.',
    'show-config': 'Print the current preference file contents and resolved configuration metadata; then exit.',
    'print-out-dir': 'Print the effective default export directory and exit.',
    'set-default-out-dir': 'Persist PATH as the default export directory, creating it if needed; then exit.',
    'choose-default-out-dir': 'Choose and save the default export directory with the native folder dialog; then exit.',
    'set-default': 'Persist a preference with KEY=VALUE; repeat for multiple values. Keys: sort, sort_order, pager, page_size, timestamps, timestamp_timezone.',
    'set-filename-template': 'Persist a filename template and exit. Use supported {fields}; the template names a file, not a directory.',
    'print-filename-template': 'Print the effective filename template and exit.',
    'reset-filename-template': 'Remove the saved filename template and restore the built-in template; then exit.',
    'reset-config': 'Remove saved exporter preferences and restore built-in defaults; then exit.',
    'mode': 'Export scope: thread (all records), turn/response (indexed prompt+response/response only), last-response, last-assistant, last-substantial, message, range, chat (messages), chat-actions, or actions.',
    'message': 'Select a 1-based message number with --mode message; use --list to inspect original message numbers.',
    'response': 'Select a 1-based response/turn number with --mode response or --mode turn; use --list-responses to inspect it.',
    'from-message': 'Inclusive first 1-based message number for --mode range; omitted starts at message 1.',
    'to-message': 'Inclusive last 1-based message number for --mode range; omitted ends at the last message.',
    'min-chars': 'Minimum stripped assistant-text characters for --mode last-substantial; if none meets it, select the last assistant message.',
    'last-n-turns': 'Include only the last N human user turns (N >= 1), with the matching native JSONL excerpt. Alias: --last N.',
    'turn': 'Export exactly human user turn N (1-based), its associated records, and matching native excerpt.',
    'since': 'Start inclusively at turn:N, id:ID, text:WORDS, time:ISO, or literal prompt text; include all following records through the captured snapshot.',
    'since-turn': 'Start inclusively at original 1-based human user turn N; use --list-turns to find the anchor.',
    'since-id': 'Start inclusively at an exact native turn ID or user-message ID; ambiguous matches require --since-occurrence N.',
    'since-text': 'Start inclusively at a case-insensitive literal substring of a human user prompt; quote spaces, and resolve multiple matches with --since-occurrence N.',
    'since-time': 'Start at the first human user turn at/after an offset-aware ISO time (e.g. 2026-11-14T17:50:00-08:00), or a local YYYY-MM-DD date.',
    'since-occurrence': 'Choose the Nth chronological match (1-based) for an ambiguous --since text/ID selector.',
    'list-turns': 'List current human-turn numbers, timestamps, native IDs, and previews; sorting does not renumber them. After an indexing-policy upgrade, rerun this list before reusing old numeric anchors.',
    'live-context': 'Reconstruct approximate active context from recorded compaction and rollback events; this is not a byte-exact model prompt.',
    'reasoning-summaries': 'Include explicit summaries recorded in the session. Hidden/raw internal reasoning is not reconstructed.',
    'list': 'List original message numbers, roles, timestamps, character counts, and previews without writing an export.',
    'list-responses': 'List original response numbers, record/action counts, text size, line/token counts, file changes, and previews.',
    'report-events': 'Report recognized, ignored, and unknown event-schema counts on stderr during export.',
    'strict-events': 'Fail when unknown event schemas are present; inspect --report-events or the manifest for details.',
    'sort': 'Sort listing/browser rows by comma-separated columns, e.g. timestamp:desc,turn:asc. recent/oldest are time aliases; native preserves source order. Export chronology and native IDs do not change.',
    'sort-order': 'Default direction for sort fields without :asc/:desc: auto uses field defaults, asc increases, desc decreases; explicit per-field directions win.',
    'pager': 'auto pages long terminal listings; always also pages short listings; never prints directly. Files, pipes, and JSON remain complete and noninteractive in every mode.',
    'page-size': 'Maximum visible listing lines per page; 0 automatically uses terminal height. Positive values are capped to the current usable height.',
    'name': 'Override the exported thread title and its filename title component; quote a title containing spaces.',
    'out-dir': 'Write final exports to PATH for this invocation; the saved/default folder is used when omitted.',
    'choose-out-dir': 'Choose this invocation\'s export directory with the native folder dialog.',
    'remember-out-dir': 'Remember the directory selected by a folder or Save As dialog for later exports.',
    'no-remember-out-dir': 'Do not save the directory selected by this invocation\'s folder or Save As dialog.',
    'save-as': 'Choose the Markdown output filename with a native Save As dialog; single-session exports only.',
    'filename-template': 'Override the output basename template for this invocation. Supported {fields} are listed below; .md is appended if absent.',
    'save-filename-template': 'Persist the filename template used by this export for future invocations.',
    'stable-filenames': 'Use the built-in timestamp-free filename template and include a short session ID for stable names.',
    'include-session-short-id': 'Include the final eight session-ID characters in {session_short_part}; batches and streaming exports also ensure an ID suffix.',
    'collision': 'Existing output: rename chooses a new basename; overwrite replaces loose files only (archives refuse overwrite); skip leaves existing outputs unchanged; error fails.',
    'open-after': 'Reveal the successful final export in the operating system file manager.',
    'plain': 'Render selected record bodies as plain text without Markdown metadata/role headings; native excerpts remain separately available.',
    'ui-style': 'Use compact UI-style rendering for non-thread selections instead of numbered metadata headings.',
    'no-ui-style': 'Use explicit headings/metadata for non-thread selections; thread exports retain their structured format.',
    'wrap-md': 'Wrap the rendered text in an outer Markdown code fence, optionally titled by --wrap-title.',
    'wrap-title': 'Title for the optional --wrap-md wrapper; accepts arbitrary quoted text.',
    'no-file': 'Do not write the Markdown output file; combine with --stdout or --clipboard. A requested manifest may still be emitted.',
    'no-filename-counts': 'Remove {counts} from generated names; this does not suppress content/count metadata inside an export.',
    'no-frontmatter': 'Omit the metadata frontmatter from structured thread Markdown.',
    'no-map': 'Omit the thread map/navigation section from structured thread Markdown.',
    'fence-turns': 'Place each structured thread turn inside a code fence to preserve literal Markdown for ingestion.',
    'timestamps': 'Include a human-readable recorded timestamp beneath each available turn/record heading.',
    'no-timestamps': 'Omit the optional human-readable turn timestamps from Markdown; source timestamps remain unchanged.',
    'timestamp-timezone': 'Display timestamps in local time, UTC, source (original offset), a fixed +/-HH:MM offset, or an IANA zone such as America/Los_Angeles; offset-aware source times determine the instant.',
    'stdout': 'Also emit the rendered Markdown/text to stdout; use --no-file for stdout-only Markdown output.',
    'clipboard': 'Copy rendered text to the system clipboard; multiple selected exports are joined in source order.',
    'manifest': 'Write a JSON manifest containing provenance, counts, source hashes, rendering choices, and schema coverage.',
    'no-manifest': 'Disable the standalone export manifest where the selected export path supports it.',
    'manifest-path': 'Write the manifest to PATH instead of the default sibling .manifest.json; archived exports require the same parent as the public Markdown destination.',
    'archive': 'Create verified ZIP output(s) after rendering, subject to the configured per-archive byte ceiling.',
    'no-archive': 'Keep uncompressed rendered outputs instead of creating ZIP archives.',
    'source-copy': 'Include the captured native JSONL (or the exact matching selected excerpt) with the rendered export.',
    'no-source-copy': 'Omit the native JSONL member from final outputs; source files are never deleted.',
    'archive-cleanup': 'Remove generated loose staging/output files only after final archive verification succeeds.',
    'no-archive-cleanup': 'Retain generated loose output files after verified archives are created.',
    'archiver-path': 'Use this exact 7-Zip-compatible CLI executable; otherwise discover an installed supported archiver.',
    'archive-backend': 'auto chooses the matching backend; 7zip uses the external CLI; python supports the explicitly selected Deflate path.',
    'separate-archives': 'Package each selected session separately instead of combining a batch into one bounded archive set.',
    'archive-max-bytes': 'Hard ceiling for every final ZIP, in integer bytes (maximum 512000000). Oversized exports become independently readable ZIP parts.',
    'archive-level': 'Compression effort, integer 1..9; higher levels generally trade more processing for smaller output.',
    'archive-method': 'ZIP compression method: LZMA preserves the high-ratio external-archiver configuration; Deflate is an explicit alternative.',
    'archive-dictionary': 'Maximum LZMA dictionary with a k/m/g suffix, e.g. 256m; reduced for shorter known inputs. Larger dictionaries can increase encoder/decoder memory.',
    'archive-word-size': 'LZMA fast-byte/match-length setting (integer 5..273), e.g. 64; meaningful with the LZMA method.',
    'archive-threads': 'Maximum compression threads (integer 1..128); actual concurrency depends on the codec, archiver, and member count.',
    'work-dir': 'Parent directory for private temporary export work. Choose a volume with adequate free space; final outputs still use --out-dir.',
    'show-archive-config': 'Print effective archive preferences as JSON, including built-in defaults, and exit.',
    'set-archive-default': 'Persist archive KEY=VALUE; repeat for multiple values. Keys: archive, copy_source, cleanup, archiver, backend, level, method, dictionary, word_size, threads, max_bytes, review, review_chunk_bytes, work_dir.',
    'review': 'Include additional lossless text-review parts inside the same final archive set; opt-in and increases content size.',
    'no-review': 'Omit optional review parts; retain the configured Markdown, manifest, and native JSONL members.',
    'review-chunk-bytes': 'Maximum UTF-8 bytes per optional review text part, including its header (4096..1500000).',
    'review-from': 'Explicitly create a standalone review ZIP and loose text folder from an existing export ZIP or text file.',
    'diagnose-archive': 'Inspect this existing ZIP without modifying it; report member formats, dictionary sizes, access, hashes/CRC, and current-runtime decoding as JSON. Repeat for multiple paths.',
    'diagnose-access': 'Inspect read access and, on Windows, ACL metadata for an explicit file/directory path; repeatable; no repair.',
    'repair-access': 'Restore inherited Windows permissions on explicitly named generated export paths; repeatable; does not change source sessions.',
    'archive-check': 'For --diagnose-archive: full reads/decompresses and checks member hashes/CRCs; metadata reads only archive/member metadata.',
    'archive-max-dictionary-mib': 'For --diagnose-archive, refuse decoding a member needing an LZMA dictionary above this explicit MiB guard (integer >= 1).',
    'tokenizer': 'Token counter: auto uses installed tiktoken or a labeled estimate; tiktoken requires it; regex uses the estimate.',
    'token-encoding': 'Exact tiktoken encoding name, e.g. cl100k_base or o200k_base; only applies when exact tokenization is available.',
    'require-tiktoken': 'Fail if exact tiktoken counting is unavailable; do not silently fall back to estimated counts.',
    'tokenizer-info': 'Print the selected tokenizer, encoding, and whether counts are exact or estimated; then exit.',
    'source-truncation': 'Handle truncation already present in recorded tool output: annotate adds a notice, preserve leaves it as recorded, error fails.',
    'redact': 'Redact recognized secret patterns from rendered text. A retained native JSONL member still contains original recorded bytes.',
    'fix-mojibake': 'Compatibility flag: text repair is already enabled by default in the in-memory renderer; use --keep-mojibake to disable it.',
    'keep-mojibake': 'Keep suspicious original character sequences instead of applying the renderer\'s mojibake-repair heuristics.',
    'keep-hogs': 'Disable in-memory removal/truncation of embedded blobs, long lines, repeated lines, and terminal controls; streaming already preserves printable text.',
    'max-line-chars': 'In-memory cleanup threshold for a single line (characters); overlong lines keep head/tail with an omission marker unless --keep-hogs.',
    'max-repeated-lines': 'In-memory cleanup threshold for consecutive identical lines; later repetitions receive an omission marker unless --keep-hogs.',
    'tool-outputs': 'Tool-output policy: none omits bodies; summary summarizes; tail keeps the end; full retains recorded output.',
    'max-tool-chars': 'Character budget used by bounded tool-output modes; full retains the complete recorded output.',
    'json': 'Emit machine-readable result/diagnostic JSON on stdout; JSON listings are never paginated.',
}

# Options whose effective defaults affect selection, rendering, or persistent
# behavior. An absent optional source/action does not need a noisy "False" note.
_DEFAULT_DESTS = {
    'browse_query', 'browse_page_size', 'browse_status', 'browse_sort', 'fast',
    'fast_threshold_mib', 'progress', 'progress_style', 'unknown_events', 'mode',
    'min_chars', 'sort', 'sort_order', 'pager', 'page_size', 'out_dir',
    'remember_out_dir', 'filename_template', 'collision', 'ui_style', 'wrap_title',
    'manifest', 'archive_enabled', 'copy_source', 'archive_cleanup', 'archiver_path',
    'archive_backend', 'archive_max_bytes', 'archive_level', 'archive_method',
    'archive_dictionary', 'archive_word_size', 'archive_threads', 'review',
    'review_chunk_bytes', 'archive_check', 'archive_max_dictionary_mib', 'tokenizer',
    'token_encoding', 'source_truncation', 'max_line_chars', 'max_repeated_lines',
    'tool_outputs', 'max_tool_chars', 'timestamps', 'timestamp_timezone', 'work_dir',
}
_AUTO_NONE = {'fast', 'progress', 'browse_page_size', 'archiver_path', 'work_dir', 'mode'}
_METAVARS = {
    'session_ids': 'UUID', 'session_id': 'UUID', 'jsonl': 'PATH', 'sessions_file': 'PATH',
    'browse_query': 'WORDS', 'browse_page_size': 'N', 'browse_sort': 'SPEC',
    'sort': 'SPEC', 'page_size': 'N', 'name': 'TITLE', 'out_dir': 'PATH',
    'filename_template': 'TEMPLATE', 'set_filename_template': 'TEMPLATE',
    'set_default_out_dir': 'PATH', 'message': 'N', 'response': 'N',
    'from_message': 'N', 'to_message': 'N', 'min_chars': 'N', 'last_n_turns': 'N',
    'turn': 'N', 'wrap_title': 'TITLE', 'manifest_path': 'PATH', 'archiver_path': 'EXE',
    'archive_max_bytes': 'BYTES', 'archive_level': 'N', 'archive_dictionary': 'SIZE',
    'archive_word_size': 'N', 'archive_threads': 'N', 'review_chunk_bytes': 'BYTES',
    'archive_max_dictionary_mib': 'MIB', 'fast_threshold_mib': 'MIB',
    'token_encoding': 'ENCODING', 'max_line_chars': 'N', 'max_repeated_lines': 'N',
    'max_tool_chars': 'N', 'chatgpt_id': 'ID', 'chatgpt_title_regex': 'REGEX',
    'chatgpt_project_id': 'ID', 'work_dir': 'PATH', 'timestamp_timezone': 'ZONE',
}


class ExportHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Readable descriptions with stable wrapping and a compact option column."""

    def __init__(self, prog: str, **kwargs: Any):
        width = max(60, min(110, shutil.get_terminal_size(fallback=(100, 30)).columns))
        kwargs.setdefault('width', width)
        kwargs.setdefault('max_help_position', 32)
        super().__init__(prog, **kwargs)


def _default_text(value: Any, dest: str) -> str:
    if value is None:
        return 'automatic' if dest in _AUTO_NONE else 'not set'
    if isinstance(value, bool):
        return 'on' if value else 'off'
    if value == '':
        if dest == 'work_dir':
            return 'output-directory volume'
        return 'default for the listing type' if dest == 'sort' else 'none'
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def configure_help(parser: argparse.ArgumentParser, *, version: str,
                   defaults: Mapping[str, Any] | None = None) -> None:
    """Annotate every visible action and show supplied effective preferences.

    Call once after adding arguments and before parse_args. Values in ``defaults``
    are argparse destination keys, for example archive_method='LZMA'. They only
    affect the explanatory text; argument parsing remains the caller's concern.
    """
    supplied = defaults or {}
    effective_defaults = {}
    for action in parser._actions:
        if action.default != argparse.SUPPRESS:
            effective_defaults.setdefault(action.dest, action.default)
    effective_defaults.update(supplied)
    parser.formatter_class = ExportHelpFormatter
    parser.usage = '%(prog)s [UUID ...] [SOURCE OPTIONS] [SELECTION] [OUTPUT OPTIONS]'
    missing = []
    for action in parser._actions:
        name = action.option_strings[0].lstrip('-') if action.option_strings else action.dest
        if name == 'h':
            name = 'help'
        description = HELP.get(name)
        if description is None:
            description = action.help if action.help not in (None, argparse.SUPPRESS) else None
        if description is None:
            missing.append(name)
            continue
        # argparse applies %-formatting to help. Literal user preference values
        # may contain percent signs, so escape the complete final description.
        if action.choices:
            description += ' Accepted: ' + ', '.join(map(str, action.choices)) + '.'
        if action.dest in _DEFAULT_DESTS:
            value = effective_defaults.get(action.dest, action.default)
            description += f' Current default ({action.dest}): {_default_text(value, action.dest)}.'
        action.help = description.replace('%', '%%')
        if action.metavar is None and action.dest in _METAVARS and action.nargs != 0:
            action.metavar = _METAVARS[action.dest]
    if missing:
        raise ValueError('Missing CLI help descriptions: ' + ', '.join(missing))
    parser.epilog = (
        'Examples:\n'
        '  codex-export --list-sessions --sort project:asc,modified:desc\n'
        '  codex-export --session-id UUID --list-turns --sort turn:desc\n'
        '  codex-export --session-id UUID --since-text "exact steering prompt"\n'
        '  codex-export --browse --browse-sort size:desc --browse-status available\n'
        '  codex-export --set-default pager=auto --set-default page_size=0\n'
        '\n'
        'Listing keys (comma-separated; add :asc or :desc to each):\n'
        '  Sessions: id, title, project, workspace, path, size, modified, status,\n'
        '            archived, missing, compression, title_source.\n'
        '  Turns: turn, timestamp, id, preview, chars, line, records, source.\n'
        '  Messages: index, role, timestamp, chars, preview, id, kind, line, source.\n'
        '  Responses: index, user_message, records, assistant_chars, lines, tokens,\n'
        '             actions, edited, created, preview, timestamp, source.\n'
        '  Events: event, count, status, timestamp, source. Projects also accept count.\n'
        '  recent/oldest select time order; native preserves catalog/source order.\n'
        '  auto direction: descending for sizes/counts/modified; ascending otherwise.\n'
        '\n'
        'Terminal listing keys: Enter/Space/PgDn next page; PgUp previous;\n'
        'Up/Down one line; Home/End first/last; q exits. Pipes and JSON stay complete.\n'
        '\n'
        'Filename fields: {title}, {mode}, {stamp}, {session_id}, {session_short},\n'
        '{session_short_part}, {lines}, {tokens}, {token_method}, {token_encoding},\n'
        '{counts}. Quote the template. Streaming may use a short ID instead of counts.\n'
        '\n'
        'Saved preferences are overridden by explicit flags for this invocation.\n'
        'Run without arguments to show this same help. codex-exporter is an alias.\n'
    )
    previous = getattr(parser, '_codex_base_format_help', parser.format_help)
    parser._codex_base_format_help = previous
    parser.format_help = lambda: f'Codex Exporter {version}\n\n' + previous()
