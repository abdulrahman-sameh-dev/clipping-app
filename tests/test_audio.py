from __future__ import annotations

import shutil
import subprocess

import pytest

from clipping_ai.audio import hash_file, probe_duration, split_audio
from clipping_ai.errors import AIError


def test_hash_file_is_stable(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello")
    assert hash_file(path) == hash_file(path)
    assert len(hash_file(path)) == 64


def test_invalid_chunk_size_is_rejected(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello")
    with pytest.raises(AIError):
        split_audio(path, chunk_seconds=0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_splits_audio_and_preserves_offsets(tmp_path):
    source = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            str(source),
        ],
        check=True,
    )
    duration = probe_duration(source)
    assert duration is not None
    chunks = split_audio(source, chunk_seconds=1, output_dir=tmp_path / "chunks")
    assert len(chunks) == 2
    assert chunks[0].offset == 0.0
    assert chunks[1].offset == 1.0
    assert all(chunk.path.exists() for chunk in chunks)
