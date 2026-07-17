from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "Export-CodexThread.py"
SAMPLE = REPO / "tests" / "fixtures" / "sample-session.jsonl"
COMPACTION = REPO / "tests" / "fixtures" / "regression" / "compaction-rollback.jsonl"


def run(args: list[str], tmp_path: Path, *, source: Path = SAMPLE, check: bool = True, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, str(SCRIPT), "--jsonl", str(source), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=check, timeout=45)


def json_result(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_repair_manifest_mode_filename_and_token_metadata(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = run(["--mode", "thread", "--out-dir", str(out), "--tokenizer", "regex", "--json"], tmp_path)
    data = json_result(result)
    assert data["parse_error_count"] == 0
    assert data["repaired_json_line_count"] == 1
    assert "_thread_" in Path(data["file"]).name
    text = Path(data["file"]).read_text(encoding="utf-8")
    assert 'token_count_method: "regex_estimate"' in text
    assert 'token_count_exact_for_encoding: false' in text
    frontmatter_count = int(next(line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("token_count:")))
    assert frontmatter_count == data["token_count"]
    assert "[SOURCE TOOL OUTPUT TRUNCATED BY CODEX BEFORE EXPORT:" in text
    manifest = json.loads(Path(data["manifest"]).read_text(encoding="utf-8"))
    assert manifest["integrity"]["repaired_json_lines"][0]["repair_count"] == 1


def test_last_n_turns(tmp_path: Path) -> None:
    result = run(["--mode", "thread", "--last-n-turns", "2", "--out-dir", str(tmp_path / "out"), "--tokenizer", "regex", "--json"], tmp_path)
    text = Path(json_result(result)["file"]).read_text(encoding="utf-8")
    assert "First prompt" not in text
    assert "Second prompt" in text and "Third prompt" in text
    assert "last-2-turns" in Path(json_result(result)["file"]).name


def test_reasoning_summary_is_opt_in_and_raw_reasoning_never_exports(tmp_path: Path) -> None:
    base = run(["--mode", "thread", "--out-dir", str(tmp_path / "a"), "--tokenizer", "regex", "--json"], tmp_path)
    base_text = Path(json_result(base)["file"]).read_text(encoding="utf-8")
    assert "Checked the relevant files" not in base_text
    assert "private raw reasoning" not in base_text
    opted = run(["--mode", "thread", "--reasoning-summaries", "--out-dir", str(tmp_path / "b"), "--tokenizer", "regex", "--json"], tmp_path)
    opted_text = Path(json_result(opted)["file"]).read_text(encoding="utf-8")
    assert "Reasoning summary (explicit Codex summary; opt-in)" in opted_text
    assert "Checked the relevant files" in opted_text
    assert "private raw reasoning" not in opted_text


def test_live_context_compaction_and_rollback(tmp_path: Path) -> None:
    result = run(["--mode", "thread", "--live-context", "--out-dir", str(tmp_path / "out"), "--tokenizer", "regex", "--json"], tmp_path, source=COMPACTION)
    data = json_result(result)
    text = Path(data["file"]).read_text(encoding="utf-8")
    assert "RECONSTRUCTED LIVE CONTEXT" in text
    assert "RECONSTRUCTED ROLLBACK" in text
    assert "Old prompt" not in text
    assert "Rolled prompt" not in text
    assert "Current prompt" in text
    manifest = json.loads(Path(data["manifest"]).read_text(encoding="utf-8"))
    assert manifest["history"]["compactions_applied"] == 1
    assert manifest["history"]["rollbacks_applied"] == 1


def test_batch_collision_and_short_ids(tmp_path: Path) -> None:
    copy = tmp_path / "rollout-2026-01-01T00-00-00-33333333-3333-7333-8333-333333333333.jsonl"
    copy.write_bytes(SAMPLE.read_bytes())
    out = tmp_path / "out"
    env = os.environ.copy()
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    cmd = [sys.executable, str(SCRIPT), "--jsonl", str(SAMPLE), "--jsonl", str(copy), "--mode", "thread", "--out-dir", str(out), "--tokenizer", "regex", "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=True, timeout=45)
    rows = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(rows) == 2
    names = [Path(row["file"]).name for row in rows]
    assert len(set(names)) == 2
    assert all(row["session_id"].replace("-", "")[-8:] in name for row, name in zip(rows, names))


def test_persistent_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    env = os.environ.copy()
    env["CODEX_EXPORT_CONFIG"] = str(config)
    target = tmp_path / "remembered"
    subprocess.run([sys.executable, str(SCRIPT), "--set-default-out-dir", str(target)], env=env, check=True, capture_output=True, text=True, timeout=45)
    result = subprocess.run([sys.executable, str(SCRIPT), "--print-out-dir"], env=env, check=True, capture_output=True, text=True, timeout=45)
    assert Path(result.stdout.strip()) == target.resolve()


def test_archived_session_discovery(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    archived = codex_home / "archived_sessions" / "2026" / "01"
    archived.mkdir(parents=True)
    target = archived / "rollout-2026-01-01T00-00-00-44444444-4444-7444-8444-444444444444.jsonl"
    target.write_bytes(SAMPLE.read_bytes())
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    result = subprocess.run([sys.executable, str(SCRIPT), "--list-sessions"], env=env, capture_output=True, text=True, encoding="utf-8", check=True, timeout=45)
    assert "44444444-4444-7444-8444-444444444444" in result.stdout


@pytest.mark.skipif(os.environ.get("RUN_OS_CLIPBOARD_TESTS") != "1", reason="real OS clipboard test is opt-in")
def test_real_os_clipboard_when_runner_permits(tmp_path: Path) -> None:
    result = run(
        ["--mode", "last-response", "--clipboard", "--no-file", "--no-manifest", "--tokenizer", "regex"],
        tmp_path,
        check=False,
    )
    if result.returncode != 0:
        combined = (result.stdout + "\n" + result.stderr).lower()
        unavailable = (
            "clipboard" in combined
            and any(marker in combined for marker in (
                "not available", "unavailable", "openclipboard", "display", "pbcopy",
                "wl-copy", "xclip", "xsel", "no suitable clipboard", "no supported clipboard",
            ))
        )
        if unavailable:
            pytest.skip("runner does not expose an interactive OS clipboard")
    assert result.returncode == 0, result.stderr


def test_tiktoken_path_records_encoding_and_ordinary_special_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_exporter import cli

    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeEncoding:
        name = "cl100k_base"

        def encode(self, text: str, *, disallowed_special=()):
            calls.append((text, tuple(disallowed_special)))
            return list(text.encode("utf-8"))

    fake = types.SimpleNamespace(get_encoding=lambda name: FakeEncoding())
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    info = cli.configure_token_counter(encoding="cl100k_base", mode="tiktoken", require=True)
    assert info["method"] == "tiktoken"
    assert info["encoding"] == "cl100k_base"
    assert info["exact_for_encoding"] is True
    assert cli.approx_token_count("literal <|endoftext|> text") > 0
    assert calls[-1][1] == ()

def test_all_export_modes_in_one_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from codex_exporter import cli

    modes = [
        ("thread", []),
        ("response", ["--response", "2"]),
        ("turn", ["--response", "2", "--no-ui-style"]),
        ("last-response", []),
        ("last-assistant", []),
        ("last-substantial", ["--min-chars", "1"]),
        ("message", ["--message", "2"]),
        ("range", ["--from-message", "2", "--to-message", "4"]),
        ("chat", []),
        ("chat-actions", []),
        ("actions", []),
    ]
    monkeypatch.setenv("CODEX_EXPORT_CONFIG", str(tmp_path / "config.json"))
    for mode, extra in modes:
        out = tmp_path / mode
        monkeypatch.setattr(sys, "argv", [
            "codex-export", "--jsonl", str(SAMPLE), "--mode", mode, *extra,
            "--out-dir", str(out), "--tokenizer", "regex", "--json",
        ])
        assert cli.main() == 0
        output = capsys.readouterr().out.strip().splitlines()[-1]
        data = json.loads(output)
        assert data["mode"].startswith(mode)
        assert Path(data["file"]).is_file()
        assert Path(data["manifest"]).is_file()


def test_list_surfaces(tmp_path: Path) -> None:
    messages = run(["--list", "--tokenizer", "regex"], tmp_path)
    responses = run(["--list-responses", "--tokenizer", "regex"], tmp_path)
    assert "| # | role |" in messages.stdout
    assert "| response |" in responses.stdout


def test_unknown_event_reporting_and_strict_mode(tmp_path: Path) -> None:
    source = tmp_path / "unknown.jsonl"
    source.write_text(SAMPLE.read_text(encoding="utf-8") + '{"type":"brand_new_event","payload":{"type":"future_shape","value":1}}\n', encoding="utf-8")
    result = run(["--mode", "thread", "--out-dir", str(tmp_path / "out"), "--tokenizer", "regex", "--json"], tmp_path, source=source)
    manifest = json.loads(Path(json_result(result)["manifest"]).read_text(encoding="utf-8"))
    assert manifest["records"]["unknown_event_types"]["brand_new_event/future_shape/-"] == 1
    strict = run(["--mode", "thread", "--strict-events", "--no-file", "--no-manifest", "--tokenizer", "regex"], tmp_path, source=source, check=False)
    assert strict.returncode != 0
    assert "Unknown event schemas" in strict.stderr


def test_source_truncation_policies(tmp_path: Path) -> None:
    preserved = run(["--mode", "thread", "--source-truncation", "preserve", "--out-dir", str(tmp_path / "p"), "--tokenizer", "regex", "--json"], tmp_path)
    assert "…1,234 tokens truncated…" in Path(json_result(preserved)["file"]).read_text(encoding="utf-8")
    failed = run(["--mode", "thread", "--source-truncation", "error", "--no-file", "--no-manifest", "--tokenizer", "regex"], tmp_path, check=False)
    assert failed.returncode != 0
    assert "Source JSONL contains 1 Codex runtime truncation marker" in failed.stderr


def test_saved_filename_template_and_collision_rename(tmp_path: Path) -> None:
    out = tmp_path / "out"
    first = run([
        "--mode", "thread", "--out-dir", str(out), "--filename-template", "{title}_{mode}.md",
        "--save-filename-template", "--tokenizer", "regex", "--json",
    ], tmp_path)
    second = run(["--mode", "thread", "--tokenizer", "regex", "--json"], tmp_path)
    first_path = Path(json_result(first)["file"])
    second_path = Path(json_result(second)["file"])
    assert first_path.name == "sample_export_thread_thread.md"
    assert second_path.name == "sample_export_thread_thread-2.md"
    assert second_path.parent == out.resolve()


def test_sessions_file_batch_input(tmp_path: Path) -> None:
    second = tmp_path / "rollout-2026-01-01T00-00-00-55555555-5555-7555-8555-555555555555.jsonl"
    second.write_bytes(SAMPLE.read_bytes())
    inputs = tmp_path / "sessions.txt"
    inputs.write_text(f"{SAMPLE}\n{second}\n", encoding="utf-8")
    env = os.environ.copy()
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--sessions-file", str(inputs), "--mode", "thread",
        "--out-dir", str(tmp_path / "out"), "--tokenizer", "regex", "--json",
    ], env=env, capture_output=True, text=True, encoding="utf-8", check=True, timeout=45)
    assert len(json.loads(result.stdout.strip().splitlines()[-1])) == 2


def test_browser_single_selection(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    active = home / "sessions" / "2026" / "01"
    active.mkdir(parents=True)
    target = active / "rollout-2026-01-01T00-00-00-66666666-6666-7666-8666-666666666666.jsonl"
    target.write_bytes(SAMPLE.read_bytes())
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    # Reproduce Windows' legacy redirected-stream encoding on every CI OS.
    # The CLI must override it so terminal-browser and --json output stay UTF-8.
    env["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--browse", "--mode", "thread", "--out-dir", str(tmp_path / "out"),
        "--tokenizer", "regex", "--json",
    ], input="1\n", env=env, capture_output=True, text=True, encoding="utf-8", check=True, timeout=45)
    assert "Codex session browser —" in result.stdout
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["session_id"] == "66666666-6666-7666-8666-666666666666"


def test_manifest_distinguishes_raw_extracted_and_selected_truncation(tmp_path: Path) -> None:
    result = run([
        "--mode", "message", "--message", "1", "--out-dir", str(tmp_path / "out"),
        "--tokenizer", "regex", "--json",
    ], tmp_path)
    manifest = json.loads(Path(json_result(result)["manifest"]).read_text(encoding="utf-8"))
    integrity = manifest["integrity"]
    assert integrity["raw_source_truncation_marker_count"] == 2
    assert integrity["extracted_source_truncation_marker_count"] == 1
    assert integrity["rendered_source_truncation_marker_count"] == 0

def test_stable_filename_and_template_config_commands(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["CODEX_EXPORT_CONFIG"] = str(tmp_path / "config.json")
    custom = "archive_{title}_{mode}{session_short_part}.md"
    subprocess.run([sys.executable, str(SCRIPT), "--set-filename-template", custom], env=env, check=True, capture_output=True, text=True, timeout=45)
    shown = subprocess.run([sys.executable, str(SCRIPT), "--print-filename-template"], env=env, check=True, capture_output=True, text=True, timeout=45)
    assert shown.stdout.strip() == custom
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--jsonl", str(SAMPLE), "--mode", "thread", "--stable-filenames",
        "--out-dir", str(tmp_path / "out"), "--tokenizer", "regex", "--json",
    ], env=env, capture_output=True, text=True, encoding="utf-8", check=True, timeout=45)
    name = Path(json.loads(result.stdout.strip().splitlines()[-1])["file"]).name
    assert "00000001" in name
    assert not any(day in name for day in ("MON_", "TUE_", "WED_", "THU_", "FRI_", "SAT_", "SUN_"))
