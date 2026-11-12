from __future__ import annotations

from fastapi.testclient import TestClient

from clipping_ai.analysis.config import AnalysisConfig, HighlightSettings
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.api.app import create_app
from tests.video_helpers import make_service, make_test_video, requires_ffmpeg


class FakeManager:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.transcribe_calls = 0

    def transcribe(self, *args, **kwargs):
        self.transcribe_calls += 1
        return {
            "provider": "whisper_cpp",
            "model": "whisper-tiny",
            "transcript": {
                "text": "Why did the game change? Because we won the final round!",
                "language": "en",
                "duration": 2.0,
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "Why did the game change?"},
                    {"start": 1.0, "end": 2.0, "text": "Because we won the final round!"},
                ],
            },
        }

    def analyze(self, *args, **kwargs):
        return {"data": {"highlights": []}}


@requires_ffmpeg
def test_analysis_endpoints(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    video = make_service(tmp_path)
    manager = FakeManager(video.settings)
    analysis = AnalysisService(
        video.settings,
        config=AnalysisConfig(
            highlights=HighlightSettings(
                min_clip_seconds=1.0,
                max_clip_seconds=5.0,
                candidate_window_seconds=5.0,
                candidate_stride_seconds=2.0,
                use_llm=False,
            )
        ),
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
    )
    app = create_app(
        manager,  # type: ignore[arg-type]
        video_service=video,
        analysis_service=analysis,
        start_worker=False,
    )
    client = TestClient(app)

    upload = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", source.read_bytes(), "video/mp4")},
    )
    assert upload.status_code == 200
    job_id = upload.json()["job"]["id"]
    video.process(job_id)

    config = client.get("/api/analysis/config")
    assert config.status_code == 200
    assert "transcription" in config.json()
    assert "highlights" in config.json()

    missing = client.get(f"/api/video/jobs/{job_id}/transcript")
    assert missing.status_code == 200
    assert missing.json()["transcript"] is None

    transcribed = client.post(
        f"/api/video/jobs/{job_id}/transcribe",
        json={"language": "en"},
    )
    assert transcribed.status_code == 200
    assert transcribed.json()["job"]["transcript"]["language"] == "en"
    assert manager.transcribe_calls == 1

    transcript = client.get(f"/api/video/jobs/{job_id}/transcript")
    assert transcript.status_code == 200
    assert transcript.json()["transcript_provider"] == "whisper_cpp"

    highlights = client.post(
        f"/api/video/jobs/{job_id}/highlights",
        json={"content_type": "podcast", "use_llm": False},
    )
    assert highlights.status_code == 200
    result = highlights.json()["job"]["highlight_result"]
    assert result["content_type"] == "podcast"
    assert result["method"] == "deterministic"
    assert result["classification"]["content_type"] == "podcast"
    assert result["classification"]["confidence"] == 1.0

    stored = client.get(f"/api/video/jobs/{job_id}/highlights")
    assert stored.status_code == 200
    assert stored.json()["highlight_result"]["highlights"]
    assert "detection_signals" in stored.json()["highlight_result"]["highlights"][0]

    analyzed = client.post(
        f"/api/video/jobs/{job_id}/analyze",
        json={"use_llm": False},
    )
    assert analyzed.status_code == 200
    assert analyzed.json()["job"]["transcript"] is not None
    assert manager.transcribe_calls == 1

    invalid = client.post(
        f"/api/video/jobs/{job_id}/highlights",
        json={"content_type": "invalid"},
    )
    assert invalid.status_code == 422
