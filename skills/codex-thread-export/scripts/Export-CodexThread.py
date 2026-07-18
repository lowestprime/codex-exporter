#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


__version__ = "0.2.0"

SCRIPT_DIR = Path(__file__).resolve().parent
FALLBACK_OUT_DIR = SCRIPT_DIR / "codex-thread-exports"
LOCAL_TZ_ENV = "CODEX_EXPORT_TZ"
DEFAULT_TOKEN_ENCODING = os.environ.get("CODEX_EXPORT_TOKEN_ENCODING", "cl100k_base")
DEFAULT_FILENAME_TEMPLATE = "codex_{title}_{mode}_{stamp}{session_short_part}{counts}.md"
DEFAULT_STABLE_FILENAME_TEMPLATE = "codex_{title}_{mode}{session_short_part}{counts}.md"
SOURCE_TRUNCATION_RE = re.compile(r"…(?P<count>[0-9,]+)\s+tokens truncated…", re.IGNORECASE)

_TOKEN_ENCODER: Any = None
_TOKEN_INFO: dict[str, Any] = {
    "method": "regex_estimate",
    "encoding": "none",
    "library": "builtin",
    "version": "",
    "exact_for_encoding": False,
    "error": "not configured",
}
_SOURCE_TRUNCATION_POLICY = "annotate"
_RUNTIME_AUDIT: dict[str, Any] = {}
_LARGE_TEXT_METRIC_CACHE: dict[int, tuple[str, int, int]] = {}


def reset_runtime_audit() -> None:
    global _RUNTIME_AUDIT, _LARGE_TEXT_METRIC_CACHE
    _LARGE_TEXT_METRIC_CACHE = {}
    _RUNTIME_AUDIT = {
        "cleaned": Counter(),
        "summarized": Counter(),
        "redacted": Counter(),
        "source_truncation_markers": 0,
        "source_truncation_tokens_reported": 0,
    }


def audit_increment(bucket: str, key: str, amount: int = 1) -> None:
    target = _RUNTIME_AUDIT.setdefault(bucket, Counter())
    if isinstance(target, Counter):
        target[key] += amount


reset_runtime_audit()


def configure_utf8_standard_streams() -> None:
    """Use deterministic UTF-8 for CLI output, including Windows pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, OSError, ValueError):
            pass


def config_path() -> Path:
    override = os.environ.get("CODEX_EXPORT_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "codex-exporter" / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeError(f"Invalid exporter config {path}: {exc}") from exc


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass


def configured_out_dir(config: dict[str, Any] | None = None) -> Path:
    config = config if config is not None else load_config()
    value = os.environ.get("CODEX_THREAD_EXPORT_DIR") or config.get("last_out_dir")
    return Path(value).expanduser() if value else FALLBACK_OUT_DIR


def choose_directory(initial: Path) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Native folder selection requires the standard-library tkinter module.") from exc
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        selected = filedialog.askdirectory(
            parent=root,
            title="Choose Codex export folder",
            initialdir=str(initial if initial.exists() else Path.home()),
            mustexist=False,
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def choose_save_file(initial_dir: Path, initial_name: str) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Native Save As selection requires the standard-library tkinter module.") from exc
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        selected = filedialog.asksaveasfilename(
            parent=root,
            title="Save Codex export as",
            initialdir=str(initial_dir if initial_dir.exists() else Path.home()),
            initialfile=initial_name,
            defaultextension=".md",
            filetypes=(("Markdown", "*.md"), ("All files", "*.*")),
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def open_in_file_manager(path: Path, *, select: bool = False) -> None:
    target = path.resolve()
    if sys.platform == "win32":
        args = ["explorer.exe"]
        if select and target.is_file():
            args.append(f"/select,{target}")
        else:
            args.append(str(target if target.is_dir() else target.parent))
        subprocess.Popen(args)
    elif sys.platform == "darwin":
        args = ["open"]
        if select and target.is_file():
            args.append("-R")
        args.append(str(target))
        subprocess.Popen(args)
    else:
        subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])


def configure_token_counter(*, encoding: str, mode: str = "auto", require: bool = False) -> dict[str, Any]:
    global _TOKEN_ENCODER, _TOKEN_INFO
    _TOKEN_ENCODER = None
    if mode == "regex":
        _TOKEN_INFO = {
            "method": "regex_estimate",
            "encoding": "none",
            "library": "builtin",
            "version": "",
            "exact_for_encoding": False,
            "special_token_policy": "ordinary_text",
            "error": "forced by --tokenizer regex",
        }
        return dict(_TOKEN_INFO)
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding(encoding)
        _TOKEN_ENCODER = enc
        try:
            version = importlib_metadata.version("tiktoken")
        except Exception:
            version = "unknown"
        _TOKEN_INFO = {
            "method": "tiktoken",
            "encoding": enc.name,
            "library": "tiktoken",
            "version": version,
            "exact_for_encoding": True,
            "special_token_policy": "ordinary_text; disallowed_special=()",
            "error": "",
        }
    except Exception as exc:
        _TOKEN_INFO = {
            "method": "regex_estimate",
            "encoding": "none",
            "requested_encoding": encoding,
            "library": "builtin",
            "version": "",
            "exact_for_encoding": False,
            "special_token_policy": "ordinary_text",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if require or mode == "tiktoken":
            raise RuntimeError(
                f"tiktoken encoding {encoding!r} is required but unavailable in {sys.executable}: {exc}"
            ) from exc
    return dict(_TOKEN_INFO)


def token_counter_info() -> dict[str, Any]:
    return dict(_TOKEN_INFO)


def token_field_label() -> str:
    info = token_counter_info()
    return f"tokens ({info['encoding']})" if info.get("exact_for_encoding") else "estimated tokens (regex)"


def escape_invalid_json_string_backslashes(line: str) -> tuple[str, int]:
    """Escape only invalid backslashes occurring inside JSON strings.

    This repairs a known Codex rollout corruption class where command output embeds
    a Windows path such as ``X:\\project`` with a single JSON backslash. It does
    not guess missing braces, concatenate records, or otherwise alter valid JSON.
    """
    out: list[str] = []
    in_string = False
    repairs = 0
    i = 0
    simple = {'"', "\\", "/", "b", "f", "n", "r", "t"}
    while i < len(line):
        ch = line[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(line):
            out.append("\\\\")
            repairs += 1
            i += 1
            continue
        nxt = line[i + 1]
        if nxt in simple:
            out.extend((ch, nxt))
            i += 2
            continue
        if nxt == "u" and i + 5 < len(line) and re.fullmatch(r"[0-9A-Fa-f]{4}", line[i + 2 : i + 6]):
            out.extend(line[i : i + 6])
            i += 6
            continue
        out.append("\\\\")
        repairs += 1
        i += 1
    return "".join(out), repairs


def parse_jsonl_line(line: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        value = json.loads(line)
        return (value if isinstance(value, dict) else None), {"status": "exact", "repairs": 0}
    except json.JSONDecodeError as first:
        repaired, repairs = escape_invalid_json_string_backslashes(line)
        if repairs:
            try:
                value = json.loads(repaired)
                if isinstance(value, dict):
                    return value, {"status": "repaired_invalid_escape", "repairs": repairs, "original_error": str(first)}
            except json.JSONDecodeError as second:
                return None, {"status": "error", "repairs": repairs, "error": str(second), "original_error": str(first)}
        try:
            value = json.loads(line, strict=False)
            if isinstance(value, dict):
                return value, {"status": "repaired_control_character", "repairs": 1, "original_error": str(first)}
        except Exception as second:
            return None, {"status": "error", "repairs": 0, "error": str(second), "original_error": str(first)}
    except Exception as exc:
        return None, {"status": "error", "repairs": 0, "error": f"{type(exc).__name__}: {exc}"}


def process_source_truncation_markers(text: str) -> str:
    matches = list(SOURCE_TRUNCATION_RE.finditer(text))
    if not matches:
        return text
    _RUNTIME_AUDIT["source_truncation_markers"] = int(_RUNTIME_AUDIT.get("source_truncation_markers", 0)) + len(matches)
    total = sum(int(m.group("count").replace(",", "")) for m in matches)
    _RUNTIME_AUDIT["source_truncation_tokens_reported"] = int(_RUNTIME_AUDIT.get("source_truncation_tokens_reported", 0)) + total
    if _SOURCE_TRUNCATION_POLICY == "error":
        raise RuntimeError(f"Source JSONL contains {len(matches)} Codex runtime truncation marker(s), reporting {total:,} omitted tokens.")
    if _SOURCE_TRUNCATION_POLICY == "preserve":
        return text
    def repl(match: re.Match[str]) -> str:
        count = match.group("count")
        return f"[SOURCE TOOL OUTPUT TRUNCATED BY CODEX BEFORE EXPORT: {count} tokens omitted; not recoverable from this rollout record]"
    return SOURCE_TRUNCATION_RE.sub(repl, text)


# Build credential signatures from fragments so repository scanners do not
# mistake the detector definitions themselves for live credentials.
_OPENAI_KEY_PREFIX = "s" + "k-"
_GITHUB_PAT_PREFIX = "github" + "_pat_"
_GITHUB_CLASSIC_PREFIX = "g" + "h"

SECRET_PATTERNS = [
    (re.compile(re.escape(_OPENAI_KEY_PREFIX) + r"[A-Za-z0-9_\-]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(re.escape(_GITHUB_PAT_PREFIX) + r"[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(re.escape(_GITHUB_CLASSIC_PREFIX) + r"[pousr]_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
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
    repaired = 0
    for ch in text:
        code = ord(ch)
        if 0x80 <= code <= 0xFF:
            try:
                decoded = bytes([code]).decode("cp1252")
                out.append(decoded)
                if decoded != ch:
                    repaired += 1
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    if repaired:
        audit_increment("cleaned", "mojibake_codepoint_repair", repaired)
    return "".join(out)


def redact(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    for index, (pattern, replacement) in enumerate(SECRET_PATTERNS, 1):
        text, count = pattern.subn(replacement, text)
        if count:
            audit_increment("redacted", f"pattern_{index}", count)
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
    text, count = OSC_PATTERN.subn("", text)
    if count: audit_increment("cleaned", "osc_sequence", count)
    text, count = ANSI_PATTERN.subn("", text)
    if count: audit_increment("cleaned", "ansi_sequence", count)
    text, count = CONTROL_PATTERN.subn("", text)
    if count: audit_increment("cleaned", "control_character", count)
    text, count = re.subn(r"(?m)^0;[^\n]*(?:\n|$)", "", text)
    if count: audit_increment("cleaned", "terminal_title_residue", count)

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

    text, count = MARKDOWN_DATA_IMAGE_PATTERN.subn(repl_markdown_img, text)
    if count: audit_increment("cleaned", "markdown_data_image", count)
    text, count = HTML_DATA_SRC_PATTERN.subn(repl_html_src, text)
    if count: audit_increment("cleaned", "html_data_src", count)
    text, count = DATA_URI_PATTERN.subn(repl_data_uri, text)
    if count: audit_increment("cleaned", "data_uri", count)
    text, count = BASE64_BLOB_PATTERN.subn(repl_base64, text)
    if count: audit_increment("cleaned", "base64_candidate", count)
    text, count = HEX_BLOB_PATTERN.subn(lambda m: summarize_blob("hex blob", m.group(0)), text)
    if count: audit_increment("cleaned", "hex_blob", count)

    lines = text.splitlines()
    compacted: list[str] = []
    prev: str | None = None
    repeat_count = 0

    for line in lines:
        if len(line) > max_line_chars:
            audit_increment("cleaned", "overlong_line", 1)
            digest = hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()[:16]
            head = line[:1200].rstrip()
            tail = line[-1200:].lstrip()
            line = f"{head}\n[single overlong line truncated: {len(line):,} characters, sha256-prefix={digest}]\n{tail}"

        if line == prev:
            repeat_count += 1
            if repeat_count == max_repeated_lines + 1:
                audit_increment("cleaned", "repeated_line_run", 1)
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
        text = process_source_truncation_markers(text)
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
    text = process_source_truncation_markers(text)
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
        audit_increment("cleaned", "tool_transport_header_block", 1)
        return "\n".join(lines[output_idx:]).strip(), meta

    if saw_transport_header:
        audit_increment("cleaned", "tool_transport_header_block", 1)
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
    if not clean:
        return ""
    if mode == "none":
        audit_increment("summarized", "tool_output_omitted", 1)
        return ""
    if mode == "full":
        return clean
    if mode == "summary":
        audit_increment("summarized", "tool_output_summary", 1)
        first = clean.splitlines()[0][:300]
        return f"[tool output omitted: {len(clean):,} characters; first line: {first!r}]"
    if mode == "tail":
        if len(clean) <= max_chars:
            return clean
        audit_increment("summarized", "tool_output_tail_truncated", 1)
        return f"[EXPORTER TOOL OUTPUT TRUNCATED: kept final {max_chars:,} of {len(clean):,} characters]\n\n{clean[-max_chars:]}"
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


def event_key(obj: dict[str, Any]) -> str:
    top_type, _payload, payload_type, _item, item_type = event_type_blob(obj)
    return "/".join(part or "-" for part in (top_type, payload_type, item_type))


def extract_reasoning_summary(payload: dict[str, Any]) -> str:
    """Return only Codex's explicit summary surface, never encrypted/raw reasoning."""
    summary = payload.get("summary")
    if not summary:
        return ""
    return extract_text(
        summary,
        strip=True,
        max_line_chars=20_000,
        max_repeated_lines=25,
        json_aware=True,
    ).strip()


def known_ignored_reason(obj: dict[str, Any], include_reasoning_summaries: bool) -> str:
    top_type, _payload, payload_type, _item, item_type = event_type_blob(obj)
    ptype = payload_type or item_type
    if ptype == "token_count":
        return "telemetry_token_count"
    if ptype == "agent_reasoning":
        return "raw_reasoning_not_exported"
    if ptype == "reasoning":
        return "reasoning_summary_empty" if include_reasoning_summaries else "reasoning_summary_opt_in_disabled"
    if top_type in {"turn_context", "session_meta", "world_state"}:
        return "metadata_only"
    if top_type == "compacted" or ptype in {"context_compacted", "thread_rolled_back"}:
        return "context_history_event"
    if ptype in {"task_started", "task_complete", "turn_aborted", "thread_goal_updated", "thread_settings_applied"}:
        return "lifecycle_event"
    if ptype in {"web_search_call", "web_search_end", "tool_search_call", "tool_search_output", "mcp_tool_call_end", "view_image_tool_call"}:
        return "unsupported_tool_lifecycle_or_specialized_event"
    if top_type == "response_item" and ptype in {"message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
        return "known_response_item_not_rendered_or_duplicate"
    if top_type == "event_msg" and ptype == "error":
        return "runtime_error_event"
    return "unrecognized_schema"


def records_from_replacement_history(
    history: list[Any],
    *,
    timestamp: str,
    include_actions: bool,
    include_reasoning_summaries: bool,
    tool_outputs: str,
    max_tool_chars: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> list[Record]:
    reconstructed: list[Record] = []
    for offset, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        pseudo = {"timestamp": timestamp, "type": "response_item", "payload": {"type": "item", "item": item}}
        rec = classify_record(
            pseudo,
            seq=-(offset + 1),
            include_actions=include_actions,
            include_reasoning_summaries=include_reasoning_summaries,
            tool_outputs=tool_outputs,
            max_tool_chars=max_tool_chars,
            strip=strip,
            max_line_chars=max_line_chars,
            max_repeated_lines=max_repeated_lines,
        )
        if rec and rec.text.strip():
            reconstructed.append(rec)
    return post_process_records(reconstructed)


def apply_rollback_to_records(records: list[Record], num_turns: int) -> list[Record]:
    if num_turns <= 0:
        return records
    user_positions = [i for i, rec in enumerate(records) if rec.kind == "message" and rec.role == "user"]
    if not user_positions:
        return []
    cut = user_positions[-num_turns] if num_turns <= len(user_positions) else 0
    return records[:cut]


def classify_record(
    obj: dict[str, Any],
    *,
    seq: int,
    include_actions: bool,
    include_reasoning_summaries: bool = False,
    tool_outputs: str,
    max_tool_chars: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> Record | None:
    timestamp = get_timestamp(obj)
    top_type, payload, payload_type, item, item_type = event_type_blob(obj)
    type_blob = " ".join([top_type, payload_type, item_type]).lower()

    reasoning_payload = item if item_type == "reasoning" else payload
    if include_reasoning_summaries and (item_type == "reasoning" or payload_type == "reasoning"):
        summary = extract_reasoning_summary(reasoning_payload)
        if summary:
            return Record(
                "note",
                "assistant",
                "Reasoning summary (explicit Codex summary; opt-in)\n\n" + summary,
                timestamp,
                "reasoning_summary",
                seq,
            )

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
    """Extract only structural session/turn metadata, never arbitrary tool payloads."""
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    candidates: list[dict[str, Any]] = [obj, payload]
    for key in ("session", "metadata", "settings", "model_info", "collaboration_mode"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        for k, v in candidate.items():
            key = str(k).lower()
            if v is None:
                continue
            if key in {"model", "model_id", "model_slug", "selected_model", "active_model"} and isinstance(v, str):
                cleaned = v.strip()
                if cleaned and len(cleaned) <= 100 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", cleaned):
                    add_unique_meta(meta, "_models_seen", cleaned)
            elif key in {"effort", "reasoning_effort"} and isinstance(v, str):
                add_unique_meta(meta, "_reasoning_efforts_seen", v)
            elif key in {"title", "thread_title"} and isinstance(v, str) and v.strip():
                add_unique_meta(meta, "_candidate_titles", v)
            elif key in {"created_at", "createdat", "create_time", "created"} and isinstance(v, str):
                add_unique_meta(meta, "_created_candidates", v)
            elif key in {"updated_at", "updatedat", "modified_at", "last_updated", "timestamp", "time"} and isinstance(v, str):
                add_unique_meta(meta, "_updated_candidates", v)
            elif key in {"cwd", "working_directory", "workspace"} and isinstance(v, str) and v.strip():
                meta.setdefault("cwd", v.strip())


def _large_text_metrics(text: str) -> tuple[int, int] | None:
    if len(text) < 1_000_000:
        return None
    cached = _LARGE_TEXT_METRIC_CACHE.get(id(text))
    if cached is not None and cached[0] is text:
        return cached[1], cached[2]
    tokens = len(_TOKEN_ENCODER.encode(text, disallowed_special=())) if _TOKEN_ENCODER is not None else len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    lines = text.count("\n") + (0 if text.endswith(("\n", "\r")) else 1)
    if len(_LARGE_TEXT_METRIC_CACHE) >= 4:
        _LARGE_TEXT_METRIC_CACHE.clear()
    _LARGE_TEXT_METRIC_CACHE[id(text)] = (text, tokens, lines)
    return tokens, lines


def approx_token_count(text: str) -> int:
    """Count tokens with the configured counter.

    With tiktoken this is exact for the explicitly named encoding, not a claim
    about a model's complete chat/tool framing. The dependency-free fallback is
    deliberately and explicitly labelled a regex estimate.
    """
    if not text:
        return 0
    metrics = _large_text_metrics(text)
    if metrics is not None:
        return metrics[0]
    if _TOKEN_ENCODER is not None:
        return len(_TOKEN_ENCODER.encode(text, disallowed_special=()))
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def count_lines(text: str) -> int:
    if not text:
        return 0
    metrics = _large_text_metrics(text)
    if metrics is not None:
        return metrics[1]
    return text.count("\n") + (0 if text.endswith(("\n", "\r")) else 1)


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
    del records
    models: list[str] = []
    for value in meta.get("_models_seen", []) or []:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned not in models:
                models.append(cleaned)
    return models


def selected_source_truncation_summary(records: list[Record]) -> tuple[int, int]:
    """Count source-loss sentinels that survive into the selected record set."""
    count = 0
    tokens = 0
    annotated = re.compile(
        r"\[SOURCE TOOL OUTPUT TRUNCATED BY CODEX BEFORE EXPORT:\s*"
        r"(?P<count>[0-9][0-9,]*)\s+tokens omitted;[^\]]*\]",
        re.IGNORECASE,
    )
    for record in records:
        text = record.text or ""
        annotated_matches = list(annotated.finditer(text))
        count += len(annotated_matches)
        tokens += sum(int(match.group("count").replace(",", "")) for match in annotated_matches)
        # Under --source-truncation preserve, the original ellipsis marker remains.
        preserved_matches = list(SOURCE_TRUNCATION_RE.finditer(annotated.sub("", text)))
        count += len(preserved_matches)
        tokens += sum(int(match.group("count").replace(",", "")) for match in preserved_matches)
    return count, tokens


def export_summary(records: list[Record], meta: dict[str, Any]) -> dict[str, Any]:
    blocks = split_prompt_response_blocks(records)
    created, updated = timestamp_range(records)
    assistant_messages = [r for r in records if r.kind == "message" and r.role == "assistant"]
    user_messages = [r for r in records if r.kind == "message" and r.role == "user"]
    action_records = [r for r in records if r.kind == "action"]
    note_records = [r for r in records if r.kind == "note"]
    selected_truncation_count, selected_truncation_tokens = selected_source_truncation_summary(records)
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
        "repaired_json_line_count": meta.get("_repaired_json_line_count", 0),
        "reconstruction_mode": meta.get("_reconstruction_mode", "chronological_source_history"),
        "compactions_applied": meta.get("_compactions_applied", 0),
        "rollbacks_applied": meta.get("_rollbacks_applied", 0),
        "unknown_event_count": sum((meta.get("_unknown_event_types") or {}).values()),
        "ignored_event_count": sum((meta.get("_ignored_event_types") or {}).values()),
        "source_truncation_markers": int(meta.get("_raw_source_truncation_marker_count", 0)),
        "source_truncation_tokens_reported": int(meta.get("_raw_source_truncation_tokens_reported", 0)),
        "extracted_source_truncation_markers": int(_RUNTIME_AUDIT.get("source_truncation_markers", 0)),
        "extracted_source_truncation_tokens_reported": int(_RUNTIME_AUDIT.get("source_truncation_tokens_reported", 0)),
        "rendered_source_truncation_markers": selected_truncation_count,
        "rendered_source_truncation_tokens_reported": selected_truncation_tokens,
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
    total_tokens_placeholder: str = "__CODEX_EXPORT_TOKEN_COUNT__",
) -> str:
    token = token_counter_info()
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
        f"history_semantics: {yaml_quote(summary.get('reconstruction_mode', 'chronological_source_history'))}",
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
        f"repaired_json_line_count: {summary.get('repaired_json_line_count', 0)}",
        f"ignored_event_count: {summary.get('ignored_event_count', 0)}",
        f"unknown_event_count: {summary.get('unknown_event_count', 0)}",
        f"compactions_applied: {summary.get('compactions_applied', 0)}",
        f"rollbacks_applied: {summary.get('rollbacks_applied', 0)}",
        f"source_truncation_marker_count: {summary.get('source_truncation_markers', 0)}",
        f"source_truncation_tokens_reported: {summary.get('source_truncation_tokens_reported', 0)}",
        f"extracted_source_truncation_marker_count: {summary.get('extracted_source_truncation_markers', 0)}",
        f"extracted_source_truncation_tokens_reported: {summary.get('extracted_source_truncation_tokens_reported', 0)}",
        f"rendered_source_truncation_marker_count: {summary.get('rendered_source_truncation_markers', 0)}",
        f"rendered_source_truncation_tokens_reported: {summary.get('rendered_source_truncation_tokens_reported', 0)}",
        f"total_lines: {total_lines_placeholder}",
        f"token_count: {total_tokens_placeholder}",
        f"token_count_method: {yaml_quote(token.get('method', 'unknown'))}",
        f"token_encoding: {yaml_quote(token.get('encoding', 'none'))}",
        f"tokenizer_library: {yaml_quote(token.get('library', 'builtin'))}",
        f"tokenizer_version: {yaml_quote(token.get('version', ''))}",
        f"token_count_exact_for_encoding: {str(bool(token.get('exact_for_encoding'))).lower()}",
        f"token_special_text_policy: {yaml_quote(token.get('special_token_policy', 'ordinary_text'))}",
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
        markdown_table_row(["History semantics", summary.get("reconstruction_mode", "chronological_source_history")]),
        markdown_table_row(["JSON lines repaired", summary.get("repaired_json_line_count", 0)]),
        markdown_table_row(["Unrecovered parse errors", summary.get("parse_error_count", 0)]),
        markdown_table_row(["Ignored source events", summary.get("ignored_event_count", 0)]),
        markdown_table_row(["Unknown source events", summary.get("unknown_event_count", 0)]),
        markdown_table_row(["Raw source truncation markers", summary.get("source_truncation_markers", 0)]),
        markdown_table_row(["Rendered source truncation markers", summary.get("rendered_source_truncation_markers", 0)]),
        "",
        "### Prompt/response index",
        "",
        f"| # | Prompt timestamp | Response updated | Prompt lines | Prompt {token_field_label()} | Response records | Response lines | Response {token_field_label()} | Actions | Edited | Created | Preview |",
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
    first_user = next((i for i, rec in enumerate(records) if rec.kind == "message" and rec.role == "user"), len(records))
    preamble = [rec for rec in records[:first_user] if rec.text.strip()]
    if preamble:
        parts.extend(["### Reconstruction / preamble records", "", render_record_stream(preamble), ""])

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
                markdown_table_row([token_field_label().capitalize(), prompt_tokens]),
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
                markdown_table_row([token_field_label().capitalize(), response_tokens]),
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
    """Resolve line/token placeholders against the final rendered document.

    The original implementation replaced placeholders once and then counted a
    different string, which could make frontmatter and filename token totals
    disagree. Keeping the untouched template permits a true fixed-point update.
    """
    template = text
    lines = 0
    tokens = 0
    candidate = template
    for _ in range(12):
        candidate = (
            template.replace("__CODEX_EXPORT_TOTAL_LINES__", str(lines))
            .replace("__CODEX_EXPORT_TOKEN_COUNT__", str(tokens))
            .replace("__CODEX_EXPORT_APPROX_TOKENS__", str(tokens))
        )
        measured_lines = count_lines(candidate)
        measured_tokens = approx_token_count(candidate)
        if measured_lines == lines and measured_tokens == tokens:
            return candidate
        lines, tokens = measured_lines, measured_tokens
    return (
        template.replace("__CODEX_EXPORT_TOTAL_LINES__", str(lines))
        .replace("__CODEX_EXPORT_TOKEN_COUNT__", str(tokens))
        .replace("__CODEX_EXPORT_APPROX_TOKENS__", str(tokens))
    )


def filename_count_suffix(text: str) -> str:
    info = token_counter_info()
    tokens = approx_token_count(text)
    if info.get("exact_for_encoding"):
        enc = safe_slug(str(info.get("encoding") or "encoding"), max_len=30)
        return f"_{count_lines(text)}lines_{tokens}{enc}_tokens"
    return f"_{count_lines(text)}lines_approx{tokens}tokens"


def mode_descriptor(args: argparse.Namespace) -> str:
    mode = str(args.mode)
    if mode in {"response", "turn"} and getattr(args, "response", None):
        mode = f"{mode}-{int(args.response):04d}"
    elif mode == "message" and getattr(args, "message", None):
        mode = f"message-{int(args.message):04d}"
    elif mode == "range":
        mode = f"range-{int(args.from_message or 1):04d}-{int(args.to_message or 0):04d}"
    if getattr(args, "last_n_turns", None):
        mode += f"-last-{int(args.last_n_turns)}-turns"
    if getattr(args, "live_context", False):
        mode += "-reconstructed-live-context"
    return mode


def render_filename(
    template: str,
    *,
    title: str,
    mode: str,
    session_id: str,
    text: str,
    include_session_short_id: bool,
) -> str:
    info = token_counter_info()
    short = session_id.replace("-", "")[-8:] if session_id else ""
    values = {
        "title": safe_slug(title),
        "mode": safe_slug(mode),
        "stamp": export_stamp(),
        "session_id": safe_slug(session_id, max_len=80),
        "session_short": short,
        "session_short_part": f"_{short}" if include_session_short_id and short else "",
        "lines": count_lines(text),
        "tokens": approx_token_count(text),
        "token_method": safe_slug(str(info.get("method") or "unknown"), max_len=30),
        "token_encoding": safe_slug(str(info.get("encoding") or "none"), max_len=30),
        "counts": filename_count_suffix(text),
    }
    try:
        name = template.format_map(values)
    except KeyError as exc:
        raise RuntimeError(f"Unknown filename-template placeholder: {exc.args[0]}") from exc
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip(" .")
    if not name.lower().endswith(".md"):
        name += ".md"
    if len(name) > 240:
        stem, suffix = Path(name).stem, Path(name).suffix
        name = stem[: 240 - len(suffix)] + suffix
    return name


def resolve_collision(path: Path, policy: str) -> Path | None:
    if not path.exists() or policy == "overwrite":
        return path
    if policy == "skip":
        return None
    if policy == "error":
        raise FileExistsError(f"Output already exists: {path}")
    if policy != "rename":
        raise ValueError(f"Unknown collision policy: {policy}")
    for index in range(2, 100_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a collision-free name for {path}")


def counter_to_dict(value: Any) -> dict[str, int]:
    if isinstance(value, Counter):
        return dict(sorted(value.items()))
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in sorted(value.items())}
    return {}


def build_manifest(
    *,
    source: Path,
    output: Path | None,
    session_id: str,
    title: str,
    mode: str,
    records: list[Record],
    meta: dict[str, Any],
    text: str,
    redacted: bool,
) -> dict[str, Any]:
    summary = export_summary(records, meta)
    return {
        "schema_version": 1,
        "exporter_version": __version__,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "session_id": session_id,
            "event_count": meta.get("_source_event_count", 0),
        },
        "output": {
            "path": str(output) if output else None,
            "title": title,
            "mode": mode,
            "lines": count_lines(text),
            "token_count": approx_token_count(text),
            "token_counter": token_counter_info(),
            "redacted": redacted,
        },
        "history": {
            "semantics": meta.get("_reconstruction_mode", "chronological_source_history"),
            "compactions_applied": meta.get("_compactions_applied", 0),
            "rollbacks_applied": meta.get("_rollbacks_applied", 0),
        },
        "records": {
            "rendered": len(records),
            "recognized_event_types": counter_to_dict(meta.get("_recognized_event_types")),
            "ignored_event_types": counter_to_dict(meta.get("_ignored_event_types")),
            "ignored_event_reasons": counter_to_dict(meta.get("_ignored_event_reasons")),
            "unknown_event_types": counter_to_dict(meta.get("_unknown_event_types")),
            "cleaned": counter_to_dict(_RUNTIME_AUDIT.get("cleaned")),
            "summarized": counter_to_dict(_RUNTIME_AUDIT.get("summarized")),
            "redacted": counter_to_dict(_RUNTIME_AUDIT.get("redacted")),
        },
        "integrity": {
            "parse_error_count": len(meta.get("_parse_errors", [])),
            "parse_errors": meta.get("_parse_errors", []),
            "repaired_json_line_count": len(meta.get("_repaired_json_lines", [])),
            "repaired_json_lines": meta.get("_repaired_json_lines", []),
            "raw_source_truncation_marker_count": int(meta.get("_raw_source_truncation_marker_count", 0)),
            "raw_source_truncation_tokens_reported": int(meta.get("_raw_source_truncation_tokens_reported", 0)),
            "extracted_source_truncation_marker_count": int(_RUNTIME_AUDIT.get("source_truncation_markers", 0)),
            "extracted_source_truncation_tokens_reported": int(_RUNTIME_AUDIT.get("source_truncation_tokens_reported", 0)),
            "rendered_source_truncation_marker_count": summary.get("rendered_source_truncation_markers", 0),
            "rendered_source_truncation_tokens_reported": summary.get("rendered_source_truncation_tokens_reported", 0),
        },
        "content_summary": summary,
    }


def write_manifest(manifest: dict[str, Any], output_path: Path | None, explicit_path: Path | None = None) -> Path | None:
    if explicit_path:
        path = explicit_path
    elif output_path:
        path = output_path.with_suffix(".manifest.json")
    else:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def report_event_audit(meta: dict[str, Any]) -> None:
    print("\nEvent audit:", file=sys.stderr)
    for label, key in (
        ("recognized", "_recognized_event_types"),
        ("ignored", "_ignored_event_types"),
        ("unknown", "_unknown_event_types"),
    ):
        values = counter_to_dict(meta.get(key))
        print(f"  {label}: {sum(values.values())}", file=sys.stderr)
        for name, count in values.items():
            print(f"    {count:>8}  {name}", file=sys.stderr)


def parse_session(
    path: Path,
    *,
    include_actions: bool,
    include_reasoning_summaries: bool = False,
    reconstruct_live_context: bool = False,
    tool_outputs: str,
    max_tool_chars: int,
    strip: bool,
    max_line_chars: int,
    max_repeated_lines: int,
) -> tuple[list[Record], dict[str, Any]]:
    records: list[Record] = []
    meta: dict[str, Any] = {
        "_recognized_event_types": Counter(),
        "_ignored_event_types": Counter(),
        "_ignored_event_reasons": Counter(),
        "_unknown_event_types": Counter(),
        "_repaired_json_lines": [],
        "_parse_errors": [],
        "_compactions_applied": 0,
        "_rollbacks_applied": 0,
        "_reconstruction_mode": "reconstructed_live_context" if reconstruct_live_context else "chronological_source_history",
        "_raw_source_truncation_marker_count": 0,
        "_raw_source_truncation_tokens_reported": 0,
    }
    last_key: tuple[str, str, str] | None = None

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for seq, line in enumerate(f, 1):
            if not line.strip():
                continue
            meta["_source_event_count"] = int(meta.get("_source_event_count", 0)) + 1
            raw_truncation_matches = list(SOURCE_TRUNCATION_RE.finditer(line))
            if raw_truncation_matches:
                meta["_raw_source_truncation_marker_count"] += len(raw_truncation_matches)
                meta["_raw_source_truncation_tokens_reported"] += sum(int(match.group("count").replace(",", "")) for match in raw_truncation_matches)
            obj, status = parse_jsonl_line(line)
            if obj is None:
                meta["_parse_errors"].append({
                    "line": seq,
                    "error": status.get("error") or status.get("original_error") or "unknown parse error",
                    "sha256": hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest(),
                    "preview": line[:240].rstrip(),
                })
                continue
            if status.get("status") != "exact":
                meta["_repaired_json_lines"].append({
                    "line": seq,
                    "repair": status.get("status"),
                    "repair_count": status.get("repairs", 0),
                    "original_error": status.get("original_error", ""),
                })

            update_session_metadata_from_event(meta, obj)
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            top_type = str(obj.get("type") or "")
            payload_type = str(payload.get("type") or "")
            key_name = event_key(obj)

            if obj.get("type") == "session_meta" and isinstance(payload, dict):
                # Retain stable session metadata but do not let arbitrary nested values
                # overwrite exporter-internal audit keys.
                for k, value in payload.items():
                    if not str(k).startswith("_"):
                        meta[k] = value

            if reconstruct_live_context and top_type == "compacted":
                history = payload.get("replacement_history")
                if isinstance(history, list) and history:
                    records = records_from_replacement_history(
                        history,
                        timestamp=get_timestamp(obj),
                        include_actions=include_actions,
                        include_reasoning_summaries=include_reasoning_summaries,
                        tool_outputs=tool_outputs,
                        max_tool_chars=max_tool_chars,
                        strip=strip,
                        max_line_chars=max_line_chars,
                        max_repeated_lines=max_repeated_lines,
                    )
                    records.insert(0, Record(
                        "note",
                        "system",
                        "[RECONSTRUCTED LIVE CONTEXT: derived from Codex replacement_history; not an exact byte-for-byte model prompt]",
                        get_timestamp(obj),
                        "reconstruction_marker",
                        seq,
                    ))
                    meta["_compactions_applied"] = int(meta.get("_compactions_applied", 0)) + 1
                    meta["_recognized_event_types"][key_name] += 1
                    last_key = None
                    continue

            if reconstruct_live_context and payload_type == "thread_rolled_back":
                try:
                    num_turns = int(payload.get("num_turns") or 0)
                except Exception:
                    num_turns = 0
                records = apply_rollback_to_records(records, num_turns)
                records.append(Record(
                    "note",
                    "system",
                    f"[RECONSTRUCTED ROLLBACK: removed {num_turns} turn(s) according to thread_rolled_back; reconstruction is approximate]",
                    get_timestamp(obj),
                    "rollback_marker",
                    seq,
                ))
                meta["_rollbacks_applied"] = int(meta.get("_rollbacks_applied", 0)) + 1
                meta["_recognized_event_types"][key_name] += 1
                last_key = None
                continue

            rec = classify_record(
                obj,
                seq=seq,
                include_actions=include_actions,
                include_reasoning_summaries=include_reasoning_summaries,
                tool_outputs=tool_outputs,
                max_tool_chars=max_tool_chars,
                strip=strip,
                max_line_chars=max_line_chars,
                max_repeated_lines=max_repeated_lines,
            )
            if not rec or not rec.text.strip():
                reason = known_ignored_reason(obj, include_reasoning_summaries)
                meta["_ignored_event_types"][key_name] += 1
                meta["_ignored_event_reasons"][reason] += 1
                if reason == "unrecognized_schema":
                    meta["_unknown_event_types"][key_name] += 1
                continue

            meta["_recognized_event_types"][key_name] += 1
            key = (rec.kind, rec.role, rec.text)
            if key == last_key:
                audit_increment("cleaned", "adjacent_duplicate_record", 1)
                continue
            last_key = key
            records.append(rec)

    meta["_parse_error_count"] = len(meta["_parse_errors"])
    meta["_repaired_json_line_count"] = len(meta["_repaired_json_lines"])
    return post_process_records(records), meta


def read_session_index_title(session_id: str) -> str:
    return read_all_session_index_titles().get(session_id.lower(), "")


def read_jsonl_set_thread_title(session_id: str) -> str:
    """Read the title argument from the most recent set_thread_title tool call in the JSONL."""
    from pathlib import Path
    home = codex_home()
    sessions_dir = home / "sessions"
    # find the JSONL for this session_id
    for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
        if p.suffix == ".jsonl" and not p.name.endswith(".bak"):
            last_title = ""
            try:
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "set_thread_title" not in line:
                            continue
                        try:
                            obj = json.loads(line)
                            # walk for {"name":"set_thread_title", ...} or {"function":"set_thread_title", ...}
                            for candidate in walk_json_values(obj):
                                if not isinstance(candidate, dict):
                                    continue
                                fn = candidate.get("name") or candidate.get("function") or ""
                                if str(fn).lower() == "set_thread_title":
                                    args = candidate.get("arguments") or candidate.get("input") or candidate.get("parameters") or {}
                                    if isinstance(args, str):
                                        args = json.loads(args)
                                    t = args.get("title") or args.get("name") or ""
                                    if isinstance(t, str) and t.strip():
                                        last_title = t.strip()
                        except Exception:
                            continue
            except Exception:
                pass
            if last_title:
                return last_title
    return ""


@lru_cache(maxsize=1)
def read_all_sqlite_titles() -> dict[str, str]:
    """Load app-owned session titles once per process from known Codex state DBs."""
    home = codex_home()
    titles: dict[str, str] = {}
    for db in (home / "state_5.sqlite", home / "state.sqlite", home / "codex.db"):
        if not db.exists():
            continue
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            con.execute("pragma query_only=on")
            tables = [str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")]
            tables.sort(key=lambda name: (name.lower() not in {"threads", "sessions"}, name.lower()))
            for table in tables:
                columns = [str(row[1]) for row in con.execute(f'pragma table_info("{table}")')]
                id_columns = [c for c in columns if c.lower() in {"id", "thread_id", "session_id", "uuid"}]
                title_columns = [c for c in columns if c.lower() in {"title", "thread_title", "thread_name", "name"}]
                for id_column in id_columns:
                    for title_column in title_columns:
                        try:
                            query = f'select "{id_column}", "{title_column}" from "{table}" where "{title_column}" is not null'
                            for raw_id, raw_title in con.execute(query):
                                if not isinstance(raw_id, str) or not isinstance(raw_title, str):
                                    continue
                                sid = raw_id.strip()
                                title = raw_title.strip()
                                if SESSION_ID_RE.fullmatch(sid) and title and len(title) <= 300:
                                    titles.setdefault(sid.lower(), title)
                        except sqlite3.Error:
                            continue
        except sqlite3.Error:
            continue
        finally:
            if con is not None:
                con.close()
    return titles


def read_sqlite_title(session_id: str) -> str:
    return read_all_sqlite_titles().get(session_id.lower(), "")


@lru_cache(maxsize=1)
def read_all_session_index_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    home = codex_home()
    for index_path in (home / "session_index.jsonl", home / "sessions" / "session_index.jsonl"):
        if not index_path.exists():
            continue
        try:
            with index_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    obj, _status = parse_jsonl_line(line)
                    if not isinstance(obj, dict):
                        continue
                    sid = obj.get("id") or obj.get("session_id") or obj.get("thread_id")
                    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid.strip()):
                        continue
                    for key in ("title", "thread_title", "thread_name", "name"):
                        value = obj.get(key)
                        if isinstance(value, str) and value.strip():
                            titles[sid.strip().lower()] = value.strip()
                            break
        except OSError:
            continue
    return titles

def normalize_title(value: str) -> str:
    value = re.sub(r"^\s*#{1,6}\s+", "", value.strip())
    value = re.sub(r"\s+", " ", value).strip()
    return value[:200]


def discover_title(records: list[Record], meta: dict[str, Any], override: str | None, session_id: str | None) -> str:
    if override and override.strip():
        return normalize_title(override)

    # Canonical, mutable app-owned title stores take precedence over prompt-derived
    # guesses. This prevents a long first prompt from replacing a manually renamed
    # Codex thread title.
    if session_id:
        for fn in (read_sqlite_title, read_session_index_title, read_jsonl_set_thread_title):
            title = fn(session_id)
            if title:
                return normalize_title(title)

    for key in ("title", "thread_title", "thread_name", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_title(value)

    for rec in records:
        if rec.kind == "message" and rec.role == "user" and rec.text.strip():
            return normalize_title(rec.text.strip().splitlines()[0])[:80]

    return normalize_title(str(meta.get("id") or session_id or "codex_thread"))


def select_last_n_turns(records: list[Record], count: int | None) -> list[Record]:
    if count is None:
        return records
    if count < 1:
        raise RuntimeError("--last-n-turns must be >= 1")
    blocks = split_prompt_response_blocks(records)
    if not blocks or count >= len(blocks):
        return records
    return records[blocks[-count]["start"] :]


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
    rows = list_session_descriptors()
    if not rows:
        print(f"_No Codex session JSONLs found under {codex_home()}._")
        return
    print("| # | session_id | size | modified | project | archived | title |")
    print("|---:|---|---:|---|---|---|---|")
    for index, row in enumerate(rows, 1):
        title = str(row["title"]).replace("|", "\\|")
        project = str(row["project"]).replace("|", "\\|")
        print(f"| {index} | {row['session_id']} | {human_size(row['size'])} | {row['modified']} | {project} | {str(row['archived']).lower()} | {title} |")


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
    print(f"| response | user message # | records | assistant chars | response lines | {token_field_label()} | action records | edited | created | first assistant preview |")
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
                f"- Token count method: `{token_counter_info().get('method')}`",
                f"- Token encoding: `{token_counter_info().get('encoding')}`",
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



def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def session_head_metadata(path: Path, max_events: int = 80) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_events:
                    break
                obj, _ = parse_jsonl_line(line)
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                for candidate in (obj, payload):
                    for key in ("cwd", "working_directory", "workspace", "project", "source", "originator"):
                        value = candidate.get(key)
                        if isinstance(value, str) and value.strip() and key not in out:
                            out[key] = value.strip()
                    for key in ("title", "thread_title", "thread_name", "name"):
                        value = candidate.get(key)
                        if isinstance(value, str) and value.strip() and "title" not in out:
                            out["title"] = value.strip()
    except OSError:
        pass
    return out


def session_descriptor(path: Path, title_map: dict[str, str] | None = None) -> dict[str, Any]:
    sid = parse_session_id_from_name(path)
    stat = path.stat()
    head = session_head_metadata(path)
    title = ""
    if sid:
        title = (
            (title_map or {}).get(sid.lower(), "")
            or read_sqlite_title(sid)
            or read_session_index_title(sid)
            or str(head.get("title") or "")
        )
    cwd = str(head.get("cwd") or head.get("working_directory") or head.get("workspace") or "")
    project = Path(cwd).name if cwd else ""
    return {
        "path": path,
        "session_id": sid or path.stem,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "title": title,
        "cwd": cwd,
        "project": project,
        "archived": "archived_sessions" in path.parts,
    }


def list_session_descriptors() -> list[dict[str, Any]]:
    title_map = read_all_session_index_titles()
    title_map.update(read_all_sqlite_titles())
    descriptors = []
    for path in all_session_files():
        try:
            descriptors.append(session_descriptor(path, title_map))
        except OSError:
            continue
    return sorted(descriptors, key=lambda row: row["mtime"], reverse=True)


def _browser_selection(raw: str, upper: int) -> list[int]:
    selected: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            selected.update(range(int(first), int(last) + 1))
        else:
            selected.add(int(part))
    invalid = sorted(value for value in selected if value < 1 or value > upper)
    if invalid:
        raise RuntimeError(f"Invalid browser row(s): {invalid}")
    return sorted(selected)


def _clear_browser_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def browse_sessions() -> list[str]:
    """Portable optional TUI; the noninteractive CLI remains the core architecture."""
    all_rows = list_session_descriptors()
    if not all_rows:
        raise RuntimeError(f"No Codex session JSONLs found under {codex_home()}")

    page_size = 20
    page = 0
    query = ""
    view = "sessions"  # sessions | projects | project
    active_project = ""

    while True:
        filtered = [
            row for row in all_rows
            if (not query or query in " ".join((row["project"], row["title"], row["session_id"], row["cwd"])).lower())
            and (view != "project" or row["project"] == active_project)
        ]
        _clear_browser_screen()
        print("Codex session browser — noninteractive commands remain available for automation")
        print(f"View: {view}" + (f" / {active_project}" if active_project else "") + (f" | Filter: {query}" if query else ""))

        if view == "projects":
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in filtered:
                grouped.setdefault(row["project"] or "(unknown project)", []).append(row)
            project_rows = sorted(grouped.items(), key=lambda item: max(r["mtime"] for r in item[1]), reverse=True)
            pages = max(1, (len(project_rows) + page_size - 1) // page_size)
            page = min(page, pages - 1)
            visible_projects = project_rows[page * page_size : (page + 1) * page_size]
            print(f"Page {page + 1}/{pages}")
            print()
            print("| # | project | sessions | most recent |")
            print("|---:|---|---:|---|")
            for index, (project, rows) in enumerate(visible_projects, 1):
                modified = datetime.fromtimestamp(max(row["mtime"] for row in rows)).isoformat(timespec="seconds")
                escaped_project = project.replace("|", "\\|")
                print(f"| {index} | {escaped_project} | {len(rows)} | {modified} |")
            visible_sessions: list[dict[str, Any]] = []
        else:
            pages = max(1, (len(filtered) + page_size - 1) // page_size)
            page = min(page, pages - 1)
            visible_sessions = filtered[page * page_size : (page + 1) * page_size]
            print(f"Page {page + 1}/{pages} — {len(filtered)} matching sessions")
            print()
            print("| # | session_id | modified | size | project | A | title |")
            print("|---:|---|---|---:|---|---|---|")
            for index, row in enumerate(visible_sessions, 1):
                title = str(row["title"]).replace("|", "\\|")
                project = str(row["project"]).replace("|", "\\|")
                print(f"| {index} | {row['session_id']} | {row['modified']} | {human_size(row['size'])} | {project} | {'Y' if row['archived'] else ''} | {title} |")
            visible_projects = []

        print()
        print("Commands: number/range select · a page-all · n/p page · m projects/sessions · / search · s ID · b back · q quit")
        raw = input("> ").strip()
        if not sys.stdin.isatty():
            print()
        lowered = raw.lower()
        if lowered in {"q", "quit", "exit"}:
            return []
        if lowered == "n":
            page = min(page + 1, pages - 1)
            continue
        if lowered == "p":
            page = max(page - 1, 0)
            continue
        if lowered == "m":
            view = "projects" if view != "projects" else "sessions"
            active_project = ""
            page = 0
            continue
        if lowered == "b":
            view = "projects"
            active_project = ""
            page = 0
            continue
        if raw.startswith("/"):
            query = raw[1:].strip().lower()
            page = 0
            continue
        if lowered == "s":
            needle = input("Session ID, rollout filename, or ID prefix: ").strip().lower()
            if not sys.stdin.isatty():
                print()
            matches = [row for row in all_rows if needle in row["session_id"].lower() or needle in row["path"].name.lower()]
            if len(matches) == 1:
                return [matches[0]["session_id"]]
            query = needle
            view = "sessions"
            page = 0
            continue
        if lowered == "a":
            return [row["session_id"] for row in visible_sessions]
        try:
            if view == "projects":
                choices = _browser_selection(raw, len(visible_projects))
                if len(choices) != 1:
                    raise RuntimeError("Select one project row at a time.")
                active_project = visible_projects[choices[0] - 1][0]
                view = "project"
                page = 0
                continue
            choices = _browser_selection(raw, len(visible_sessions))
            if choices:
                return [visible_sessions[index - 1]["session_id"] for index in choices]
        except (ValueError, RuntimeError) as exc:
            input(f"{exc} Press Enter to continue.")


def resolve_source_paths(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[tuple[Path, str]]:
    pairs: list[tuple[Path, str]] = []
    if args.browse:
        for sid in browse_sessions():
            pairs.append((find_session_file(sid), sid))
    for sid in args.session_id or []:
        pairs.append((find_session_file(sid), sid))
    for path in args.jsonl or []:
        path = path.expanduser().resolve()
        if not path.is_file():
            parser.error(f"JSONL not found: {path}")
        pairs.append((path, parse_session_id_from_name(path)))
    if args.sessions_file:
        for raw in args.sessions_file.read_text(encoding="utf-8").splitlines():
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            candidate = Path(value).expanduser()
            if candidate.is_file():
                pairs.append((candidate.resolve(), parse_session_id_from_name(candidate)))
            else:
                pairs.append((find_session_file(value), value))
    if args.latest_session:
        path = latest_session_file()
        pairs.append((path, parse_session_id_from_name(path)))
    dedup: dict[str, tuple[Path, str]] = {}
    for path, sid in pairs:
        dedup[str(path.resolve()).casefold()] = (path, sid)
    result = list(dedup.values())
    if not result:
        parser.error("provide --session-id, --jsonl, --sessions-file, --latest-session, or --browse")
    return result


def main() -> int:
    configure_utf8_standard_streams()
    ap = argparse.ArgumentParser(
        prog="codex-export",
        description="Export local Codex session JSONL to fidelity-oriented Markdown, individually or in deterministic batches.",
    )
    ap.add_argument("--version", action="version", version=f"codex-export {__version__}")
    ap.add_argument("--session-id", action="append", help="Session UUID; repeat for batch export")
    ap.add_argument("--jsonl", action="append", type=Path, help="Direct JSONL path; repeat for batch export")
    ap.add_argument("--sessions-file", type=Path, help="Text file containing one session UUID or JSONL path per line")
    ap.add_argument("--latest-session", action="store_true")
    ap.add_argument("--list-sessions", action="store_true")
    ap.add_argument("--browse", action="store_true", help="Optional standard-library terminal browser with multi-select")

    config_group = ap.add_argument_group("persistent configuration")
    config_group.add_argument("--show-config", action="store_true")
    config_group.add_argument("--print-out-dir", action="store_true")
    config_group.add_argument("--set-default-out-dir", type=Path)
    config_group.add_argument("--choose-default-out-dir", action="store_true")
    config_group.add_argument("--set-filename-template")
    config_group.add_argument("--print-filename-template", action="store_true")
    config_group.add_argument("--reset-filename-template", action="store_true")
    config_group.add_argument("--reset-config", action="store_true")

    ap.add_argument("--mode", choices=["thread", "response", "turn", "last-response", "last-assistant", "last-substantial", "message", "range", "chat", "chat-actions", "actions"], default="last-response")
    ap.add_argument("--message", type=int)
    ap.add_argument("--response", type=int)
    ap.add_argument("--from-message", type=int)
    ap.add_argument("--to-message", type=int)
    ap.add_argument("--min-chars", type=int, default=1000)
    ap.add_argument("--last-n-turns", type=int)
    ap.add_argument("--live-context", action="store_true", help="Reconstruct active context from compaction and rollback events; explicitly approximate")
    ap.add_argument("--reasoning-summaries", action="store_true", help="Include explicit recorded reasoning summaries; never raw internal reasoning")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--list-responses", action="store_true")
    ap.add_argument("--report-events", action="store_true")
    ap.add_argument("--strict-events", action="store_true", help="Fail if an unrecognized event schema is observed")

    ap.add_argument("--name")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--choose-out-dir", action="store_true")
    ap.add_argument("--remember-out-dir", dest="remember_out_dir", action="store_true", default=True)
    ap.add_argument("--no-remember-out-dir", dest="remember_out_dir", action="store_false")
    ap.add_argument("--save-as", action="store_true", help="Open native Save As dialog; single-session exports only")
    ap.add_argument("--filename-template")
    ap.add_argument("--save-filename-template", action="store_true")
    ap.add_argument("--stable-filenames", action="store_true", help="Use a timestamp-free template and include a short session ID")
    ap.add_argument("--include-session-short-id", action="store_true")
    ap.add_argument("--collision", choices=["rename", "overwrite", "skip", "error"], default="rename")
    ap.add_argument("--open-after", action="store_true")
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--ui-style", action="store_true", default=True)
    ap.add_argument("--no-ui-style", dest="ui_style", action="store_false")
    ap.add_argument("--wrap-md", action="store_true")
    ap.add_argument("--wrap-title", default="")
    ap.add_argument("--no-file", action="store_true")
    ap.add_argument("--no-filename-counts", action="store_true")
    ap.add_argument("--no-frontmatter", action="store_true")
    ap.add_argument("--no-map", action="store_true")
    ap.add_argument("--fence-turns", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--clipboard", action="store_true")
    ap.add_argument("--manifest", dest="manifest", action="store_true", default=True)
    ap.add_argument("--no-manifest", dest="manifest", action="store_false")
    ap.add_argument("--manifest-path", type=Path)

    ap.add_argument("--tokenizer", choices=["auto", "tiktoken", "regex"], default="auto")
    ap.add_argument("--token-encoding", default=DEFAULT_TOKEN_ENCODING)
    ap.add_argument("--require-tiktoken", action="store_true")
    ap.add_argument("--tokenizer-info", action="store_true")
    ap.add_argument("--source-truncation", choices=["annotate", "preserve", "error"], default="annotate")
    ap.add_argument("--redact", action="store_true")
    ap.add_argument("--fix-mojibake", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keep-mojibake", action="store_true")
    ap.add_argument("--keep-hogs", action="store_true")
    ap.add_argument("--max-line-chars", type=int, default=20_000)
    ap.add_argument("--max-repeated-lines", type=int, default=25)
    ap.add_argument("--tool-outputs", choices=["none", "summary", "tail", "full"], default="full")
    ap.add_argument("--max-tool-chars", type=int, default=20_000)
    ap.add_argument("--json", dest="json_result", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if args.reset_config:
        config_path().unlink(missing_ok=True)
        print(config_path())
        return 0
    if args.set_filename_template is not None:
        cfg["filename_template"] = args.set_filename_template
        save_config(cfg)
        print(args.set_filename_template)
        return 0
    if args.reset_filename_template:
        cfg.pop("filename_template", None)
        save_config(cfg)
        print(DEFAULT_FILENAME_TEMPLATE)
        return 0
    if args.print_filename_template:
        print(str(cfg.get("filename_template") or DEFAULT_FILENAME_TEMPLATE))
        return 0
    if args.choose_default_out_dir:
        selected = choose_directory(configured_out_dir(cfg))
        if selected is None:
            return 1
        cfg["last_out_dir"] = str(selected.resolve())
        save_config(cfg)
        print(selected.resolve())
        return 0
    if args.set_default_out_dir:
        chosen = args.set_default_out_dir.expanduser().resolve()
        chosen.mkdir(parents=True, exist_ok=True)
        cfg["last_out_dir"] = str(chosen)
        save_config(cfg)
        print(chosen)
        return 0
    if args.show_config:
        print(json.dumps({"config_path": str(config_path()), **cfg}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.print_out_dir:
        print(configured_out_dir(cfg).resolve())
        return 0
    if args.list_sessions:
        print_session_list()
        return 0

    configure_token_counter(encoding=args.token_encoding, mode=args.tokenizer, require=args.require_tiktoken)
    if args.tokenizer_info:
        print(json.dumps({"python": sys.executable, **token_counter_info()}, ensure_ascii=False, indent=2, sort_keys=True))
        if not any((args.session_id, args.jsonl, args.sessions_file, args.latest_session, args.browse)):
            return 0

    global _SOURCE_TRUNCATION_POLICY
    _SOURCE_TRUNCATION_POLICY = args.source_truncation
    sources = resolve_source_paths(args, ap)
    if args.save_as and len(sources) != 1:
        ap.error("--save-as is only valid for a single source")
    if args.manifest_path and len(sources) != 1:
        ap.error("--manifest-path is only valid for a single source")

    out_dir = args.out_dir.expanduser().resolve() if args.out_dir else configured_out_dir(cfg).expanduser().resolve()
    if args.choose_out_dir:
        selected = choose_directory(out_dir)
        if selected is None:
            return 1
        out_dir = selected.resolve()
    if (args.out_dir or args.choose_out_dir) and args.remember_out_dir:
        cfg["last_out_dir"] = str(out_dir)
        save_config(cfg)
    template = (
        DEFAULT_STABLE_FILENAME_TEMPLATE if args.stable_filenames
        else args.filename_template or str(cfg.get("filename_template") or DEFAULT_FILENAME_TEMPLATE)
    )
    if args.save_filename_template:
        cfg["filename_template"] = template
        save_config(cfg)

    results: list[dict[str, Any]] = []
    clipboard_parts: list[str] = []
    for source_path, supplied_sid in sources:
        reset_runtime_audit()
        include_actions = args.mode in {"thread", "response", "turn", "last-response", "chat-actions", "actions"} or args.list_responses
        records, meta = parse_session(
            path=source_path,
            include_actions=include_actions,
            include_reasoning_summaries=args.reasoning_summaries,
            reconstruct_live_context=args.live_context,
            tool_outputs=args.tool_outputs,
            max_tool_chars=args.max_tool_chars,
            strip=not args.keep_hogs,
            max_line_chars=args.max_line_chars,
            max_repeated_lines=args.max_repeated_lines,
        )
        if args.report_events:
            report_event_audit(meta)
        if args.strict_events and sum(counter_to_dict(meta.get("_unknown_event_types")).values()):
            raise RuntimeError("Unknown event schemas were detected; inspect --report-events or the manifest.")
        if args.list:
            print_message_list(records)
            continue
        if args.list_responses:
            print_response_list(records)
            continue

        scoped = select_last_n_turns(records, args.last_n_turns)
        selected_records = choose_records(scoped, args)
        effective_sid = supplied_sid or parse_session_id_from_name(source_path) or str(meta.get("id") or "")
        title = discover_title(records, meta, args.name, effective_sid)
        descriptor = mode_descriptor(args)
        if args.mode == "thread" and not args.plain:
            text = render_thread_export(records=selected_records, title=title, source=source_path, mode=descriptor, meta=meta, session_id=effective_sid, include_frontmatter=not args.no_frontmatter, include_map=not args.no_map, fence_turns=args.fence_turns)
        elif args.mode == "turn" and not args.plain and not args.ui_style:
            text = render_thread_export(records=selected_records, title=title, source=source_path, mode=descriptor, meta=meta, session_id=effective_sid, include_frontmatter=not args.no_frontmatter, include_map=not args.no_map, fence_turns=args.fence_turns)
        else:
            text = render_export(selected_records, title, source_path, descriptor, args.plain, args.ui_style, not args.plain)
        if not args.keep_mojibake:
            text = cp437_cp1252_mojibake_repair(text)
        text = redact(text, args.redact)
        text = strip_hogs(text, enabled=not args.keep_hogs, max_line_chars=args.max_line_chars, max_repeated_lines=args.max_repeated_lines)
        if args.wrap_md:
            text = wrap_markdown(text, title=args.wrap_title or None)
        text = finalize_count_placeholders(text)

        include_short = args.stable_filenames or args.include_session_short_id or len(sources) > 1
        filename = render_filename(template, title=title, mode=descriptor, session_id=effective_sid, text=text, include_session_short_id=include_short)
        if args.no_filename_counts:
            filename = render_filename(template.replace("{counts}", ""), title=title, mode=descriptor, session_id=effective_sid, text=text, include_session_short_id=include_short)
        output_path: Path | None = None
        if not args.no_file:
            out_dir.mkdir(parents=True, exist_ok=True)
            candidate = out_dir / filename
            if args.save_as:
                selected_path = choose_save_file(out_dir, filename)
                if selected_path is None:
                    return 1
                candidate = selected_path
                if args.remember_out_dir:
                    cfg["last_out_dir"] = str(candidate.parent.resolve())
                    save_config(cfg)
            output_path = resolve_collision(candidate, args.collision)
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(text, encoding="utf-8", newline="\n")

        manifest_path = None
        if args.manifest:
            manifest = build_manifest(source=source_path, output=output_path, session_id=effective_sid, title=title, mode=descriptor, records=selected_records, meta=meta, text=text, redacted=args.redact)
            manifest_path = write_manifest(manifest, output_path, args.manifest_path)
        if args.stdout:
            if len(sources) > 1:
                print(f"\n<!-- codex-export source: {source_path} -->\n")
            print(text, end="")
        if args.clipboard:
            clipboard_parts.append(text)
        if args.open_after and output_path:
            open_in_file_manager(output_path, select=True)
        result = {
            "version": __version__, "file": str(output_path) if output_path else None,
            "manifest": str(manifest_path) if manifest_path else None,
            "source_jsonl": str(source_path), "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "session_id": effective_sid or None, "title": title, "mode": descriptor,
            "lines": count_lines(text), "token_count": approx_token_count(text), "token_counter": token_counter_info(),
            "parse_error_count": len(meta.get("_parse_errors", [])), "repaired_json_line_count": len(meta.get("_repaired_json_lines", [])),
            "redacted": bool(args.redact), "models": collect_models(selected_records, meta),
        }
        results.append(result)
        if not args.json_result and output_path:
            print(output_path)

    if args.clipboard and clipboard_parts:
        copy_to_clipboard("\n\n".join(clipboard_parts))
    if args.json_result:
        print(json.dumps(results[0] if len(results) == 1 else results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
