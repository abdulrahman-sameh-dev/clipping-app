from __future__ import annotations

from clipping_ai.evaluation.fixtures import load_corpus


def test_corpus_is_versioned_and_covers_required_edge_cases() -> None:
    corpus = load_corpus()
    assert corpus.version == 1
    assert corpus.labels_are_synthetic is True
    assert len(corpus.fixtures) >= 11

    edge_cases = {case for fixture in corpus.fixtures for case in fixture.edge_cases}
    required = {
        "long_multitopic",
        "continuous_explanation",
        "religious_context",
        "mixed_content",
        "arabic_rtl",
        "mixed_arabic_english",
        "quiet_low_motion",
        "late_moment",
        "few_highlights",
        "many_highlights",
        "adjacent_distinct",
        "context_truncation",
    }
    assert required <= edge_cases

    stress = next(
        fixture
        for fixture in corpus.fixtures
        if "context_truncation" in fixture.edge_cases
    )
    assert stress.transcript().text.__len__() > 24_000
    assert len(stress.segments) > 2_000


def test_each_fixture_has_stable_valid_annotations() -> None:
    corpus = load_corpus()
    ids = [fixture.fixture_id for fixture in corpus.fixtures]
    assert len(ids) == len(set(ids))

    for fixture in corpus.fixtures:
        assert fixture.fixture_id.startswith("fixture-")
        assert fixture.duration > 0
        assert fixture.segments
        assert fixture.gold_spans
        assert fixture.annotation_notes
        for span in fixture.gold_spans:
            assert 0 <= span.start < span.end <= fixture.duration
        for segment in fixture.segments:
            assert 0 <= segment.start <= segment.end <= fixture.duration
        if "topic_labels" in fixture.edge_cases:
            assert any(span.topic for span in fixture.gold_spans)


def test_corpus_load_is_deterministic() -> None:
    assert load_corpus() == load_corpus()
