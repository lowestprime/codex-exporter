"""Portable review bundles retain every byte of the rendered transcript.

All fixtures are synthetic. Assertions independently decode ZIP members, verify
hashes and source offsets, and reconstruct the original rather than mirroring
implementation helpers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import struct
import warnings
import zipfile

import pytest

from codex_exporter import review


MARKER = b"\n--- BEGIN TRANSCRIPT ---\n"
CHUNK_BYTES = 16_384
SOURCE_DIGEST = hashlib.sha256(b"synthetic native rollout").hexdigest()


def _markdown(tmp_path: Path, raw: bytes, name: str = "synthetic-thread.md") -> Path:
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _index(result: dict) -> dict:
    return json.loads(Path(result["review_index"]).read_text(encoding="utf-8"))


def _verify_parts(result: dict, original: bytes, chunk_bytes: int = CHUNK_BYTES) -> dict:
    assert result["review_verified"] is True
    directory = Path(result["review_directory"])
    archive = Path(result["review_archive"])
    assert directory.is_dir() and archive.is_file()
    assert Path(result["review_index"]).parent == directory
    assert Path(result["review_recent"]).parent == directory
    index = _index(result)
    parts = index["parts"]
    assert result["review_part_count"] == len(parts)
    assert parts
    assert index["markdown"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert index["markdown"]["bytes"] == len(original)

    rebuilt = bytearray()
    expected_start = 0
    for part in parts:
        path = directory / part["filename"]
        payload = path.read_bytes()
        header_size = part["header_bytes"]
        header, body = payload[:header_size], payload[header_size:]
        assert header.endswith(MARKER)
        header.decode("utf-8", errors="strict")
        body.decode("utf-8", errors="strict")
        assert len(payload) == part["file_bytes"] <= chunk_bytes
        assert len(body) == part["body_bytes"]
        assert hashlib.sha256(payload).hexdigest() == part["sha256"]
        assert hashlib.sha256(body).hexdigest() == part["body_sha256"]
        assert part["source_byte_start"] == expected_start
        assert part["source_byte_end"] == expected_start + len(body)
        assert body == original[part["source_byte_start"]:part["source_byte_end"]]
        assert part["source_line_start"] == original[:expected_start].count(b"\n") + 1
        if body:
            end = part["source_byte_end"]
            expected_line_end = original[:end].count(b"\n") + int(original[end - 1:end] != b"\n")
            assert part["source_line_end"] == expected_line_end
        expected_start = part["source_byte_end"]
        rebuilt.extend(body)
    assert bytes(rebuilt) == original
    assert expected_start == len(original)

    expected_files = {p.name for p in directory.iterdir() if p.is_file()}
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        assert len(infos) == len(expected_files)
        assert {info.filename for info in infos} == expected_files
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos)
        assert not any(info.flag_bits & 1 for info in infos)
        assert zipped.testzip() is None
        for info in infos:
            assert "/" not in info.filename and "\\" not in info.filename
            assert zipped.read(info) == (directory / info.filename).read_bytes()
    return index


@pytest.mark.parametrize("raw", [
    b"",
    b"A complete line.\nA final line without LF.",
    b"\xef\xbb\xbfCRLF stays exact.\r\nAnother Windows line.\r\nFinal\r",
    ("Caf\u00e9 \u03b1\u03b2\u03b3 \U0001f9e0 \u4e2d\u6587\r\n" * 5_000 + "tail without newline \U0001f680").encode("utf-8"),
    ("\u4e2d\U0001f9e0" * 12_000).encode("utf-8"),
], ids=["empty", "no-final-newline", "bom-crlf", "unicode-lines", "one-long-unicode-line"])
def test_parts_reassemble_exact_utf8_bytes_with_source_offsets(tmp_path: Path, raw: bytes):
    markdown = _markdown(tmp_path, raw)
    result = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    _verify_parts(result, raw)
    assert markdown.read_bytes() == raw


def test_all_prose_code_and_binary_spelling_are_preserved(tmp_path: Path):
    # Long encoded strings and JSON/code can be meaningful evidence. Review
    # chunking must not silently run heuristic content suppression or redaction.
    raw = (b"# Evidence\n```json\n{\"payload\": \"" + b"A" * 75_000 + b"\"}\n```\n"
           b"![image](data:image/png;base64," + b"abcd" * 8_000 + b")\n"
           b"Repeated transcript line\n" * 100)
    markdown = _markdown(tmp_path, raw)
    result = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    index = _verify_parts(result, raw)
    assert len(index["parts"]) > 2


def test_recent_is_bounded_labeled_suffix_and_upload_guide_is_retained(tmp_path: Path):
    raw = b"OLD CONTEXT\n" + b"Historical record.\n" * 20_000 + b"LATEST VISIBLE RECORD\n"
    markdown = _markdown(tmp_path, raw)
    result = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    _verify_parts(result, raw)
    recent = Path(result["review_recent"]).read_bytes()
    assert len(recent) <= CHUNK_BYTES
    assert b"excerpt" in recent.lower()
    assert b"LATEST VISIBLE RECORD" in recent
    assert b"OLD CONTEXT" not in recent
    assert MARKER in recent
    body = recent.split(MARKER, 1)[1]
    assert raw.endswith(body)
    guide = (Path(result["review_directory"]) / "START_HERE.txt").read_text(encoding="utf-8")
    assert "RECENT.txt" in guide
    assert "review-index.json" in guide
    assert "parts" in guide
    assert "extract" in guide.lower() or "upload" in guide.lower()


@pytest.mark.parametrize("manifest", [
    {"source_sha256": SOURCE_DIGEST, "source": "C:/synthetic/rollout.jsonl",
     "source_snapshot": {"captured_sha256": SOURCE_DIGEST, "captured_size_bytes": 321}},
    {"source": {"sha256": SOURCE_DIGEST, "path": "C:/synthetic/rollout.jsonl"},
     "source_snapshot": {"captured_sha256": SOURCE_DIGEST, "captured_size_bytes": 321}},
], ids=["streaming", "buffered"])
def test_provenance_and_original_manifest_survive_without_native_jsonl(tmp_path: Path, manifest: dict):
    raw = b"# Rendered subset\nOnly selected messages belong here.\n"
    markdown = _markdown(tmp_path, raw)
    manifest_path = markdown.with_suffix(".manifest.json")
    manifest_raw = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_raw)
    result = review.create_review_bundle(markdown, manifest_path, chunk_bytes=CHUNK_BYTES)
    index = _verify_parts(result, raw)
    assert index["source_sha256"] == SOURCE_DIGEST
    assert (Path(result["review_directory"]) / "original-export.manifest.json").read_bytes() == manifest_raw
    assert markdown.read_bytes() == raw
    assert manifest_path.read_bytes() == manifest_raw
    assert not list(Path(result["review_directory"]).glob("*.jsonl"))


def test_existing_outputs_are_kept_and_collision_uses_new_pair(tmp_path: Path):
    markdown = _markdown(tmp_path, b"First version\n")
    first = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    first_zip = Path(first["review_archive"]).read_bytes()
    first_files = {p.name: p.read_bytes() for p in Path(first["review_directory"]).iterdir() if p.is_file()}
    markdown.write_bytes(b"Second version\n")
    second = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    assert first["review_archive"] != second["review_archive"]
    assert first["review_directory"] != second["review_directory"]
    assert Path(first["review_archive"]).read_bytes() == first_zip
    assert {p.name: p.read_bytes() for p in Path(first["review_directory"]).iterdir() if p.is_file()} == first_files
    _verify_parts(second, b"Second version\n")


@pytest.mark.parametrize("occupied", ["directory", "archive"])
def test_collision_when_only_one_output_path_exists(tmp_path: Path, occupied: str):
    markdown = _markdown(tmp_path, b"Transcript\n")
    existing_dir = tmp_path / "synthetic-thread.review"
    existing_zip = tmp_path / "synthetic-thread.review.zip"
    if occupied == "directory":
        existing_dir.mkdir()
        (existing_dir / "user-file.txt").write_bytes(b"keep")
    else:
        existing_zip.write_bytes(b"do not overwrite")
    result = review.create_review_bundle(markdown, chunk_bytes=CHUNK_BYTES)
    assert Path(result["review_directory"]) != existing_dir
    assert Path(result["review_archive"]) != existing_zip
    if occupied == "directory":
        assert (existing_dir / "user-file.txt").read_bytes() == b"keep"
    else:
        assert existing_zip.read_bytes() == b"do not overwrite"
    _verify_parts(result, b"Transcript\n")


def test_retrofit_lzma_reads_only_markdown_and_matching_manifest(tmp_path: Path, monkeypatch):
    raw = ("# Original export\r\nLossless Unicode \U0001f9e0\r\n" * 1_000).encode("utf-8")
    source = tmp_path / "old-lzma-export.zip"
    manifest_raw = json.dumps({"source_sha256": SOURCE_DIGEST}).encode("utf-8")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_LZMA) as zipped:
        zipped.writestr("thread.md", raw)
        zipped.writestr("thread.manifest.json", manifest_raw)
        zipped.writestr("thread.jsonl", b"Native image bytes must never be decoded for review." * 1_000)
    before = source.read_bytes()
    open_member = zipfile.ZipFile.open
    opened = []

    def forbid_native_read(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else str(name)
        opened.append(filename)
        assert not filename.endswith(".jsonl"), "Review conversion decoded the native snapshot"
        return open_member(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", forbid_native_read)
    result = review.review_existing_export(source, tmp_path / "converted", chunk_bytes=CHUNK_BYTES)
    index = _verify_parts(result, raw)
    assert index["source_sha256"] == SOURCE_DIGEST
    assert "thread.md" in opened and "thread.manifest.json" in opened
    assert source.read_bytes() == before
    assert not list((tmp_path / "converted").rglob("*.jsonl"))


@pytest.mark.parametrize("suffix", [".md", ".txt"])
def test_retrofit_loose_rendered_file_preserves_input(tmp_path: Path, suffix: str):
    raw = "# Plain review input\n\u4e2d\u6587 \U0001f9e0\n".encode("utf-8")
    source = _markdown(tmp_path, raw, "existing" + suffix)
    result = review.review_existing_export(source, tmp_path / "converted", chunk_bytes=CHUNK_BYTES)
    _verify_parts(result, raw)
    assert source.read_bytes() == raw


@pytest.mark.parametrize("members", [
    [("thread.md", b"A"), ("other.md", b"B")],
    [("thread.md", b"A"), ("thread.md", b"B")],
    [("thread.md", b"A"), ("../outside.jsonl", b"unread")],
    [("/absolute.md", b"A")],
    [("C:\\absolute.md", b"A")],
    [("folder/../thread.md", b"A")],
    [("thread.md", b"A"), ("thread.manifest.json", b"{}"), ("thread.manifest.json", b"{}")],
    [("thread.jsonl", b"native only")],
], ids=["two-markdowns", "duplicate-markdown", "unselected-traversal", "absolute", "windows-path", "traversal", "duplicate-manifest", "no-markdown"])
def test_retrofit_rejects_ambiguous_or_unsafe_inventory(tmp_path: Path, members: list[tuple[str, bytes]]):
    source = tmp_path / "untrusted.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, payload in members:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                zipped.writestr(name, payload)
    original = source.read_bytes()
    output = tmp_path / "converted"
    with pytest.raises((ValueError, RuntimeError, zipfile.BadZipFile)):
        review.review_existing_export(source, output, chunk_bytes=CHUNK_BYTES)
    assert source.read_bytes() == original
    assert not list(output.rglob("*.review.zip"))


def test_invalid_utf8_is_rejected_without_replacement_characters(tmp_path: Path):
    markdown = _markdown(tmp_path, b"Good prefix\nInvalid UTF-8: \xf0\x9f")
    output = tmp_path / "converted"
    with pytest.raises((UnicodeError, ValueError, RuntimeError)):
        review.create_review_bundle(markdown, output_dir=output, chunk_bytes=CHUNK_BYTES)
    assert markdown.read_bytes() == b"Good prefix\nInvalid UTF-8: \xf0\x9f"
    assert not list(output.rglob("*.review.zip"))


def test_retrofit_rejects_encrypted_members_before_attempting_read(tmp_path: Path):
    source = tmp_path / "encrypted-flag.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("thread.md", b"Synthetic transcript\n")
    # ZIP encryption flags live in both local and central headers. Modifying
    # these flags is sufficient to exercise inventory rejection without keys.
    payload = bytearray(source.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = payload.index(signature)
        flags = struct.unpack_from("<H", payload, start + offset)[0]
        struct.pack_into("<H", payload, start + offset, flags | 1)
    source.write_bytes(payload)
    with pytest.raises((ValueError, RuntimeError, zipfile.BadZipFile)):
        review.review_existing_export(source, tmp_path / "converted", chunk_bytes=CHUNK_BYTES)
    assert source.read_bytes() == payload


def test_retrofit_rejects_symlink_member(tmp_path: Path):
    source = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("thread.md")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(info, "../../outside-secret")
    with pytest.raises((ValueError, RuntimeError, zipfile.BadZipFile)):
        review.review_existing_export(source, tmp_path / "converted", chunk_bytes=CHUNK_BYTES)


@pytest.mark.parametrize("basename", ["../escape", "/absolute", "folder/name", "folder\\name", ".."])
def test_explicit_basename_cannot_escape_output_directory(tmp_path: Path, basename: str):
    markdown = _markdown(tmp_path, b"Synthetic transcript\n")
    with pytest.raises((ValueError, RuntimeError)):
        review.create_review_bundle(markdown, output_dir=tmp_path / "converted",
                                    basename=basename, chunk_bytes=CHUNK_BYTES)


@pytest.mark.parametrize("limit", [0, 4_095, 1_500_001, True, "1000000"])
def test_invalid_chunk_limits_are_rejected_before_writing(tmp_path: Path, limit):
    markdown = _markdown(tmp_path, b"Synthetic transcript\n")
    output = tmp_path / "converted"
    with pytest.raises(ValueError):
        review.create_review_bundle(markdown, output_dir=output, chunk_bytes=limit)
    assert not output.exists()


def test_minimum_chunk_size_keeps_header_and_recent_within_limit(tmp_path: Path):
    raw = ("Unicode 🧠 中文\r\n" * 2_000).encode("utf-8")
    markdown = _markdown(tmp_path, raw)
    result = review.create_review_bundle(markdown, chunk_bytes=4_096)
    _verify_parts(result, raw, chunk_bytes=4_096)
    assert Path(result["review_recent"]).stat().st_size <= 4_096


@pytest.mark.parametrize("name", ["thread:stream.md", "CON.md", "NUL.md", "LPT1.md", "thread?.md"])
def test_retrofit_rejects_windows_stream_and_device_names(tmp_path: Path, name: str):
    source = tmp_path / "unsafe-windows-name.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, b"This must not be extracted to a Windows device or stream.")
    with pytest.raises(ValueError):
        review.review_existing_export(source, tmp_path / "converted")
