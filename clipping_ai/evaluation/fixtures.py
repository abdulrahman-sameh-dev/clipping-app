"""Offline, versioned fixture corpus for highlight discovery evaluation."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from clipping_ai.evaluation.models import EvaluationCorpus, EvaluationFixture

CORPUS_VERSION = 1
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "v1"


def _context_truncation_fixture() -> EvaluationFixture:
    segments: list[dict[str, object]] = []
    filler = " ".join(["detail"] * 12)
    for index in range(2100):
        start = index * 6.0
        if index == 2095:
            text = (
                "Key decision insight: the final synthesis connects every earlier topic "
                "into one actionable conclusion and important takeaway."
            )
        else:
            text = f"marker {index} {filler}"
        segments.append(
            {
                "id": index + 1,
                "start": start,
                "end": start + (26.0 if index == 2095 else 4.0),
                "text": text,
            }
        )
    return EvaluationFixture.model_validate(
        {
            "fixture_id": "fixture-context-truncation",
            "content_type": "podcast",
            "language": "en",
            "duration": 12_600,
            "segments": segments,
            "gold_spans": [
                {
                    "start": 12_570,
                    "end": 12_596,
                    "topic": "synthesis",
                    "label": "final synthesis",
                }
            ],
            "topic_labels": ["synthesis"],
            "edge_cases": [
                "context_truncation",
                "long_multitopic",
                "late_moment",
                "no_signals",
                "topic_labels",
            ],
            "annotation_notes": [
                "The gold moment begins after both the character and segment visibility limits."
            ],
        }
    )


@lru_cache(maxsize=1)
def load_corpus(path: Path | None = None) -> EvaluationCorpus:
    corpus_dir = path or _FIXTURES_DIR
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads((corpus_dir / manifest["fixtures_file"]).read_text(encoding="utf-8"))
    corpus = EvaluationCorpus.model_validate(payload)
    if corpus.version != manifest["version"]:
        raise ValueError("fixture corpus version does not match manifest")
    corpus.fixtures.append(_context_truncation_fixture())
    return corpus
