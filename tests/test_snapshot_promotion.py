"""Only private verified snapshots may be promoted to exported source copies."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from codex_exporter import archive_pipeline
from codex_exporter.snapshot import SourceSnapshot, snapshot_source


@pytest.fixture
def original(tmp_path: Path) -> tuple[Path, bytes]:
    source = tmp_path / "original.jsonl"
    raw = b'{"type":"event_msg","payload":{"type":"agent_message","message":"captured"}}\n'
    source.write_bytes(raw)
    return source, raw


def test_hardlink_reuses_private_snapshot_without_linking_original(original, tmp_path):
    source, raw = original
    destination = tmp_path / "exported.jsonl"
    with snapshot_source(source, directory=tmp_path) as snapshot:
        digest, size, strategy = archive_pipeline._stage_snapshot_copy(snapshot, destination)
        assert strategy == "snapshot_hardlink"
        assert os.path.samefile(destination, snapshot.path)
        assert not os.path.samefile(destination, source)
        assert (digest, size) == (hashlib.sha256(raw).hexdigest(), len(raw))
        assert destination.read_bytes() == source.read_bytes() == raw
        private_path = snapshot.path
    assert not private_path.exists()
    assert destination.read_bytes() == source.read_bytes() == raw


def test_cross_device_link_failure_falls_back_to_verified_copy(original, tmp_path, monkeypatch):
    source, raw = original
    destination = tmp_path / "exported.jsonl"

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Synthetic cross-device link failure")

    with snapshot_source(source, directory=tmp_path) as snapshot:
        monkeypatch.setattr(archive_pipeline.os, "link", cross_device)
        digest, size, strategy = archive_pipeline._stage_snapshot_copy(snapshot, destination)
        assert strategy == "snapshot_copy"
        assert not os.path.samefile(destination, snapshot.path)
        assert not os.path.samefile(destination, source)
        assert (digest, size) == (hashlib.sha256(raw).hexdigest(), len(raw))
        assert destination.read_bytes() == raw
    assert destination.read_bytes() == source.read_bytes() == raw


def test_existing_destination_is_never_overwritten(original, tmp_path):
    source, raw = original
    destination = tmp_path / "exported.jsonl"
    previous = b"Previously exported bytes must remain intact."
    destination.write_bytes(previous)
    with snapshot_source(source, directory=tmp_path) as snapshot:
        with pytest.raises(FileExistsError):
            archive_pipeline._stage_snapshot_copy(snapshot, destination)
        assert snapshot.path.read_bytes() == raw
        assert destination.read_bytes() == previous
    assert source.read_bytes() == raw
    assert destination.read_bytes() == previous


def test_tampered_snapshot_is_rejected_before_promotion(original, tmp_path):
    source, raw = original
    destination = tmp_path / "exported.jsonl"
    with snapshot_source(source, directory=tmp_path) as snapshot:
        old = snapshot.path.stat()
        snapshot.path.write_bytes(raw.replace(b"captured", b"tampered"))
        os.utime(snapshot.path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with pytest.raises(RuntimeError, match="Snapshot content changed"):
            archive_pipeline._stage_snapshot_copy(snapshot, destination)
        assert not destination.exists()
    assert source.read_bytes() == raw


def test_forged_snapshot_cannot_promote_app_owned_original(original, tmp_path):
    source, raw = original
    destination = tmp_path / "exported.jsonl"
    forged = SourceSnapshot(
        path=source,
        original_path=source,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        metadata={"policy": "verified_prefix", "prefix_verified": True},
        _owned_directory=source.parent,
    )
    with pytest.raises(ValueError, match="owned private snapshot"):
        archive_pipeline._stage_snapshot_copy(forged, destination)
    assert not destination.exists()
    assert source.read_bytes() == raw
