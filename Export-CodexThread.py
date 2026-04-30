#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


__version__ = "0.1.0"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = Path(os.environ.get("CODEX_THREAD_EXPORT_DIR") or (SCRIPT_DIR / "codex-thread-exports"))
LOCAL_TZ_ENV = "CODEX_EXPORT_TZ"


SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[:=]\s*)([^\s'\";]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(cloudflare[_-]?tunnel[_-]?token)(\s*[:=]\s*)([^\s'\";]+)"), r"\1\2[REDACTED]"),
]

DATA_URI_PATTERN = re.compile(
    r"data:(?P<mime>image|audio|video|application|font)/[A-Za-z0-9.+_-]+;base64,(?P<data>[A-Za-z0-9+/=\r\n_-]{512,})",
    re.IGNORECASE,
)
MARKDOWN_DATA_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[A-Za-z0-9.+_-]+;base64,[A-Za-z0-9+/=\r\n_-]{512,}\s*\)",
    re.IGNORECASE,
)
HTML_DATA_SRC_PATTERN = re.compile(
    r"""(?P<prefix>\bsrc\s*=\s*["'])data:(?P<mime>image|audio|video|application|font)/[A-Za-z0-9.+_-]+;base64,[A-Za-z0-9+/=\r\n_-]{512,}(?P<suffix>["'])""",
    re.IGNORECASE,
)
BASE64_BLOB_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?:[A-Za-z0-9+/]{2048,}={0,2}|[A-Za-z0-9_-]{2048,}={0,2})(?![A-Za-z0-9+/=_-])"
)
HEX_BLOB_PATTERN = re.compile(r"(?<![A-Fa-f0-9])(?:[A-Fa-f0-9]{4096,})(?![A-Fa-f0-9])")
OSC_PATTERN = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
ANSI_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

IMAGEISH_TYPES = {
    "image",
    "input_image",
    "output_image",
    "screenshot",
    "computer_screenshot",
    "image_url",
    "file_image",
}

MESSAGE_TYPES = {"message", "user_message", "assistant_message", "agent_message", "system_message"}
ACTION_HINTS = (
    "tool",
    "function",
    "shell",
    "command",
    "exec",
    "apply_patch",
    "patch",
    "edit",
    "write_stdin",
    "call",
    "local_shell",
    "browser",
)
NOISY_TYPE_HINTS = (
    "token_count",
    "turn_context",
    "session_meta",
    "reasoning",
    "delta",
    "rate_limits",
)


@dataclass
class Record:
    kind: str                 # message | action | note
    role: str                 # user | assistant | tool | system
    text: str
    timestamp: str = ""
    source_type: str = ""
    seq: int = 0
    command_key: str = ""
    output_key: str = ""


def cp437_cp1252_mojibake_repair(text: str) -> str:
    """Repair already-corrupted clip.exe-style mojibake such as IÆll -> I’ll."""
    if not any(ch in text for ch in "ÆæôöâäÖÜ"):
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x80 <= code <= 0xFF:
            try:
                out.append(bytes([code]).decode("cp1252"))
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    return "".join(out)


def redact(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def bytes_from_base64_len(chars: int) -> int:
    return int(chars * 3 / 4)


def summarize_blob(label: str, raw: str, extra: str = "") -> str:
    chars = len(raw)
    approx = bytes_from_base64_len(chars) if "base64" in label.lower() else chars
    digest = hashlib.sha256(raw[:2_000_000].encode("utf-8", errors="ignore")).hexdigest()[:16]
    suffix = f"; {extra}" if extra else ""
    return f"[{label} omitted: {chars:,} characters, ~{approx:,} bytes, sha256-prefix={digest}{suffix}]"


def strip_hogs(
    text: str,
    *,
    enabled: bool = True,
    max_line_chars: int = 20_000,
    max_repeated_lines: int = 25,
) -> str:
    if not enabled or not text:
        return text or ""

    # Remove OSC/title-control sequences before stripping ESC/BEL. Otherwise
    # Windows/Next terminal title updates can leave visible residues such as
    # ``0;npm0;npm run dev`` in exported output.
    text = OSC_PATTERN.sub("", text)
    text = ANSI_PATTERN.sub("", text)
    text = CONTROL_PATTERN.sub("", text)
    text = re.sub(r"(?m)^0;[^\n]*(?:\n|$)", "", text)

    def repl_markdown_img(match: re.Match[str]) -> str:
        alt = match.group("alt") or "image"
        return f"![{alt}]([base64 image omitted])"

    def repl_html_src(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}[base64 {match.group('mime')} omitted]{match.group('suffix')}"

    def repl_data_uri(match: re.Match[str]) -> str:
        return summarize_blob(f"data:{match.group('mime')} base64 payload", match.group("data"))

    def repl_base64(match: re.Match[str]) -> str:
        blob = re.sub(r"\s+", "", match.group(0))
        try:
            padded = blob + ("=" * ((4 - len(blob) % 4) % 4))
            base64.b64decode(padded, validate=False)
        except Exception:
            return match.group(0)
        return summarize_blob("base64 blob", blob)

    text = MARKDOWN_DATA_IMAGE_PATTERN.sub(repl_markdown_img, text)
    text = HTML_DATA_SRC_PATTERN.sub(repl_html_src, text)
    text = DATA_URI_PATTERN.sub(repl_data_uri, text)
    text = BASE64_BLOB_PATTERN.sub(repl_base64, text)
    text = HEX_BLOB_PATTERN.sub(lambda m: summarize_blob("hex blob", m.group(0)), text)

    lines = text.splitlines()
    compacted: list[str] = []
    prev: str | None = None
    repeat_count = 0

    for line in lines:
        if len(line) > max_line_chars:
            digest = hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()[:16]
            head = line[:1200].rstrip()
            tail = line[-1200:].lstrip()
            line = f"{head}\n[single overlong line truncated: {len(line):,} characters, sha256-prefix={digest}]\n{tail}"

        if line == prev:
            repeat_count += 1
            if repeat_count == max_repeated_lines + 1:
                compacted.append(f"[repeated identical line omitted after {max_repeated_lines:,} repeats]")
            elif repeat_count > max_repeated_lines:
                continue
            else:
                compacted.append(line)
        else:
            prev = line
            repeat_count = 1
            compacted.append(line)

    return "\n".join(compacted).strip()


def safe_slug(text: str, max_len: int = 80) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s.-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._- ")
    return (text or "codex_thread")[:max_len].strip("._- ")


def _abbrev_tz(name: str) -> str:
    """Compress full Windows tz names like ``Pacific Daylight Time`` to ``PDT``.

    Already-short forms like ``PDT`` and ``UTC+02:00`` pass through. Used to
    keep filenames safe across platforms (Windows ``%Z`` returns the full
    locale name; Linux/macOS return the abbreviation).
    """
    s = (name or "").strip()
    if not s:
        return ""
    if " " in s:
        words = [w for w in re.split(r"\s+", s) if w]
        # Map "Pacific Daylight Time" -> "PDT"; otherwise keep the joined words.
        if len(words) >= 2 and all(w[0].isalpha() for w in words):
            return "".join(w[0] for w in words).upper()
        return "".join(words)
    return s


def export_stamp() -> str:
    """Render the timestamp suffix used in output filenames.

    Resolution order:
      1. ``CODEX_EXPORT_TZ`` env var (any IANA tz, e.g. ``America/Los_Angeles``)
      2. The system local timezone
      3. Naive local time (last resort)

    Format is preserved across versions: ``WED_04292026_060940_PM-PDT``.
    """
    tz_name = os.environ.get(LOCAL_TZ_ENV, "").strip()
    now: datetime
    if ZoneInfo and tz_name:
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now().astimezone()
    else:
        try:
            now = datetime.now().astimezone()
        except Exception:
            now = datetime.now()
    head = now.strftime("%a_%m%d%Y_%I%M%S_%p").upper()
    tz = _abbrev_tz(now.strftime("%Z"))
    return f"{head}-{tz}" if tz else head


# Back-compat alias for callers that still import the old name.
pacific_stamp = export_stamp


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def session_roots() -> list[Path]:
    home = codex_home()
    return [p for p in (home / "sessions", home / "archived_sessions") if p.exists()]


SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def all_session_files() -> list[Path]:
    out: list[Path] = []
    for root in session_roots():
        out.extend(root.rglob("*.jsonl"))
    return out


def parse_session_id_from_name(path: Path) -> str:
    m = SESSION_ID_RE.search(path.name)
    return m.group(0) if m else ""


def latest_session_file() -> Path:
    candidates = all_session_files()
    if not candidates:
        raise FileNotFoundError(f"No Codex session JSONL files found under {codex_home()}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_session_file(session_id: str) -> Path:
    roots = session_roots()
    if not roots:
        raise FileNotFoundError(f"No Codex session roots found under {codex_home()}")

    candidates: list[Path] = []
    for root in roots:
        candidates.extend(root.rglob(f"*{session_id}*.jsonl"))

    if not candidates:
        needle = session_id.encode("utf-8")
        for root in roots:
            for path in root.rglob("*.jsonl"):
                try:
                    with path.open("rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            if needle in chunk:
                                candidates.append(path)
                                break
                except OSError:
                    continue

    if not candidates:
        raise FileNotFoundError(f"No local Codex JSONL transcript found for session: {session_id}")

    return max(candidates, key=lambda p: p.stat().st_mtime)


def maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not ((s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def as_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(as_string(v) for v in value if as_string(v)).strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def image_placeholder(value: dict[str, Any]) -> str:
    mime = value.get("mime_type") or value.get("mime") or value.get("media_type") or "image"
    detail = value.get("detail") or value.get("quality") or ""
    url = value.get("url") or value.get("image_url") or ""
    if isinstance(url, dict):
        url = url.get("url") or ""
    if isinstance(url, str) and url.startswith("data:"):
        return f"[{mime} content part omitted: embedded base64 image/data URI]"
    suffix = f", detail={detail}" if detail else ""
    if url:
        return f"[{mime} content part omitted: url/reference present{suffix}]"
    return f"[{mime} content part omitted{suffix}]"


def extract_text_parts(
    value: Any,
    *,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
    depth: int = 0,
    json_aware: bool = False,
) -> list[str]:
    if value is None or depth > 14:
        return []

    if isinstance(value, str):
        if json_aware:
            parsed = maybe_json(value)
            if parsed is not None:
                return extract_text_parts(
                    parsed,
                    strip=strip,
                    max_line_chars=max_line_chars,
                    max_repeated_lines=max_repeated_lines,
                    depth=depth + 1,
                    json_aware=True,
                )
        text = strip_hogs(value, enabled=strip, max_line_chars=max_line_chars, max_repeated_lines=max_repeated_lines)
        return [text] if text else []

    if isinstance(value, (int, float, bool)):
        return [str(value)]

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(
                extract_text_parts(
                    item,
                    strip=strip,
                    max_line_chars=max_line_chars,
                    max_repeated_lines=max_repeated_lines,
                    depth=depth + 1,
                    json_aware=json_aware,
                )
            )
        return [p for p in parts if p]

    if isinstance(value, dict):
        value_type = str(value.get("type") or "").lower()
        if value_type in IMAGEISH_TYPES or any(k in value for k in ("image_url", "b64_json", "base64_image")):
            return [image_placeholder(value)]

        ordered_keys = (
            "text",
            "output_text",
            "input_text",
            "markdown",
            "message",
            "content",
            "summary",
            "transcript",
            "stdout",
            "stderr",
            "output",
            "result",
            "diff",
            "patch",
        )
        parts: list[str] = []
        for key in ordered_keys:
            if key in value:
                parts.extend(
                    extract_text_parts(
                        value.get(key),
                        strip=strip,
                        max_line_chars=max_line_chars,
                        max_repeated_lines=max_repeated_lines,
                        depth=depth + 1,
                        json_aware=json_aware,
                    )
                )

        if not parts:
            for key in ("item", "payload", "data", "event", "response"):
                if key in value:
                    parts.extend(
                        extract_text_parts(
                            value.get(key),
                            strip=strip,
                            max_line_chars=max_line_chars,
                            max_repeated_lines=max_repeated_lines,
                            depth=depth + 1,
                            json_aware=json_aware,
                        )
                    )
                    if parts:
                        break
        return [p for p in parts if p]

    text = strip_hogs(str(value), enabled=strip, max_line_chars=max_line_chars, max_repeated_lines=max_repeated_lines)
    return [text] if text else []


def extract_text(value: Any, *, strip: bool, max_line_chars: int, max_repeated_lines: int, json_aware: bool = False) -> str:
    parts = extract_text_parts(value, strip=strip, max_line_chars=max_line_chars, max_repeated_lines=max_repeated_lines, json_aware=json_aware)
    # Preserve order while de-duplicating exact repeated payloads.
    return "\n\n".join(dict.fromkeys(p.strip() for p in parts if p.strip())).strip()


def parse_command_envelope(value: Any) -> tuple[str, dict[str, Any]]:
    """Return (command, metadata) from Codex/container-style arguments.

    Important: local Codex JSONL often stores command arguments as a JSON string:
    {"cmd":"...","workdir":"...","yield_time_ms":1000,"max_output_tokens":4000}
    The GUI displays only cmd; exporting the raw JSON was the main v3 mismatch.
    """
    meta: dict[str, Any] = {}

    if isinstance(value, str):
        parsed = maybe_json(value)
        if parsed is not None:
            return parse_command_envelope(parsed)
        return value, meta

    if isinstance(value, list):
        return " ".join(as_string(x) for x in value), meta

    if isinstance(value, dict):
        meta = {k: v for k, v in value.items() if k not in {"cmd", "command", "script", "code", "shell_command"}}
        for key in ("cmd", "command", "script", "code", "shell_command"):
            if key in value:
                return as_string(value.get(key)), meta
        if "argv" in value:
            return as_string(value.get("argv")), meta
        if "args" in value:
            return parse_command_envelope(value.get("args"))
        if "arguments" in value:
            return parse_command_envelope(value.get("arguments"))
        return "", meta

    return as_string(value), meta


def strip_powershell_wrapper(command: str) -> str:
    command = command.strip()
    if not command:
        return ""

    # C:\Program Files\PowerShell\7\pwsh.exe -Command <script>
    wrapper = re.compile(
        r"""^(?P<exe>(?:"[^"]*pwsh(?:\.exe)?"|[A-Za-z]:\\[^\r\n]*?pwsh(?:\.exe)?|pwsh(?:\.exe)?))\s+-Command\s+(?P<script>[\s\S]+)$""",
        re.IGNORECASE,
    )
    m = wrapper.match(command)
    if m:
        script = m.group("script").strip()
        if (script.startswith('"') and script.endswith('"')) or (script.startswith("'") and script.endswith("'")):
            script = script[1:-1]
        return script.strip()

    return command


def normalize_command(command: str) -> str:
    command = command.replace("\r\n", "\n").replace("\r", "\n").strip()
    command = strip_powershell_wrapper(command)
    return command.strip()


def command_key(command: str) -> str:
    cmd = normalize_command(command)
    cmd = re.sub(r"\s+", " ", cmd).strip().lower()
    return cmd


def extract_command(obj: dict[str, Any]) -> str:
    # Prefer arguments/call fields that are closest to user-visible tool invocations.
    for key in ("arguments", "args"):
        if key in obj:
            cmd, _meta = parse_command_envelope(obj.get(key))
            if cmd:
                return normalize_command(cmd)

    for key in ("command", "cmd", "shell_command", "script", "code"):
        if key in obj:
            cmd, _meta = parse_command_envelope(obj.get(key))
            if cmd:
                return normalize_command(cmd)

    for key in ("call", "action", "input"):
        if key in obj:
            cmd, _meta = parse_command_envelope(obj.get(key))
            if cmd:
                return normalize_command(cmd)

    return ""


def strip_codex_tool_metadata(text: str) -> tuple[str, dict[str, Any]]:
    """Remove Codex/container transport headers while preserving substantive output.

    The Codex GUI direct-copy view shows command stdout/stderr and a concise
    Success/Failure state, not container transport details such as Chunk ID,
    Wall time, Process exited, Original token count, and Output:. Keep those
    details out of normal Markdown while extracting exit_code for status.
    """
    meta: dict[str, Any] = {}
    if not text:
        return "", meta

    lines = text.splitlines()
    if not lines:
        return text, meta

    # Common container-tool shape:
    # Chunk ID: ...
    # Wall time: ...
    # Process exited with code 0
    # Original token count: ...
    # Output:
    # <substantive output>
    saw_transport_header = False
    output_idx: int | None = None
    kept: list[str] = []

    for i, line in enumerate(lines[:8]):
        stripped = line.strip()
        if stripped.startswith("Chunk ID:") or stripped.startswith("Wall time:") or stripped.startswith("Original token count:"):
            saw_transport_header = True
            continue
        m = re.match(r"Process exited with code\s+(-?\d+)", stripped)
        if m:
            saw_transport_header = True
            try:
                meta["exit_code"] = int(m.group(1))
            except ValueError:
                meta["exit_code"] = m.group(1)
            continue
        if stripped == "Output:":
            saw_transport_header = True
            output_idx = i + 1
            break
        kept.append(line)

    if output_idx is not None:
        return "\n".join(lines[output_idx:]).strip(), meta

    if saw_transport_header:
        # Fall back to dropping only recognized transport lines from the top.
        rest: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("Chunk ID:", "Wall time:", "Original token count:")):
                continue
            m = re.match(r"Process exited with code\s+(-?\d+)", stripped)
            if m:
                try:
                    meta["exit_code"] = int(m.group(1))
                except ValueError:
                    meta["exit_code"] = m.group(1)
                continue
            if stripped == "Output:":
                continue
            rest.append(line)
        return "\n".join(rest).strip(), meta

    return text, meta


def extract_output_and_meta(
    obj: dict[str, Any],
    *,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {}

    # Tool output records often have {"output":"...","metadata":{...}} as a JSON string.
    for key in ("output", "result", "stdout", "stderr", "text", "content", "message", "diff", "patch"):
        if key not in obj:
            continue

        value = obj.get(key)
        parsed = maybe_json(value) if isinstance(value, str) else None
        if isinstance(parsed, dict):
            if isinstance(parsed.get("metadata"), dict):
                meta.update(parsed["metadata"])
            text = extract_text(
                parsed.get("output") if "output" in parsed else parsed,
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
                json_aware=True,
            )
        else:
            text = extract_text(
                value,
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
                json_aware=True,
            )

        if text:
            clean_text, transport_meta = strip_codex_tool_metadata(text)
            meta.update({k: v for k, v in transport_meta.items() if k not in meta})
            return clean_text, meta

    if isinstance(obj.get("metadata"), dict):
        meta.update(obj["metadata"])

    return "", meta


def summarize_tool_output(text: str, mode: str, max_chars: int) -> str:
    clean = text.strip()
    if not clean or mode == "none":
        return ""
    if mode == "full":
        return clean
    if mode == "summary":
        first = clean.splitlines()[0][:300]
        return f"[tool output omitted: {len(clean):,} characters; first line: {first!r}]"
    if mode == "tail":
        if len(clean) <= max_chars:
            return clean
        return f"[tool output truncated to final {max_chars:,} characters from {len(clean):,} total]\n\n{clean[-max_chars:]}"
    raise ValueError(f"Unknown tool output mode: {mode}")


def guess_lang(text: str, fallback: str = "text") -> str:
    sample = text.lstrip()
    if not sample:
        return fallback
    if sample.startswith("{") or sample.startswith("["):
        return "json"
    if sample.startswith("diff --git") or sample.startswith("@@ "):
        return "diff"
    if sample.startswith("import ") or "export function " in sample or "const " in sample:
        return "ts"
    if "$ErrorActionPreference" in sample or "Get-Content " in sample or "npm run " in sample or sample.startswith("$ "):
        return "ps1"
    return fallback


def max_backtick_run(text: str) -> int:
    runs = [len(m.group(0)) for m in re.finditer(r"`+", text)]
    return max(runs) if runs else 0


def fenced_block(text: str, lang: str = "", ticks: int = 3) -> str:
    body = text.rstrip()
    fence_len = max(ticks, max_backtick_run(body) + 1)
    fence = "`" * fence_len
    return f"{fence}{lang}\n{body}\n{fence}"


def first_line(text: str, limit: int = 220) -> str:
    line = re.sub(r"\s+", " ", text.strip().splitlines()[0] if text.strip() else "")
    return line if len(line) <= limit else line[: limit - 1] + "…"

def first_command_line(command: str, limit: int = 220) -> str:
    lines = [ln.strip() for ln in command.strip().splitlines() if ln.strip()]
    if len(lines) > 1 and re.fullmatch(r"\$?ErrorActionPreference\s*=\s*['\"]Stop['\"]", lines[0], flags=re.IGNORECASE):
        line = lines[1]
    else:
        line = lines[0] if lines else ""
    line = re.sub(r"\s+", " ", line)
    return line if len(line) <= limit else line[: limit - 1] + "…"



def parse_apply_patch(command: str) -> list[dict[str, Any]]:
    """Parse an apply_patch payload into per-file edited/created/deleted blocks."""
    if "*** Begin Patch" not in command or "*** End Patch" not in command:
        return []

    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_path: str | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            files.append(current)
            current = None

    for raw in command.splitlines():
        line = raw.rstrip("\n")
        if line.strip() in {"*** Begin Patch", "*** End Patch"}:
            continue

        m = re.match(r"\*\*\* (Add|Update|Delete) File:\s+(.+?)\s*$", line)
        if m:
            flush()
            op = m.group(1).lower()
            current_path = m.group(2).strip()
            current = {"op": op, "path": current_path, "lines": [], "added": 0, "removed": 0}
            continue

        if current is None:
            continue

        current["lines"].append(line)
        if line.startswith("+") and not line.startswith("+++"):
            current["added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["removed"] += 1

    flush()
    return files


def patch_lang_for_path(path: str, fallback: str = "diff") -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".ts": "ts",
        ".tsx": "tsx",
        ".js": "js",
        ".jsx": "jsx",
        ".mjs": "js",
        ".cjs": "js",
        ".json": "json",
        ".md": "md",
        ".css": "css",
        ".html": "html",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".ps1": "ps1",
        ".py": "python",
        ".sh": "bash",
    }.get(suffix, fallback)


def render_patch_file_block(file_info: dict[str, Any]) -> str:
    op = str(file_info.get("op") or "update")
    path = str(file_info.get("path") or "unknown")
    added = int(file_info.get("added") or 0)
    removed = int(file_info.get("removed") or 0)
    lines = [str(x) for x in file_info.get("lines") or []]

    if op == "add":
        label = "Created file"
        # Reconstruct added file contents without patch '+' markers when possible.
        content_lines = [ln[1:] for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
        body = "\n".join(content_lines).rstrip()
        lang = patch_lang_for_path(path, "text")
    elif op == "delete":
        label = "Deleted file"
        content_lines = [ln[1:] for ln in lines if ln.startswith("-") and not ln.startswith("---")]
        body = "\n".join(content_lines).rstrip()
        lang = patch_lang_for_path(path, "text")
    else:
        label = "Edited file"
        body = "\n".join(lines).rstrip()
        lang = "diff"

    parts = [label, path, f"+{added}", f"-{removed}"]
    if body:
        parts.append(fenced_block(body, lang, 3))
    return "\n".join(parts)


def render_apply_patch_record(command: str, output: str, meta: dict[str, Any]) -> str:
    files = parse_apply_patch(command)
    if not files:
        return ""

    created = sum(1 for f in files if f.get("op") == "add")
    edited = sum(1 for f in files if f.get("op") == "update")
    deleted = sum(1 for f in files if f.get("op") == "delete")

    summary_bits: list[str] = []
    if created:
        summary_bits.append(f"Created {created} file" + ("" if created == 1 else "s"))
    if edited:
        summary_bits.append(f"Edited {edited} file" + ("" if edited == 1 else "s"))
    if deleted:
        summary_bits.append(f"Deleted {deleted} file" + ("" if deleted == 1 else "s"))

    parts: list[str] = [", ".join(summary_bits) if summary_bits else "Applied patch"]
    parts.extend(render_patch_file_block(f) for f in files)

    # Preserve meaningful apply_patch tool output, but avoid duplicating the raw
    # patch or noisy success JSON. The GUI usually shows the edited-file cards
    # plus Success.
    cleaned_output = (output or "").strip()
    if cleaned_output and not cleaned_output.lower().startswith("success. updated the following files"):
        parts.append(fenced_block(cleaned_output, guess_lang(cleaned_output, "text"), 3))

    exit_code = meta.get("exit_code", meta.get("exitCode", meta.get("code")))
    if exit_code == 0 or str(exit_code) == "0" or cleaned_output.lower().startswith("success"):
        parts.append("Success")

    return "\n\n".join(p for p in parts if p).strip()


def render_action_record(
    *,
    title: str,
    command: str,
    output: str,
    meta: dict[str, Any],
    source_type: str,
    timestamp: str,
    seq: int,
) -> Record | None:
    title_l = (title or "").lower()
    cmd_key = command_key(command) if command else ""

    if command and ("*** Begin Patch" in command and "*** End Patch" in command):
        patch_text = render_apply_patch_record(command, output, meta)
        if patch_text:
            out_key = hashlib.sha256(patch_text.encode("utf-8", errors="ignore")).hexdigest()
            return Record("action", "tool", patch_text, timestamp, source_type or title, seq, cmd_key, out_key)

    # Drop tool lifecycle placeholders that carry neither a command nor output.
    # The GUI direct-copy transcript does not include empty transport markers.
    if not command and not output:
        return None

    chunks: list[str] = []

    if command:
        chunks.append(f"Ran {first_command_line(command)}")
        chunks.append(fenced_block(f"$ {command}", "ps1", 3))

    if output:
        # GUI direct-copy output records usually do not show labels such as
        # ``Action mcp_tool_call_end`` or ``Action write_stdin``; the output
        # itself is the substantive content.
        chunks.append(fenced_block(output, guess_lang(output, "text"), 3))

    exit_code = meta.get("exit_code")
    if exit_code is None:
        exit_code = meta.get("exitCode")
    if exit_code is None:
        exit_code = meta.get("code")

    if exit_code == 0 or str(exit_code) == "0":
        # GUI-style direct copies show "Success"; keep it concise and searchable.
        if output or command:
            chunks.append("Success")
    elif exit_code not in (None, ""):
        chunks.append(f"Exit code {exit_code}")

    text = "\n\n".join(chunks).strip()
    if not text:
        return None

    out_key = hashlib.sha256(output.encode("utf-8", errors="ignore")).hexdigest() if output else ""
    return Record("action", "tool", text, timestamp, source_type, seq, cmd_key, out_key)


def extract_action_record(
    obj: dict[str, Any],
    *,
    timestamp: str,
    source_type: str,
    seq: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
    tool_outputs: str,
    max_tool_chars: int,
) -> Record | None:
    title = as_string(obj.get("title") or obj.get("name") or obj.get("type") or source_type or "action")
    command = extract_command(obj)
    output, meta = extract_output_and_meta(obj, strip=strip, max_line_chars=max_line_chars, max_repeated_lines=max_repeated_lines)
    output = summarize_tool_output(output, tool_outputs, max_tool_chars)

    return render_action_record(title=title, command=command, output=output, meta=meta, source_type=source_type, timestamp=timestamp, seq=seq)


def get_timestamp(obj: dict[str, Any]) -> str:
    for key in ("timestamp", "time", "created_at", "createdAt"):
        value = obj.get(key)
        if value:
            return str(value)
    return ""


def event_type_blob(obj: dict[str, Any]) -> tuple[str, dict[str, Any], str, dict[str, Any], str]:
    top_type = str(obj.get("type") or "")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    return top_type, payload, payload_type, item, item_type


def classify_record(
    obj: dict[str, Any],
    *,
    seq: int,
    include_actions: bool,
    tool_outputs: str,
    max_tool_chars: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> Record | None:
    timestamp = get_timestamp(obj)
    top_type, payload, payload_type, item, item_type = event_type_blob(obj)
    type_blob = " ".join([top_type, payload_type, item_type]).lower()

    if any(h in type_blob for h in NOISY_TYPE_HINTS):
        return None

    # Response item messages.
    if item:
        item_role = str(item.get("role") or "")
        if (item_type == "message" or item_type in MESSAGE_TYPES) and item_role in {"user", "assistant", "system"}:
            text = extract_text(
                item.get("content") or item.get("message") or item.get("text"),
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
            )
            if text:
                return Record("message", item_role, text, timestamp, item_type, seq)

        if include_actions and any(h in item_type.lower() for h in ACTION_HINTS):
            return extract_action_record(
                item,
                timestamp=timestamp,
                source_type=item_type,
                seq=seq,
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
                tool_outputs=tool_outputs,
                max_tool_chars=max_tool_chars,
            )

    # Codex app/CLI message shapes.
    if payload_type in {"user_message", "agent_message", "assistant_message"}:
        role = "user" if payload_type == "user_message" else "assistant"
        text = extract_text(
            payload.get("message") or payload.get("text") or payload.get("content"),
            strip=strip,
            max_line_chars=max_line_chars,
            max_repeated_lines=max_repeated_lines,
        )
        if text:
            return Record("message", role, text, timestamp, payload_type, seq)

    # Generic assistant messages sometimes live directly on payload.
    role = str(payload.get("role") or obj.get("role") or "")
    if role in {"user", "assistant", "system"} and any(k in payload for k in ("text", "content", "message")):
        text = extract_text(
            payload.get("message") or payload.get("text") or payload.get("content"),
            strip=strip,
            max_line_chars=max_line_chars,
            max_repeated_lines=max_repeated_lines,
        )
        if text:
            return Record("message", role, text, timestamp, payload_type or top_type, seq)

    if include_actions and any(h in type_blob for h in ACTION_HINTS):
        action_obj = payload if payload else obj
        rec = extract_action_record(
            action_obj,
            timestamp=timestamp,
            source_type=payload_type or top_type,
            seq=seq,
            strip=strip,
            max_line_chars=max_line_chars,
            max_repeated_lines=max_repeated_lines,
            tool_outputs=tool_outputs,
            max_tool_chars=max_tool_chars,
        )
        if rec:
            return rec

    return None


def is_command_only_action(rec: Record) -> bool:
    return rec.kind == "action" and bool(rec.command_key) and not rec.output_key


def is_output_only_action(rec: Record) -> bool:
    return rec.kind == "action" and bool(rec.output_key) and not rec.command_key


def merge_command_outputs(records: list[Record]) -> list[Record]:
    """Merge command-only action cards with following output-only cards.

    Codex JSONL often stores tool calls and their outputs as separate records,
    while the GUI displays them as one user-visible command/output unit. FIFO
    merging preserves full output lines while removing duplicated transport
    cards.
    """
    merged: list[Record] = []
    pending: list[Record] = []

    def flush_pending() -> None:
        if pending:
            merged.extend(pending)
            pending.clear()

    for rec in records:
        if rec.kind != "action":
            flush_pending()
            merged.append(rec)
            continue

        if is_command_only_action(rec):
            pending.append(rec)
            continue

        if is_output_only_action(rec) and pending:
            cmd = pending.pop(0)
            text = "\n\n".join(part for part in (cmd.text.rstrip(), rec.text.rstrip()) if part).strip()
            merged.append(
                Record(
                    "action",
                    "tool",
                    text,
                    cmd.timestamp or rec.timestamp,
                    cmd.source_type or rec.source_type,
                    cmd.seq or rec.seq,
                    cmd.command_key,
                    rec.output_key,
                )
            )
            continue

        flush_pending()
        merged.append(rec)

    flush_pending()
    return merged


def action_summary_for_block(block: list[Record]) -> str:
    command_count = sum(1 for r in block if r.kind == "action" and (r.command_key or r.text.startswith("Ran ")))
    created = sum(len(re.findall(r"(?m)^Created file$", r.text)) for r in block)
    edited = sum(len(re.findall(r"(?m)^Edited file$", r.text)) for r in block)
    deleted = sum(len(re.findall(r"(?m)^Deleted file$", r.text)) for r in block)

    bits: list[str] = []
    if created:
        bits.append(f"Created {created} file" + ("" if created == 1 else "s"))
    if edited:
        bits.append(f"Edited {edited} file" + ("" if edited == 1 else "s"))
    if deleted:
        bits.append(f"Deleted {deleted} file" + ("" if deleted == 1 else "s"))
    if command_count:
        bits.append(f"ran {command_count} command" + ("" if command_count == 1 else "s"))

    if not bits:
        return ""
    if len(bits) == 1 and bits[0].startswith("ran "):
        return bits[0][:1].upper() + bits[0][1:]
    return ", ".join(bits)


def insert_action_group_summaries(records: list[Record]) -> list[Record]:
    """Add Codex-GUI-style rollup lines before contiguous action blocks."""
    out: list[Record] = []
    i = 0
    while i < len(records):
        rec = records[i]
        if rec.kind != "action":
            out.append(rec)
            i += 1
            continue

        j = i
        block: list[Record] = []
        while j < len(records) and records[j].kind == "action":
            block.append(records[j])
            j += 1

        summary = action_summary_for_block(block)
        if summary:
            out.append(Record("note", "system", summary, block[0].timestamp, "summary", block[0].seq))
        out.extend(block)
        i = j

    return out


def post_process_records(records: list[Record]) -> list[Record]:
    """Remove exporter-induced duplicates, merge split command/output records,
    and add GUI-like action summaries while preserving chronological order."""
    processed: list[Record] = []
    recent_commands: list[str] = []
    recent_outputs: list[str] = []

    for rec in records:
        if rec.kind == "action" and rec.command_key:
            if rec.command_key in recent_commands and not rec.output_key:
                continue
            recent_commands.append(rec.command_key)
            recent_commands = recent_commands[-12:]

        if rec.kind == "action" and rec.output_key:
            # Dedupe immediately repeated tool-output JSON echoes.
            if rec.output_key in recent_outputs[-3:]:
                continue
            recent_outputs.append(rec.output_key)
            recent_outputs = recent_outputs[-12:]

        # Remove empty/noisy apply_patch/action cards if any escaped extraction.
        if rec.kind == "action" and rec.text.strip() in {"Action `apply_patch`", "Action `patch_apply_end`", "Action `tool`"}:
            continue

        # Codex apply_patch emits a separate "Success. Updated the following files"
        # output record after the meaningful edited-file cards. The GUI direct-copy
        # view surfaces the file cards and success state, not this duplicate list.
        if rec.kind == "action" and not rec.command_key and "Success. Updated the following files:" in rec.text:
            continue

        processed.append(rec)

    processed = merge_command_outputs(processed)
    processed = insert_action_group_summaries(processed)
    return processed



def walk_json_values(value: Any, *, depth: int = 0) -> list[Any]:
    """Small bounded JSON walker used only for metadata discovery."""
    if depth > 10:
        return []
    out = [value]
    if isinstance(value, dict):
        for v in value.values():
            out.extend(walk_json_values(v, depth=depth + 1))
    elif isinstance(value, list):
        for v in value:
            out.extend(walk_json_values(v, depth=depth + 1))
    return out


def add_unique_meta(meta: dict[str, Any], key: str, value: str) -> None:
    value = str(value).strip()
    if not value:
        return
    bucket = meta.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def update_session_metadata_from_event(meta: dict[str, Any], obj: dict[str, Any]) -> None:
    """Best-effort extraction of models/date/title-like metadata from raw JSONL events.

    Codex JSONL schemas have changed across app/CLI builds. This intentionally avoids
    assuming a single schema and instead records stable, low-risk metadata fields
    when they appear anywhere in an event.
    """
    for candidate in walk_json_values(obj):
        if not isinstance(candidate, dict):
            continue

        for k, v in candidate.items():
            key = str(k).lower()
            if v is None:
                continue

            if key in {"model", "model_id", "model_slug", "selected_model", "active_model"}:
                if isinstance(v, str):
                    add_unique_meta(meta, "_models_seen", v)

            if key in {"effort", "reasoning_effort", "reasoning"}:
                if isinstance(v, str):
                    add_unique_meta(meta, "_reasoning_efforts_seen", v)
                elif isinstance(v, dict):
                    effort = v.get("effort") or v.get("level")
                    if isinstance(effort, str):
                        add_unique_meta(meta, "_reasoning_efforts_seen", effort)

            if key in {"title", "thread_title", "name"} and isinstance(v, str) and v.strip():
                meta.setdefault("_candidate_titles", [])
                if v not in meta["_candidate_titles"]:
                    meta["_candidate_titles"].append(v)

            if key in {"created_at", "createdat", "create_time", "created"} and isinstance(v, str):
                add_unique_meta(meta, "_created_candidates", v)

            if key in {"updated_at", "updatedat", "modified_at", "last_updated", "timestamp", "time"} and isinstance(v, str):
                add_unique_meta(meta, "_updated_candidates", v)


def approx_token_count(text: str) -> int:
    """Dependency-free approximate token count for filenames/metadata.

    If tiktoken is installed, use cl100k_base; otherwise use a conservative regex
    estimate. The value is intentionally labelled approximate.
    """
    if not text:
        return 0
    try:  # optional; keep this tool dependency-free by default
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Split words, numbers, punctuation, and non-space symbols. This tracks
        # token count better than chars/4 for command-heavy technical transcripts.
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def count_lines(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def yaml_quote(value: Any) -> str:
    s = "" if value is None else str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_list(values: list[str], indent: str = "  ") -> list[str]:
    if not values:
        return [indent + "- " + yaml_quote("unknown")]
    return [indent + "- " + yaml_quote(v) for v in values]


def timestamp_range(records: list[Record]) -> tuple[str, str]:
    stamps = [r.timestamp for r in records if r.timestamp]
    return (stamps[0], stamps[-1]) if stamps else ("", "")


def record_line_count(records: list[Record]) -> int:
    return sum(count_lines(r.text) for r in records if r.text)


def record_token_count(records: list[Record]) -> int:
    return sum(approx_token_count(r.text) for r in records if r.text)


def split_prompt_response_blocks(records: list[Record]) -> list[dict[str, Any]]:
    """Return blocks keyed by each user prompt and its following response records."""
    user_positions = [i for i, r in enumerate(records) if r.kind == "message" and r.role == "user"]
    blocks: list[dict[str, Any]] = []
    for idx, start in enumerate(user_positions, 1):
        end = user_positions[idx] if idx < len(user_positions) else len(records)
        prompt = records[start]
        response_records = [r for r in records[start + 1 : end] if r.role != "user"]
        blocks.append({"index": idx, "prompt": prompt, "response": response_records, "start": start, "end": end})
    return blocks


def count_file_cards(records: list[Record], label: str) -> int:
    pattern = re.compile(rf"(?m)^{re.escape(label)}$")
    return sum(len(pattern.findall(r.text)) for r in records if r.text)


def collect_models(records: list[Record], meta: dict[str, Any]) -> list[str]:
    models: list[str] = []
    for value in meta.get("_models_seen", []) or []:
        if isinstance(value, str) and value.strip() and value not in models:
            models.append(value.strip())

    # Very cautious fallback: only harvest model-looking names from short lines
    # that explicitly mention model/using/running, not arbitrary app content.
    model_re = re.compile(r"\b(?:gpt-\d(?:[.\w-]*codex)?|gpt-\d[.\w-]*|codex-mini-latest|claude(?:\s+opus|\s+sonnet)?[ \w.-]*|gemini[ \w.-]*)\b", re.I)
    for rec in records:
        for line in rec.text.splitlines()[:8]:
            if not re.search(r"\b(model|using|running|reasoning effort)\b", line, re.I):
                continue
            for match in model_re.findall(line):
                cleaned = re.sub(r"\s+", " ", match).strip(" .,:;`")
                if cleaned and cleaned not in models:
                    models.append(cleaned)
    return models


def export_summary(records: list[Record], meta: dict[str, Any]) -> dict[str, Any]:
    blocks = split_prompt_response_blocks(records)
    created, updated = timestamp_range(records)
    assistant_messages = [r for r in records if r.kind == "message" and r.role == "assistant"]
    user_messages = [r for r in records if r.kind == "message" and r.role == "user"]
    action_records = [r for r in records if r.kind == "action"]
    note_records = [r for r in records if r.kind == "note"]
    return {
        "created_at": created,
        "updated_at": updated,
        "prompt_count": len(user_messages),
        "response_count": len(blocks),
        "assistant_message_count": len(assistant_messages),
        "action_record_count": len(action_records),
        "note_record_count": len(note_records),
        "created_file_cards": count_file_cards(records, "Created file"),
        "edited_file_cards": count_file_cards(records, "Edited file"),
        "deleted_file_cards": count_file_cards(records, "Deleted file"),
        "models": collect_models(records, meta),
        "record_count": len(records),
        "source_event_count": meta.get("_source_event_count", 0),
        "parse_error_count": meta.get("_parse_error_count", 0),
    }


def markdown_table_row(values: list[Any]) -> str:
    def cell(v: Any) -> str:
        s = "" if v is None else str(v)
        s = s.replace("\n", " ").replace("|", "\\|")
        return s
    return "| " + " | ".join(cell(v) for v in values) + " |"


def turn_preview(text: str, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", text.strip())
    return (s[: limit - 1] + "…") if len(s) > limit else s


def render_thread_frontmatter(
    *,
    title: str,
    session_id: str | None,
    source: Path,
    source_sha: str,
    mode: str,
    summary: dict[str, Any],
    total_lines_placeholder: str = "__CODEX_EXPORT_TOTAL_LINES__",
    total_tokens_placeholder: str = "__CODEX_EXPORT_APPROX_TOKENS__",
) -> str:
    lines: list[str] = [
        "---",
        f"title: {yaml_quote(title)}",
        f"session_id: {yaml_quote(session_id or '')}",
        f"source_jsonl: {yaml_quote(str(source))}",
        f"source_sha256: {yaml_quote(source_sha)}",
        f"exported_at: {yaml_quote(datetime.now().isoformat(timespec='seconds'))}",
        f"thread_created_at: {yaml_quote(summary.get('created_at', ''))}",
        f"thread_updated_at: {yaml_quote(summary.get('updated_at', ''))}",
        f"mode: {yaml_quote(mode)}",
        "models_used:",
        *yaml_list(summary.get("models", [])),
        f"prompt_count: {summary.get('prompt_count', 0)}",
        f"response_count: {summary.get('response_count', 0)}",
        f"assistant_message_count: {summary.get('assistant_message_count', 0)}",
        f"action_record_count: {summary.get('action_record_count', 0)}",
        f"created_file_cards: {summary.get('created_file_cards', 0)}",
        f"edited_file_cards: {summary.get('edited_file_cards', 0)}",
        f"deleted_file_cards: {summary.get('deleted_file_cards', 0)}",
        f"source_event_count: {summary.get('source_event_count', 0)}",
        f"parse_error_count: {summary.get('parse_error_count', 0)}",
        f"total_lines: {total_lines_placeholder}",
        f"approx_tokens: {total_tokens_placeholder}",
        "token_count_method: " + yaml_quote("approximate; tiktoken cl100k_base if installed, otherwise dependency-free regex estimate"),
        "---",
        "",
    ]
    return "\n".join(lines)


def render_export_map(records: list[Record], meta: dict[str, Any], title: str) -> str:
    summary = export_summary(records, meta)
    blocks = split_prompt_response_blocks(records)

    parts: list[str] = [
        "## Thread export map",
        "",
        "| Metric | Value |",
        "|---|---:|",
        markdown_table_row(["Thread title", title]),
        markdown_table_row(["Thread created", summary.get("created_at", "")]),
        markdown_table_row(["Thread updated", summary.get("updated_at", "")]),
        markdown_table_row(["Prompts", summary.get("prompt_count", 0)]),
        markdown_table_row(["Responses", summary.get("response_count", 0)]),
        markdown_table_row(["Assistant message records", summary.get("assistant_message_count", 0)]),
        markdown_table_row(["Action records", summary.get("action_record_count", 0)]),
        markdown_table_row(["Edited file cards", summary.get("edited_file_cards", 0)]),
        markdown_table_row(["Created file cards", summary.get("created_file_cards", 0)]),
        markdown_table_row(["Deleted file cards", summary.get("deleted_file_cards", 0)]),
        markdown_table_row(["Models detected", ", ".join(summary.get("models", [])) or "unknown"]),
        "",
        "### Prompt/response index",
        "",
        "| # | Prompt timestamp | Response updated | Prompt lines | Prompt tokens | Response records | Response lines | Response tokens | Actions | Edited | Created | Preview |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    for block in blocks:
        prompt: Record = block["prompt"]
        response: list[Record] = block["response"]
        last_ts = next((r.timestamp for r in reversed(response) if r.timestamp), "")
        prompt_lines = count_lines(prompt.text)
        prompt_tokens = approx_token_count(prompt.text)
        response_lines = record_line_count(response)
        response_tokens = record_token_count(response)
        actions = sum(1 for r in response if r.kind == "action")
        edited = count_file_cards(response, "Edited file")
        created = count_file_cards(response, "Created file")
        preview = turn_preview(prompt.text)
        parts.append(
            markdown_table_row(
                [
                    block["index"],
                    prompt.timestamp,
                    last_ts,
                    prompt_lines,
                    prompt_tokens,
                    len(response),
                    response_lines,
                    response_tokens,
                    actions,
                    edited,
                    created,
                    preview,
                ]
            )
        )

    parts.append("")
    return "\n".join(parts)


def render_record_stream(records: list[Record]) -> str:
    return "\n\n".join(r.text.rstrip() for r in records if r.text.strip()).rstrip()


def render_thread_export(
    *,
    records: list[Record],
    title: str,
    source: Path,
    mode: str,
    meta: dict[str, Any],
    session_id: str | None,
    include_frontmatter: bool = True,
    include_map: bool = True,
    fence_turns: bool = False,
) -> str:
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    summary = export_summary(records, meta)
    parts: list[str] = []

    if include_frontmatter:
        parts.append(
            render_thread_frontmatter(
                title=title,
                session_id=session_id,
                source=source,
                source_sha=source_sha,
                mode=mode,
                summary=summary,
            )
        )

    parts.extend([f"# {title}", ""])

    if include_map:
        parts.append(render_export_map(records, meta, title))

    parts.extend(["## Transcript", ""])
    blocks = split_prompt_response_blocks(records)

    for block in blocks:
        idx = int(block["index"])
        prompt: Record = block["prompt"]
        response: list[Record] = block["response"]

        prompt_lines = count_lines(prompt.text)
        prompt_tokens = approx_token_count(prompt.text)
        response_lines = record_line_count(response)
        response_tokens = record_token_count(response)
        response_start = next((r.timestamp for r in response if r.timestamp), "")
        response_end = next((r.timestamp for r in reversed(response) if r.timestamp), "")

        parts.extend(
            [
                f"## Turn {idx:03d}",
                "",
                f"### Prompt {idx:03d}",
                "",
                "| Field | Value |",
                "|---|---:|",
                markdown_table_row(["Role", prompt.role]),
                markdown_table_row(["Timestamp", prompt.timestamp]),
                markdown_table_row(["Lines", prompt_lines]),
                markdown_table_row(["Approx tokens", prompt_tokens]),
                "",
            ]
        )

        if fence_turns:
            parts.append(fenced_block(prompt.text.rstrip(), "md"))
        else:
            parts.append(prompt.text.rstrip())

        parts.extend(
            [
                "",
                f"### Response {idx:03d}",
                "",
                "| Field | Value |",
                "|---|---:|",
                markdown_table_row(["Started", response_start]),
                markdown_table_row(["Updated", response_end]),
                markdown_table_row(["Records", len(response)]),
                markdown_table_row(["Assistant messages", sum(1 for r in response if r.kind == "message" and r.role == "assistant")]),
                markdown_table_row(["Action records", sum(1 for r in response if r.kind == "action")]),
                markdown_table_row(["Edited file cards", count_file_cards(response, "Edited file")]),
                markdown_table_row(["Created file cards", count_file_cards(response, "Created file")]),
                markdown_table_row(["Lines", response_lines]),
                markdown_table_row(["Approx tokens", response_tokens]),
                "",
            ]
        )

        response_text = render_record_stream(response)
        if response_text:
            if fence_turns:
                parts.append(fenced_block(response_text, "md"))
            else:
                parts.append(response_text)

        parts.append("")

    if not blocks:
        parts.append("_No prompt/response blocks were extracted from this thread._\n")

    return "\n".join(parts).rstrip() + "\n"


def finalize_count_placeholders(text: str) -> str:
    # Two-pass replacement stabilizes counts after placeholder width changes.
    for _ in range(3):
        lines = count_lines(text)
        tokens = approx_token_count(text)
        new = text.replace("__CODEX_EXPORT_TOTAL_LINES__", str(lines)).replace("__CODEX_EXPORT_APPROX_TOKENS__", str(tokens))
        if new == text:
            return text
        text = new
    return text


def filename_count_suffix(text: str) -> str:
    return f"_{count_lines(text)}lines_{approx_token_count(text)}tokens"


def parse_session(
    path: Path,
    *,
    include_actions: bool,
    tool_outputs: str,
    max_tool_chars: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> tuple[list[Record], dict[str, Any]]:
    records: list[Record] = []
    meta: dict[str, Any] = {}
    last_key: tuple[str, str, str] | None = None

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for seq, line in enumerate(f, 1):
            if not line.strip():
                continue
            meta["_source_event_count"] = int(meta.get("_source_event_count", 0)) + 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                meta["_parse_error_count"] = int(meta.get("_parse_error_count", 0)) + 1
                continue

            update_session_metadata_from_event(meta, obj)
            payload = obj.get("payload")
            if obj.get("type") == "session_meta" and isinstance(payload, dict):
                meta.update(payload)

            rec = classify_record(
                obj,
                seq=seq,
                include_actions=include_actions,
                tool_outputs=tool_outputs,
                max_tool_chars=max_tool_chars,
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
            )
            if not rec or not rec.text.strip():
                continue

            key = (rec.kind, rec.role, rec.text)
            if key == last_key:
                continue
            last_key = key
            records.append(rec)

    return post_process_records(records), meta


def read_session_index_title(session_id: str) -> str:
    home = codex_home()
    candidates = [home / "session_index.jsonl", home / "sessions" / "session_index.jsonl"]
    for index_path in candidates:
        if not index_path.exists():
            continue
        try:
            for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if session_id not in line:
                    continue
                obj = json.loads(line)
                for key in ("title", "name", "thread_title", "summary"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            continue
    return ""


def read_sqlite_title(session_id: str) -> str:
    home = codex_home()
    for db in (home / "state_5.sqlite", home / "state.sqlite"):
        if not db.exists():
            continue
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()]
            for table in tables:
                cols = [r[1] for r in cur.execute(f'pragma table_info("{table}")').fetchall()]
                title_cols = [c for c in cols if c.lower() in {"title", "name", "summary", "thread_title"}]
                id_cols = [c for c in cols if c.lower() in {"id", "thread_id", "session_id", "uuid"}]
                if not title_cols or not id_cols:
                    continue
                for id_col in id_cols:
                    for title_col in title_cols:
                        try:
                            row = cur.execute(
                                f'select "{title_col}" from "{table}" where "{id_col}" = ? limit 1',
                                (session_id,),
                            ).fetchone()
                            if row and isinstance(row[0], str) and row[0].strip():
                                return row[0].strip()
                        except Exception:
                            pass
        except Exception:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    return ""


def discover_title(records: list[Record], meta: dict[str, Any], override: str | None, session_id: str | None) -> str:
    if override:
        return override.strip()

    for key in ("title", "thread_title", "name", "summary"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    if session_id:
        for fn in (read_session_index_title, read_sqlite_title):
            title = fn(session_id)
            if title:
                return title

    for rec in records:
        if rec.kind == "message" and rec.role == "user" and rec.text.strip():
            return rec.text.strip().splitlines()[0][:80]

    return str(meta.get("id") or session_id or "codex_thread")


def response_block_records(records: list[Record], response_index: int, *, include_prompt: bool) -> list[Record]:
    blocks = split_prompt_response_blocks(records)
    if response_index < 1 or response_index > len(blocks):
        raise RuntimeError(f"--response must be 1..{len(blocks)}")
    block = blocks[response_index - 1]
    out: list[Record] = []
    if include_prompt:
        out.append(block["prompt"])
    out.extend(block["response"])
    return out


def choose_records(records: list[Record], args: argparse.Namespace) -> list[Record]:
    messages = [r for r in records if r.kind == "message"]
    assistants = [r for r in records if r.kind == "message" and r.role == "assistant"]
    actions = [r for r in records if r.kind == "action"]

    if args.mode == "thread":
        return records

    if args.mode == "response":
        if args.response is None:
            raise RuntimeError("--response N is required with --mode response")
        return response_block_records(records, args.response, include_prompt=False)

    if args.mode == "turn":
        if args.response is None:
            raise RuntimeError("--response N is required with --mode turn")
        return response_block_records(records, args.response, include_prompt=True)

    if args.mode == "last-response":
        last_user_idx = None
        for i, rec in enumerate(records):
            if rec.kind == "message" and rec.role == "user":
                last_user_idx = i
        selected = records[last_user_idx + 1 :] if last_user_idx is not None else records
        return [r for r in selected if r.role != "user"]

    if args.mode == "last-assistant":
        if not assistants:
            raise RuntimeError("No assistant message found in transcript.")
        return [assistants[-1]]

    if args.mode == "last-substantial":
        for rec in reversed(assistants):
            if len(rec.text.strip()) >= args.min_chars:
                return [rec]
        if assistants:
            return [assistants[-1]]
        raise RuntimeError("No assistant message found in transcript.")

    if args.mode == "message":
        if args.message is None:
            raise RuntimeError("--message N is required with --mode message")
        if args.message < 1 or args.message > len(messages):
            raise RuntimeError(f"--message must be 1..{len(messages)}")
        return [messages[args.message - 1]]

    if args.mode == "range":
        start = args.from_message or 1
        end = args.to_message or len(messages)
        if start < 1 or end > len(messages) or start > end:
            raise RuntimeError(f"Invalid message range. Valid range is 1..{len(messages)}")
        return messages[start - 1 : end]

    if args.mode == "chat":
        return messages

    if args.mode == "chat-actions":
        return records

    if args.mode == "actions":
        return actions

    raise RuntimeError(f"Unknown mode: {args.mode}")
def print_session_list() -> None:
    """List all local Codex session JSONLs as a Markdown table.

    Resolves titles cheaply via ``session_index.jsonl`` and the local sqlite
    state if available; otherwise leaves the title column blank. Sorted newest
    first by file mtime so the user can quickly grab the most recent session.
    """
    files = all_session_files()
    if not files:
        print(f"_No Codex session JSONLs found under {codex_home()}._")
        return

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    print("| # | session_id | size | modified | title |")
    print("|---:|---|---:|---|---|")
    for i, p in enumerate(files, 1):
        sid = parse_session_id_from_name(p)
        try:
            size = p.stat().st_size
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            size = 0
            mtime = ""
        title = ""
        if sid:
            title = read_session_index_title(sid) or read_sqlite_title(sid)
            title = title.replace("|", "\\|")
        if not sid:
            sid = p.name
        print(f"| {i} | {sid} | {size:,} | {mtime} | {title} |")


def print_message_list(records: list[Record]) -> None:
    messages = [r for r in records if r.kind == "message"]
    print("| # | role | timestamp | chars | preview |")
    print("|---:|---|---|---:|---|")
    for i, rec in enumerate(messages, 1):
        preview = rec.text.strip().replace("\n", " ")
        preview = re.sub(r"\s+", " ", preview)
        if len(preview) > 120:
            preview = preview[:117] + "..."
        preview = preview.replace("|", "\\|")
        print(f"| {i} | {rec.role} | {rec.timestamp or ''} | {len(rec.text):,} | {preview} |")


def print_response_list(records: list[Record]) -> None:
    blocks = split_prompt_response_blocks(records)
    print("| response | user message # | records | assistant chars | response lines | response tokens | action records | edited | created | first assistant preview |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for block in blocks:
        idx = block["index"]
        response = block["response"]
        a_chars = sum(len(r.text) for r in response if r.kind == "message" and r.role == "assistant")
        action_count = sum(1 for r in response if r.kind == "action")
        edited = count_file_cards(response, "Edited file")
        created = count_file_cards(response, "Created file")
        lines = record_line_count(response)
        tokens = record_token_count(response)
        preview = next((r.text.strip().splitlines()[0] for r in response if r.kind == "message" and r.role == "assistant"), "")
        preview = re.sub(r"\s+", " ", preview)
        if len(preview) > 100:
            preview = preview[:97] + "..."
        preview = preview.replace("|", "\\|")
        print(f"| {idx} | {idx} | {len(response)} | {a_chars:,} | {lines:,} | {tokens:,} | {action_count:,} | {edited:,} | {created:,} | {preview} |")

def render_record(rec: Record, index: int | None, plain: bool, ui_style: bool) -> str:
    if plain or ui_style:
        return rec.text.rstrip()
    heading_role = rec.role.capitalize()
    stamp = f" · {rec.timestamp}" if rec.timestamp else ""
    idx = f"{index}. " if index is not None else ""
    return f"## {idx}{heading_role}{stamp}\n\n{rec.text.rstrip()}"


def render_export(records: list[Record], title: str, source: Path, mode: str, plain: bool, ui_style: bool, include_meta: bool) -> str:
    if plain or ui_style:
        return "\n\n".join(r.text.rstrip() for r in records if r.text.strip()).rstrip() + "\n"

    parts: list[str] = [f"# {title}", ""]
    if include_meta:
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        parts.extend(
            [
                "## Export metadata",
                f"- Source JSONL: `{source}`",
                f"- Source SHA256: `{sha}`",
                f"- Mode: `{mode}`",
                f"- Exported: `{datetime.now().isoformat(timespec='seconds')}`",
                "",
            ]
        )
    for i, rec in enumerate(records, 1):
        parts.append(render_record(rec, i, plain=False, ui_style=False))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def wrap_markdown(text: str, title: str | None = None) -> str:
    body = text.rstrip()
    header = f"# {title}\n\n" if title else ""
    ticks = "`" * max(4, max_backtick_run(body) + 1)
    return f"{header}{ticks}md\n{body}\n{ticks}\n"


def set_windows_clipboard_unicode(text: str) -> None:
    """Set Windows clipboard as CF_UNICODETEXT with 64-bit-safe ctypes prototypes.

    The earlier implementation relied on ctypes default int return values. On
    64-bit Python that can truncate HGLOBAL/HANDLE values, which explains the
    user's observed mismatch where the file was clean but Get-Clipboard still
    returned stale/mojibake text. This version retries OpenClipboard, uses
    pointer-sized restypes/argtypes, and verifies the clipboard text after set.
    """
    import time

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool

    raw = (text + "\0").encode("utf-16-le")
    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
    if not h_global:
        raise OSError("GlobalAlloc failed")

    locked = kernel32.GlobalLock(h_global)
    if not locked:
        kernel32.GlobalFree(h_global)
        raise OSError("GlobalLock failed")
    try:
        ctypes.memmove(locked, raw, len(raw))
    finally:
        kernel32.GlobalUnlock(h_global)

    opened = False
    for _ in range(20):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)

    if not opened:
        kernel32.GlobalFree(h_global)
        raise OSError("OpenClipboard failed after retries")

    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(h_global)
            raise OSError("EmptyClipboard failed")
        if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
            kernel32.GlobalFree(h_global)
            raise OSError("SetClipboardData failed")
        h_global = None  # Clipboard owns the handle now.
    finally:
        user32.CloseClipboard()

    # Verify by reading back from CF_UNICODETEXT. This catches stale clipboard
    # ownership/content bugs immediately instead of silently leaving old text.
    verify_windows_clipboard_unicode(text)


def verify_windows_clipboard_unicode(expected: str) -> None:
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed during verification")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            raise OSError("GetClipboardData(CF_UNICODETEXT) failed during verification")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("GlobalLock clipboard handle failed during verification")
        try:
            actual = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()

    if actual != expected:
        raise OSError(
            f"Clipboard verification failed: expected {len(expected):,} chars, got {len(actual):,} chars"
        )

def copy_to_clipboard(text: str) -> None:
    if os.name == "nt":
        set_windows_clipboard_unicode(text)
    elif sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text, text=True, encoding="utf-8", check=True)
    else:
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                subprocess.run(cmd, input=text, text=True, encoding="utf-8", check=True)
                return
            except Exception:
                pass
        raise RuntimeError("No supported clipboard command found.")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="codex-export",
        description=(
            "Export local Codex app/CLI session JSONL transcripts to clean, "
            "GUI-fidelity Markdown with frontmatter, thread maps, per-turn "
            "metadata, edited-file patch cards, verified Unicode clipboard, "
            "and base64/blob stripping."
        ),
    )
    ap.add_argument("--version", action="version", version=f"codex-export {__version__}")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--session-id", help="Codex session/thread UUID (run /status in the Codex app to copy it)")
    source.add_argument("--jsonl", type=Path, help="Direct path to a rollout/session JSONL file")
    source.add_argument(
        "--latest-session",
        action="store_true",
        help="Use the most recently modified local session JSONL (no session-id needed)",
    )

    ap.add_argument(
        "--list-sessions",
        action="store_true",
        help="List all local Codex session JSONLs (id, size, mtime, title) and exit",
    )

    ap.add_argument(
        "--mode",
        choices=["thread", "response", "turn", "last-response", "last-assistant", "last-substantial", "message", "range", "chat", "chat-actions", "actions"],
        default="last-response",
    )
    ap.add_argument("--message", type=int, help="1-based message index for --mode message")
    ap.add_argument("--response", type=int, help="1-based prompt/response block index for --mode response or --mode turn")
    ap.add_argument("--from-message", type=int, help="1-based start message index for --mode range")
    ap.add_argument("--to-message", type=int, help="1-based end message index for --mode range")
    ap.add_argument("--min-chars", type=int, default=1000, help="Minimum assistant chars for --mode last-substantial")
    ap.add_argument("--list", action="store_true", help="List user/assistant messages and exit")
    ap.add_argument("--list-responses", action="store_true", help="List prompt-to-response blocks and action counts")

    ap.add_argument("--name", help="Override thread name used in title and filename")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--plain", action="store_true", help="Write only selected Markdown content, no metadata wrapper")
    ap.add_argument("--ui-style", action="store_true", default=True, help="Render selected turn like Codex UI transcript text")
    ap.add_argument("--no-ui-style", dest="ui_style", action="store_false")
    ap.add_argument("--wrap-md", action="store_true", help="Wrap entire export in an outer markdown code fence")
    ap.add_argument("--wrap-title", default="", help="Optional heading before the outer markdown fence, e.g. Last Response")
    ap.add_argument("--no-file", action="store_true", help="Do not write file; useful with --clipboard or --stdout")
    ap.add_argument("--no-filename-counts", action="store_true", help="Do not append total line/token counts to output filenames")
    ap.add_argument("--no-frontmatter", action="store_true", help="Disable YAML frontmatter for --mode thread")
    ap.add_argument("--no-map", action="store_true", help="Disable top thread export map for --mode thread")
    ap.add_argument("--fence-turns", action="store_true", help="Fence each prompt/response body in --mode thread for maximum boundary safety")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--clipboard", action="store_true")

    ap.add_argument("--redact", action="store_true", help="Apply secret-pattern redaction (OpenAI/GitHub tokens, bearer tokens, password=... lines)")
    ap.add_argument("--fix-mojibake", action="store_true", help=argparse.SUPPRESS)  # back-compat no-op; repair is on by default
    ap.add_argument("--keep-mojibake", action="store_true", help="Disable default repair of already-corrupted CP437/CP1252 mojibake such as IÆll -> I’ll")
    ap.add_argument("--keep-hogs", action="store_true", help="Disable default stripping of base64/data-URI/hex/overlong-line hogs")
    ap.add_argument("--max-line-chars", type=int, default=20_000)
    ap.add_argument("--max-repeated-lines", type=int, default=25)
    ap.add_argument("--tool-outputs", choices=["none", "summary", "tail", "full"], default="full")
    ap.add_argument("--max-tool-chars", type=int, default=20_000)
    ap.add_argument(
        "--json",
        dest="json_result",
        action="store_true",
        help="Print a single JSON line summarizing the export (file path, lines, tokens, models, source SHA) for skill/script chaining",
    )

    args = ap.parse_args()

    if args.list_sessions:
        print_session_list()
        return 0

    if args.latest_session:
        path = latest_session_file()
    elif args.jsonl:
        path = args.jsonl
    elif args.session_id:
        path = find_session_file(args.session_id)
    else:
        ap.error("one of --session-id, --jsonl, --latest-session, or --list-sessions is required")
    include_actions = args.mode in {"thread", "response", "turn", "last-response", "chat-actions", "actions"} or args.list_responses

    records, meta = parse_session(
        path=path,
        include_actions=include_actions,
        tool_outputs=args.tool_outputs,
        max_tool_chars=args.max_tool_chars,
        strip=not args.keep_hogs,
        max_line_chars=args.max_line_chars,
        max_repeated_lines=args.max_repeated_lines,
    )

    if args.list:
        print_message_list(records)
        return 0
    if args.list_responses:
        print_response_list(records)
        return 0

    selected = choose_records(records, args)
    title = discover_title(records, meta, args.name, args.session_id)
    if args.mode == "thread" and not args.plain:
        text = render_thread_export(
            records=selected,
            title=title,
            source=path,
            mode=args.mode,
            meta=meta,
            session_id=args.session_id,
            include_frontmatter=not args.no_frontmatter,
            include_map=not args.no_map,
            fence_turns=args.fence_turns,
        )
    elif args.mode == "turn" and not args.plain and not args.ui_style:
        # Structured single prompt+response block, with full per-turn metadata.
        text = render_thread_export(
            records=selected,
            title=title,
            source=path,
            mode=args.mode,
            meta=meta,
            session_id=args.session_id,
            include_frontmatter=not args.no_frontmatter,
            include_map=not args.no_map,
            fence_turns=args.fence_turns,
        )
    else:
        text = render_export(
            selected,
            title=title,
            source=path,
            mode=args.mode,
            plain=args.plain,
            ui_style=args.ui_style,
            include_meta=not args.plain,
        )

    if not getattr(args, "keep_mojibake", False):
        text = cp437_cp1252_mojibake_repair(text)
    text = redact(text, args.redact)
    text = strip_hogs(text, enabled=not args.keep_hogs, max_line_chars=args.max_line_chars, max_repeated_lines=args.max_repeated_lines)

    if args.wrap_md:
        text = wrap_markdown(text, title=args.wrap_title or None)

    text = finalize_count_placeholders(text)

    out_path = None
    if not args.no_file:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "" if args.no_filename_counts else filename_count_suffix(text)
        filename = f"codex_{safe_slug(title)}_{export_stamp()}{suffix}.md"
        out_path = args.out_dir / filename
        out_path.write_text(text, encoding="utf-8", newline="\n")

    if args.clipboard:
        copy_to_clipboard(text)

    if args.stdout:
        print(text, end="")

    if args.json_result:
        result = {
            "version": __version__,
            "file": str(out_path) if out_path else None,
            "source_jsonl": str(path),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "session_id": args.session_id or parse_session_id_from_name(path) or None,
            "title": title,
            "mode": args.mode,
            "lines": count_lines(text),
            "approx_tokens": approx_token_count(text),
            "redacted": bool(args.redact),
            "clipboard": bool(args.clipboard),
            "models": collect_models(records, meta),
        }
        print(json.dumps(result, ensure_ascii=False))
    elif out_path:
        print(out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
