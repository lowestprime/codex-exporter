"""End-to-end smoke tests against a synthetic Codex session JSONL fixture.

The fixture lives at ``tests/fixtures/sample-session.jsonl`` and is shaped to
exercise the paths the user iterated v3..v7 to fix: user/assistant message
extraction, apply_patch -> Edited/Created/Deleted file cards, command/output
merging, transport-header stripping (Chunk ID/Wall time/Process exited), and
secret redaction. Each test invokes the script as a subprocess so we're
testing the same surface a Codex skill or shell user would invoke.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "Export-CodexThread.py"
FIXTURE = REPO / "tests" / "fixtures" / "sample-session.jsonl"


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT), "--jsonl", str(FIXTURE), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True, **kw)


def test_version() -> None:
    out = subprocess.run([sys.executable, str(SCRIPT), "--version"], capture_output=True, text=True, check=True)
    assert "codex-export" in out.stdout
    assert "0." in out.stdout


def test_thread_export(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    res = run(["--mode", "thread", "--out-dir", str(out_dir), "--no-filename-counts"])
    files = list(out_dir.glob("*.md"))
    assert files, f"no export written; stdout={res.stdout!r} stderr={res.stderr!r}"
    text = files[0].read_text(encoding="utf-8")

    # Frontmatter present
    assert text.startswith("---\n"), "missing YAML frontmatter"
    assert "session_id:" in text
    assert "models_used:" in text

    # Map present
    assert "## Thread export map" in text
    assert "### Prompt/response index" in text

    # Both turns rendered
    assert "## Turn 001" in text
    assert "## Turn 002" in text

    # Created-file card from apply_patch
    assert "Created file" in text, f"missing Created file card; export was:\n{text[:2000]}"
    assert "README.md" in text

    # Transport headers were stripped from the merged shell output
    assert "Chunk ID:" not in text
    assert "Original token count:" not in text
    assert "Process exited with code" not in text
    # The actual command output survived the header strip
    assert "hello" in text

    # Action group summary line present
    assert "Created 1 file" in text or "ran 1 command" in text.lower()


def test_redact(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    res = run(["--mode", "thread", "--out-dir", str(out_dir), "--redact", "--no-filename-counts"])
    text = next(out_dir.glob("*.md")).read_text(encoding="utf-8")
    # Both the OpenAI key and the password value must be gone. Either secret
    # could be replaced by [REDACTED_OPENAI_KEY] or the broader [REDACTED]
    # depending on which pattern fires first; we just assert removal.
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in text
    assert "hunter2hunter2hunter2" not in text
    assert "[REDACTED" in text  # at least one redaction sentinel made it in


def test_last_response_clipboard_skipped(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    res = run(["--mode", "last-response", "--out-dir", str(out_dir), "--no-filename-counts"])
    text = next(out_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "README content" in text


def test_list_responses() -> None:
    res = run(["--list-responses"])
    assert "| response |" in res.stdout
    assert "| 1 |" in res.stdout and "| 2 |" in res.stdout


def test_json_result(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    res = run(["--mode", "thread", "--out-dir", str(out_dir), "--json", "--no-filename-counts"])
    line = res.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["mode"] == "thread"
    assert payload["lines"] > 10
    assert payload["approx_tokens"] > 10
    assert payload["source_sha256"] and len(payload["source_sha256"]) == 64
    assert Path(payload["file"]).exists()


def test_chat_mode(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    res = run(["--mode", "chat", "--out-dir", str(out_dir), "--no-filename-counts"])
    text = next(out_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "summarize the plan" in text
    assert "Here is the plan" in text
    # Actions/tool output should NOT show up in chat mode
    assert "$ echo hello" not in text
    assert "Created file" not in text


if __name__ == "__main__":  # pragma: no cover
    import tempfile, traceback
    failed = 0
    for name in [n for n in globals() if n.startswith("test_")]:
        try:
            with tempfile.TemporaryDirectory() as td:
                fn = globals()[name]
                if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    fn(Path(td))
                else:
                    fn()
                print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    raise SystemExit(failed)
