from __future__ import annotations

import json

import pytest

from clipping_ai.errors import AIError
from clipping_ai.video.config import VideoConfig, load_video_config


def test_defaults_are_usable(tmp_path):
    config = load_video_config(tmp_path)
    assert config.enabled is True
    assert config.max_upload_bytes > 0
    assert ".mp4" in config.allowed_extensions
    assert config.audio_extension == ".flac"


def test_local_override_is_deep_merged(tmp_path):
    config_dir = tmp_path / "config"
    local_dir = config_dir / "local"
    local_dir.mkdir(parents=True)
    (config_dir / "video.json").write_text(
        json.dumps({"video": {"chunk_seconds": 30, "audio_codec": "wav"}}),
        encoding="utf-8",
    )
    (local_dir / "video.json").write_text(
        json.dumps({"video": {"chunk_seconds": 5}}),
        encoding="utf-8",
    )
    config = load_video_config(config_dir)
    assert config.chunk_seconds == 5
    assert config.audio_codec == "wav"
    assert config.audio_extension == ".wav"


def test_extensions_and_schemes_are_normalised():
    config = VideoConfig(allowed_extensions=["MP4", ".mkv", "mp4"], url_schemes=["HTTPS"])
    assert config.allowed_extensions == [".mp4", ".mkv"]
    assert config.url_schemes == ["https"]
    assert config.allows_extension(".MP4") is True


def test_invalid_config_is_rejected(tmp_path):
    (tmp_path / "video.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(AIError):
        load_video_config(tmp_path)


def test_invalid_values_are_rejected():
    with pytest.raises(ValueError):
        VideoConfig(chunk_seconds=0)
    with pytest.raises(ValueError):
        VideoConfig(chunk_overlap_seconds=10, chunk_seconds=10)
    with pytest.raises(ValueError):
        VideoConfig(allowed_extensions=[])
    with pytest.raises(ValueError):
        VideoConfig(audio_codec="")
