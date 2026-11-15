"""Portable synthetic archive backends; never inspect private user exports."""
from __future__ import annotations

import hashlib
import zipfile

import pytest


@pytest.fixture
def fake_range_compressor(monkeypatch):
    """Real ZIP/LZMA payloads with deterministic range overflow for unit tests."""
    from codex_exporter import archive_stream

    def compress(path, name, source, offset, count, limit, **kwargs):
        with source.open('rb') as stream:
            stream.seek(offset)
            content = stream.read(count)
        if len(content) != count:
            raise RuntimeError('Synthetic source was truncated')
        with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_LZMA) as archive:
            archive.writestr(name, content)
        size = path.stat().st_size
        if size > limit:
            path.unlink()
            raise archive_stream.RangeTooLarge(count, size, limit)
        return archive_stream.CompressedRange(count, hashlib.sha256(content).hexdigest())

    monkeypatch.setattr(archive_stream, 'compress_range', compress)
    return compress
