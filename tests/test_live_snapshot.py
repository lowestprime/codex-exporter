"""Deterministic live-rollout regressions using only synthetic local records.

The writer is advanced at named export stages instead of relying on timing or
sleeping, so the same races are exercised on Windows, macOS, and Linux.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

from codex_exporter import cli


SID = "11111111-2222-4333-8444-555555555555"
BEFORE = "Message present before the snapshot"
AFTER = "Message appended after the snapshot"


def _record(message: str) -> bytes:
    return (json.dumps({
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": message},
    }, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.fixture
def rollout(tmp_path: Path, monkeypatch):
    source = tmp_path / f"rollout-2026-11-06T00-00-00-{SID}.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": SID}},
        {"type": "event_msg", "payload": {
            "type": "user_message", "message": "Export this running thread"}},
    ]
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows) + _record(BEFORE)
    source.write_bytes(raw)
    monkeypatch.setenv("CODEX_EXPORT_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(cli, "codex_home", lambda: tmp_path / "unused-codex-home")
    return source, raw


@pytest.fixture
def archiver(tmp_path: Path) -> Path:
    """A real, independently readable ZIP without a platform 7z dependency."""
    script = tmp_path / "synthetic_archiver.py"
    script.write_text(
        "import sys, zipfile\n"
        "args = sys.argv[1:]\n"
        "archive = next(arg for arg in args if arg.endswith('.zip'))\n"
        "members = args[args.index(archive) + 1:]\n"
        "with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_LZMA) as output:\n"
        "    for member in members:\n"
        "        output.write(member, arcname=member)\n",
        encoding="utf-8",
    )
    executable = tmp_path / ("synthetic-7z.cmd" if os.name == "nt" else "synthetic-7z")
    if os.name == "nt":
        executable.write_text(
            f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        executable.write_text(f"#!{sys.executable}\n" + script.read_text(encoding="utf-8"),
                              encoding="utf-8")
        executable.chmod(0o755)
    return executable


def _export(source: Path, output: Path, *, fast: bool, monkeypatch, capsys,
            extra: tuple[str, ...] = ()) -> dict:
    monkeypatch.setattr(sys, "argv", [
        "codex-export", "--jsonl", str(source), "--mode", "thread",
        "--fast" if fast else "--no-fast", "--no-progress", "--tokenizer", "regex",
        "--name", "Live snapshot regression", "--out-dir", str(output),
        "--no-remember-out-dir", "--json", *extra,
    ])
    assert cli.main() == 0
    return json.loads(capsys.readouterr().out)


def _artifacts(result: dict) -> tuple[bytes, bytes | None, dict]:
    if result.get("archive"):
        with zipfile.ZipFile(result["archive"]) as archive:
            markdown = archive.read(result["archived_markdown"])
            manifest = json.loads(archive.read(result["archived_manifest"]))
            raw = archive.read(result["archived_source"]) if result["archived_source"] else None
        return markdown, raw, manifest
    return (Path(result["file"]).read_bytes(),
            Path(result["source_copy"]).read_bytes() if result.get("source_copy") else None,
            json.loads(Path(result["manifest"]).read_text(encoding="utf-8")))


def _manifest_digest(manifest: dict) -> str:
    return manifest.get("source_sha256") or manifest["source"]["sha256"]


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
@pytest.mark.parametrize("layout", ["zip-clean", "zip-keep", "zip-no-source", "loose", "markdown-only"])
def test_append_during_render_exports_one_consistent_snapshot(
    rollout, archiver, tmp_path, monkeypatch, capsys, fast, layout,
):
    source, original = rollout
    classify = cli.classify_record
    appended = False

    def append_while_rendering(*args, **kwargs):
        nonlocal appended
        if not appended:
            with source.open("ab") as writer:
                writer.write(_record(AFTER))
            appended = True
        return classify(*args, **kwargs)

    monkeypatch.setattr(cli, "classify_record", append_while_rendering)
    extra = ("--archiver-path", str(archiver))
    if layout == "zip-keep":
        extra += ("--no-archive-cleanup",)
    elif layout == "zip-no-source":
        extra += ("--no-source-copy",)
    elif layout == "loose":
        extra += ("--no-archive",)
    elif layout == "markdown-only":
        extra += ("--no-archive", "--no-source-copy")

    output = tmp_path / "export"
    result = _export(source, output, fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=extra)
    markdown, raw, manifest = _artifacts(result)
    digest = hashlib.sha256(original).hexdigest()
    assert appended
    assert BEFORE.encode() in markdown
    assert AFTER.encode() not in markdown
    assert result["source_sha256"] == _manifest_digest(manifest) == digest
    assert digest.encode() in markdown
    manifest_source = manifest["source"]
    assert (manifest_source["path"] if isinstance(manifest_source, dict) else manifest_source) == str(source)
    assert result["source_jsonl"] == str(source)
    assert manifest["source_snapshot"] == result["source_snapshot"]
    assert result["source_snapshot"]["captured_sha256"] == digest
    assert result["source_snapshot"]["captured_size_bytes"] == len(original)
    assert result["source_snapshot"]["prefix_verified"] is True
    assert cli.yaml_quote(str(source)).encode() in markdown
    assert source.read_bytes() == original + _record(AFTER)
    if layout in {"zip-no-source", "markdown-only"}:
        assert raw is None
    else:
        assert raw == original
        assert result["source_copy_sha256"] == digest
    if layout.startswith("zip"):
        assert result["archive_verified"] is True
        assert result["loose_files_removed"] is (layout != "zip-keep")
        if layout != "zip-keep":
            assert not list(output.glob("*.md"))
            assert not list(output.glob("*.manifest.json"))
            assert not list(output.glob("*.jsonl"))
    assert not list(output.rglob("*.partial"))


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
def test_partial_utf8_final_record_is_deferred_then_included_next_run(
    rollout, tmp_path, monkeypatch, capsys, fast,
):
    source, original = rollout
    last = _record("A complete Unicode response 🧠")
    # Freeze midway through a multibyte code point, as can happen during a write.
    cutoff = last.index("🧠".encode()) + 2
    partial = last[:cutoff]
    source.write_bytes(original + partial)
    first = _export(source, tmp_path / "first", fast=fast, monkeypatch=monkeypatch,
                    capsys=capsys, extra=("--no-archive",))
    markdown, raw, manifest = _artifacts(first)
    assert raw == original
    assert first["parse_error_count"] == 0
    assert first["source_sha256"] == _manifest_digest(manifest) == hashlib.sha256(original).hexdigest()
    assert b"A complete Unicode response" not in markdown
    assert source.read_bytes() == original + partial
    assert first["source_snapshot"]["deferred_tail_bytes"] == len(partial)
    assert manifest["source_snapshot"] == first["source_snapshot"]
    assert first["source_snapshot"]["captured_size_bytes"] == len(original)

    with source.open("ab") as writer:
        writer.write(last[cutoff:])
    second = _export(source, tmp_path / "second", fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--no-archive",))
    markdown, raw, manifest = _artifacts(second)
    assert raw == original + last
    assert second["parse_error_count"] == 0
    assert "A complete Unicode response 🧠" in markdown.decode("utf-8")
    assert second["source_sha256"] == _manifest_digest(manifest) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
def test_valid_final_record_without_newline_is_retained(
    rollout, tmp_path, monkeypatch, capsys, fast,
):
    source, original = rollout
    final = _record("Valid final record has no newline").rstrip(b"\n")
    source.write_bytes(original + final)
    result = _export(source, tmp_path / "export", fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--no-archive",))
    markdown, raw, manifest = _artifacts(result)
    assert raw == original + final
    assert b"Valid final record has no newline" in markdown
    assert result["parse_error_count"] == 0
    assert result["source_sha256"] == _manifest_digest(manifest) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
def test_newline_terminated_corruption_remains_auditable(
    rollout, tmp_path, monkeypatch, capsys, fast,
):
    source, original = rollout
    corrupt = b'{"type": this is corrupt JSON}\n'
    source.write_bytes(original + corrupt)
    result = _export(source, tmp_path / "export", fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--no-archive",))
    _, raw, manifest = _artifacts(result)
    assert raw == original + corrupt
    assert result["parse_error_count"] == 1
    assert result["source_sha256"] == _manifest_digest(manifest) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("append_when", ["before-copy", "after-copy"])
def test_capture_is_bounded_and_verifies_the_retained_prefix(
    rollout, tmp_path, monkeypatch, append_when,
):
    from codex_exporter import snapshot

    source, original = rollout
    stage = tmp_path / "snapshot-stage"
    stage.mkdir()
    copy_prefix = snapshot._copy_prefix

    def append_to_source():
        with source.open("ab") as writer:
            writer.write(_record(AFTER))

    def copy_while_writing(*args, **kwargs):
        if append_when == "before-copy":
            append_to_source()
        result = copy_prefix(*args, **kwargs)
        if append_when == "after-copy":
            append_to_source()
        return result

    monkeypatch.setattr(snapshot, "_copy_prefix", copy_while_writing)
    with snapshot.snapshot_source(source, directory=stage) as frozen:
        assert frozen.path.read_bytes() == original
        assert frozen.original_path == source
        assert frozen.size_bytes == len(original)
        assert frozen.sha256 == hashlib.sha256(original).hexdigest()
        metadata = frozen.metadata
        assert metadata["policy"] == "verified_prefix"
        assert metadata["source_size_bytes_at_open"] == len(original)
        assert metadata["source_size_bytes_after_capture"] == len(original) + len(_record(AFTER))
        assert metadata["captured_size_bytes"] == len(original)
        assert metadata["deferred_tail_bytes"] == 0
        assert metadata["captured_sha256"] == frozen.sha256
        assert metadata["source_changed_during_capture"] is True
        assert metadata["prefix_verified"] is True
        assert str(frozen.path) not in json.dumps(metadata)
        staged_path = frozen.path
    assert not staged_path.exists()
    assert source.read_bytes() == original + _record(AFTER)
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("change", ["truncate", "rewrite", "replace"])
def test_capture_rejects_changes_to_the_existing_prefix_and_removes_staging(
    rollout, tmp_path, monkeypatch, change,
):
    from codex_exporter import snapshot

    source, original = rollout
    stage = tmp_path / "snapshot-stage"
    stage.mkdir()
    copy_prefix = snapshot._copy_prefix

    def mutate_after_copy(*args, **kwargs):
        result = copy_prefix(*args, **kwargs)
        if change == "truncate":
            source.write_bytes(original[:len(original) // 2])
        elif change == "rewrite":
            # Preserve length and mtime: metadata alone cannot verify this prefix.
            old_stat = source.stat()
            replacement = original.replace(BEFORE.encode(), b"X" * len(BEFORE))
            source.write_bytes(replacement)
            os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        else:
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_bytes(original)
            try:
                os.replace(replacement, source)
            except PermissionError:
                pytest.skip("This filesystem denies replacing an open file")
        return result

    monkeypatch.setattr(snapshot, "_copy_prefix", mutate_after_copy)
    with pytest.raises((RuntimeError, OSError)):
        with snapshot.snapshot_source(source, directory=stage):
            pytest.fail("A modified original prefix was accepted as a verified snapshot")
    assert source.exists()
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("change", ["append", "rewrite", "remove"])
def test_changes_after_capture_do_not_invalidate_frozen_bytes(
    rollout, tmp_path, change,
):
    from codex_exporter import snapshot

    source, original = rollout
    with snapshot.snapshot_source(source, directory=tmp_path) as frozen:
        if change == "append":
            with source.open("ab") as writer:
                writer.write(_record(AFTER))
        elif change == "rewrite":
            source.write_bytes(_record("New unrelated rollout contents"))
        else:
            source.unlink()
        assert frozen.path.read_bytes() == original
        assert frozen.sha256 == hashlib.sha256(original).hexdigest()
        frozen_path = frozen.path
    assert not frozen_path.exists()


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
def test_compressed_rollout_uses_decoded_snapshot_provenance(
    rollout, archiver, tmp_path, monkeypatch, capsys, fast,
):
    zstd = pytest.importorskip("zstandard")
    source, original = rollout
    compressed = source.with_suffix(".jsonl.zst")
    physical = zstd.ZstdCompressor().compress(original)
    compressed.write_bytes(physical)
    source.unlink()
    result = _export(compressed, tmp_path / "export", fast=fast,
                     monkeypatch=monkeypatch, capsys=capsys,
                     extra=("--archiver-path", str(archiver)))
    markdown, raw, manifest = _artifacts(result)
    assert raw == original
    digest = hashlib.sha256(original).hexdigest()
    assert result["source_sha256"] == result["source_copy_sha256"] == digest
    assert _manifest_digest(manifest) == digest
    manifest_source = manifest["source"]
    assert (manifest_source["path"] if isinstance(manifest_source, dict) else manifest_source) == str(compressed)
    assert manifest["source_snapshot"]["captured_sha256"] == digest
    assert cli.yaml_quote(str(compressed)).encode() in markdown
    assert result["source_jsonl"] == str(compressed)
    if fast:
        assert manifest["source_encoding"] == "zstandard"
    assert compressed.read_bytes() == physical


def test_snapshot_cleanup_runs_on_consumer_failure(rollout, tmp_path):
    from codex_exporter.snapshot import snapshot_source

    source, original = rollout
    stage = tmp_path / "snapshot-stage"
    stage.mkdir()
    with pytest.raises(RuntimeError, match="synthetic rendering failure"):
        with snapshot_source(source, directory=stage) as frozen:
            assert frozen.path.read_bytes() == original
            raise RuntimeError("synthetic rendering failure")
    assert source.read_bytes() == original
    assert list(stage.iterdir()) == []


def test_truncated_compressed_frame_is_not_silently_accepted(rollout, tmp_path):
    zstd = pytest.importorskip("zstandard")
    from codex_exporter.snapshot import snapshot_source

    source, original = rollout
    compressed = source.with_suffix(".jsonl.zst")
    # All JSONL bytes can be decoded even when the frame checksum is incomplete.
    physical = zstd.ZstdCompressor(write_checksum=True).compress(original)
    compressed.write_bytes(physical[:-1])
    stage = tmp_path / "snapshot-stage"
    stage.mkdir()
    with pytest.raises((RuntimeError, OSError, zstd.ZstdError)):
        with snapshot_source(compressed, directory=stage):
            pytest.fail("An incomplete compressed frame was presented as a complete snapshot")
    assert compressed.read_bytes() == physical[:-1]
    assert list(stage.iterdir()) == []


def test_concatenated_compressed_frames_preserve_all_decoded_bytes(rollout, tmp_path):
    zstd = pytest.importorskip("zstandard")
    from codex_exporter.snapshot import snapshot_source

    source, original = rollout
    compressed = source.with_suffix(".jsonl.zst")
    split = len(original) // 2
    compressor = zstd.ZstdCompressor(write_checksum=True)
    physical = compressor.compress(original[:split]) + compressor.compress(original[split:])
    compressed.write_bytes(physical)
    with snapshot_source(compressed, directory=tmp_path) as frozen:
        assert frozen.path.read_bytes() == original
        assert frozen.sha256 == hashlib.sha256(original).hexdigest()
        assert frozen.metadata["source_size_bytes_at_open"] == len(physical)
        assert frozen.metadata["deferred_tail_bytes"] == 0
    assert compressed.read_bytes() == physical


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
@pytest.mark.parametrize("change", ["rewrite", "remove"])
def test_original_can_rotate_after_capture_without_changing_the_archive(
    rollout, archiver, tmp_path, monkeypatch, capsys, fast, change,
):
    source, original = rollout
    classify = cli.classify_record
    changed = False
    new_contents = _record("Replacement rollout outside the captured snapshot")

    def rotate_while_rendering(*args, **kwargs):
        nonlocal changed
        if not changed:
            if change == "rewrite":
                source.write_bytes(new_contents)
            else:
                source.unlink()
            changed = True
        return classify(*args, **kwargs)

    monkeypatch.setattr(cli, "classify_record", rotate_while_rendering)
    result = _export(source, tmp_path / "export", fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--archiver-path", str(archiver)))
    markdown, raw, manifest = _artifacts(result)
    digest = hashlib.sha256(original).hexdigest()
    assert changed
    assert raw == original
    assert result["source_sha256"] == result["source_copy_sha256"] == digest
    assert _manifest_digest(manifest) == digest
    assert BEFORE.encode() in markdown
    assert b"Replacement rollout" not in markdown
    assert result["archive_verified"] is True
    assert result["loose_files_removed"] is True
    if change == "rewrite":
        assert source.read_bytes() == new_contents
    else:
        assert not source.exists()


def test_no_file_export_reports_the_captured_hash_during_append(
    rollout, tmp_path, monkeypatch, capsys,
):
    source, original = rollout
    classify = cli.classify_record
    appended = False

    def append_while_rendering(*args, **kwargs):
        nonlocal appended
        if not appended:
            with source.open("ab") as writer:
                writer.write(_record(AFTER))
            appended = True
        return classify(*args, **kwargs)

    monkeypatch.setattr(cli, "classify_record", append_while_rendering)
    output = tmp_path / "unused-output"
    result = _export(source, output, fast=False, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--no-file",))
    assert appended
    assert result["file"] is None
    assert result["manifest"] is None
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["source_snapshot"]["captured_size_bytes"] == len(original)
    assert not result.get("archive")
    assert not output.exists()
    assert source.read_bytes() == original + _record(AFTER)


@pytest.mark.parametrize("fast", [False, True], ids=["buffered", "streamed"])
def test_repairable_windows_escape_at_eof_is_preserved_without_newline(
    rollout, tmp_path, monkeypatch, capsys, fast,
):
    source, original = rollout
    final = r'{"type":"event_msg","payload":{"type":"agent_message","message":"Saved C:\Logs\export"}}'.encode()
    source.write_bytes(original + final)
    result = _export(source, tmp_path / "export", fast=fast, monkeypatch=monkeypatch,
                     capsys=capsys, extra=("--no-archive",))
    markdown, raw, manifest = _artifacts(result)
    assert raw == original + final
    assert r"Saved C:\Logs\export".encode() in markdown
    assert result["parse_error_count"] == 0
    assert result["source_snapshot"]["deferred_tail_bytes"] == 0
    assert result["source_sha256"] == _manifest_digest(manifest) == hashlib.sha256(raw).hexdigest()
    if fast:
        assert manifest["records"]["repaired_lines"] == 1
    else:
        assert manifest["integrity"]["repaired_json_line_count"] == 1
