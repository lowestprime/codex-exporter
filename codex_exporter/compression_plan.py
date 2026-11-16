"""Bounded, stratified codec probes for heterogeneous export members.

Samples guide range selection only. Compression with the requested settings and
independent size, CRC and SHA-256 checks remain authoritative. Small independent
samples may miss long-distance repetition; observed completed ranges calibrate
later predictions without changing the compression codec or its configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import zipfile

from .progress import ProgressReporter

SAMPLE_BYTES = 256 * 1024
MAX_SAMPLES = 16
BAND_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class CompressionBand:
    start: int
    end: int
    ratio: float


@dataclass(frozen=True)
class CompressionProfile:
    size: int
    bands: tuple[CompressionBand, ...]
    sample_bytes: int

    def estimate(self, offset: int = 0, count: int | None = None) -> float:
        end = self.size if count is None else min(self.size, offset + count)
        return sum(max(0, min(end, band.end) - max(offset, band.start)) * band.ratio
                   for band in self.bands)

    def range_bytes(self, offset: int, budget: float, *, correction: float = 1.0) -> int:
        """Predict a contiguous input range, with no claim of a hard byte bound."""
        if offset >= self.size:
            return 0
        if budget <= 0 or not math.isfinite(budget) or not math.isfinite(correction) or correction <= 0:
            raise ValueError('Compression planning requires a positive finite budget and correction')
        cursor = offset
        for band in self.bands:
            if band.end <= cursor:
                continue
            start = max(cursor, band.start)
            rate = max(band.ratio * correction, 0.000001)
            cost = (band.end - start) * rate
            if cost > budget:
                return max(1, start - offset + int(budget / rate))
            budget -= cost
            cursor = band.end
        return self.size - offset

    def receipt(self) -> dict:
        return {'sample_count': len(self.bands) if self.sample_bytes else 0, 'sample_bytes': self.sample_bytes,
                'estimated_compressed_bytes': round(self.estimate()),
                'minimum_sample_ratio': min(band.ratio for band in self.bands) if self.sample_bytes else None,
                'maximum_sample_ratio': max(band.ratio for band in self.bands) if self.sample_bytes else None,
                'scope': 'Stratified independent samples; planning estimate, not an output-size guarantee'}


def sample_profile(source: Path, name: str, *, work: Path, number: int,
                   max_bytes: int, compress, reporter=None) -> CompressionProfile:
    """Probe across the member with at most 4 MiB of input and one visible stage."""
    size = source.stat().st_size
    count = min(size, SAMPLE_BYTES, max(4096, max_bytes // 8))
    # Tiny members need no subprocess probes. Preserve the conservative bound.
    if size <= min(65_536, max_bytes // 4):
        return CompressionProfile(size, (CompressionBand(0, size, 1.02),), 0)
    samples = min(MAX_SAMPLES, max(1, (size + BAND_BYTES - 1) // BAND_BYTES), max(1, size // count))
    # Four positions also protect medium inputs with a misleading header.
    samples = min(max(samples, 4), MAX_SAMPLES, max(1, size // count))
    bands = []
    sampled = 0
    reporter = reporter or ProgressReporter(enabled=False)
    silent = ProgressReporter(enabled=False)
    with reporter.stage('Sample compression profile', total=samples, unit='samples',
                        detail=f'{name}; bounded samples across the complete member') as meter:
        for index in range(samples):
            start, end = size * index // samples, size * (index + 1) // samples
            length = min(count, end - start)
            # Include both endpoints. Interior cells use their midpoint.
            offset = (start if index == 0 else end - length if index == samples - 1
                      else start + (end - start - length) // 2)
            candidate = work / f'ratio-sample-{number:04d}-{index:02d}.zip'
            try:
                compress(candidate, name, source, offset, length, length * 2 + 4096,
                         reporter=silent, adaptive=False, stage_label='Sample compression ratio')
                with zipfile.ZipFile(candidate) as archive:
                    info = archive.getinfo(name)
                    ratio = max(info.compress_size / max(1, length), 0.000001)
                bands.append(CompressionBand(start, end, ratio))
                sampled += length
                meter.advance(1, detail=f'{name}; sample {index + 1}/{samples}; ratio {ratio:.3f}')
            except BaseException as primary:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    raise primary from cleanup_error
                raise
            else:
                candidate.unlink(missing_ok=True)
    return CompressionProfile(size, tuple(bands), sampled)
