from __future__ import annotations

import pytest

from clipping_ai.errors import AIError
from clipping_ai.video.audio import extract_audio, split_audio_file
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.metadata import probe_video
from tests.video_helpers import make_test_video, requires_ffmpeg


@requires_ffmpeg
def test_probe_video_normalises_metadata(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    metadata = probe_video(source, VideoConfig())
    assert metadata.duration_seconds == pytest.approx(2.0, abs=0.2)
    assert metadata.size_bytes > 0
    assert metadata.has_video is True
    assert metadata.has_audio is True
    assert metadata.video_streams[0].width == 160
    assert metadata.video_streams[0].height == 120
    assert metadata.audio_streams[0].sample_rate == 44100


@requires_ffmpeg
def test_probe_video_rejects_missing_audio(tmp_path):
    source = make_test_video(tmp_path / "video-only.mp4", with_audio=False)
    with pytest.raises(AIError) as exc_info:
        probe_video(source, VideoConfig())
    assert "audio" in exc_info.value.message.lower()


@requires_ffmpeg
def test_probe_video_rejects_missing_file_and_long_duration(tmp_path):
    with pytest.raises(AIError):
        probe_video(tmp_path / "missing.mp4", VideoConfig())
    source = make_test_video(tmp_path / "sample.mp4")
    with pytest.raises(AIError):
        probe_video(source, VideoConfig(max_duration_seconds=1))


@requires_ffmpeg
def test_audio_extraction_and_chunk_offsets(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    config = VideoConfig(chunk_seconds=1, chunk_overlap_seconds=0)
    audio = extract_audio(source, tmp_path / "audio" / "audio.flac", config)
    assert audio.exists()
    chunks = split_audio_file(audio, tmp_path / "audio" / "chunks", config, duration=2.0)
    assert len(chunks) == 2
    assert [chunk.index for chunk in chunks] == [0, 1]
    assert [chunk.offset for chunk in chunks] == [0.0, 1.0]
    assert all(chunk.path.exists() for chunk in chunks)
    assert all(chunk.duration == pytest.approx(1.0) for chunk in chunks)


def test_unsupported_audio_codec_is_rejected(tmp_path):
    config = VideoConfig(audio_codec="nope")
    with pytest.raises(AIError):
        extract_audio(tmp_path / "x.mp4", tmp_path / "x.flac", config)
