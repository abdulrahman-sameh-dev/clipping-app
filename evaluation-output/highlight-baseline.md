# Offline Highlight Discovery Baseline

SYNTHETIC EVALUATION RESULTS ONLY. These numbers are not real-world performance.

## 1. Dataset and fixture coverage
Corpus version: 1. Fixtures: 12.
Fixtures are synthetic or manually authored; no private videos or copyrighted transcripts were used.

## 2. Metric definitions and limitations
- **span_iou**: Intersection divided by union of two absolute-time spans; invalid intervals score zero.
- **precision**: Matched predictions divided by valid predictions; 1.0 when both prediction and gold sets are empty.
- **recall**: Matched gold spans divided by valid gold spans; 1.0 when gold is empty.
- **f1**: Harmonic mean of precision and recall.
- **boundary_error**: Absolute start and end timestamp differences for greedily matched spans.
- **quartile_coverage**: Selected span seconds intersecting each quartile divided by gold seconds in that quartile, capped at 1.0.
- **temporal_distribution**: Counts and seconds of selected spans by midpoint quartile.
- **redundancy**: Pairwise IoU among selected spans, including count of pairs with positive overlap.
- **distinct_topic_coverage**: Fraction of authored gold topics with at least one matched selected span.
- **candidate_survival**: Stage after-count divided by stage before-count and by the largest stage before-count.
- **context_truncation**: Difference between full transcript text/segments and the first 24,000 characters or first 2,000 segments.
- **cap_statistics**: Candidates hidden by the 12-candidate LLM cap and highlights hidden by the max_highlights cap.
- **fallback_frequency**: Fraction of canned LLM calls that produced no usable highlights and used deterministic fallback.

## 3. Per-content-type results
| content_type | fixtures | precision | recall | f1 | redundancy |
| --- | --- | --- | --- | --- | --- |
| educational | 6 | 0.409 | 1.000 | 0.569 | 0.000 |
| mixed | 2 | 0.500 | 1.000 | 0.661 | 0.000 |
| podcast | 4 | 0.500 | 0.679 | 0.507 | 0.000 |

## 4. Aggregate results
Precision 0.454; recall 0.893; F1 0.563; topic coverage 0.893.
Fallback calls: 0/12.

## 5. Temporal coverage
| quartile | gold_seconds | selected_seconds | coverage_ratio |
| --- | --- | --- | --- |
| 1 | 485.0 | 1440.0 | 1.000 |
| 2 | 800.0 | 1466.0 | 1.000 |
| 3 | 775.0 | 1006.0 | 1.000 |
| 4 | 631.0 | 1200.0 | 1.000 |

### Per-fixture metrics
| fixture | type | precision | recall | f1 | hidden_segments | llm_hidden | final_hidden |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fixture-long-multitopic | podcast | 0.833 | 1.000 | 0.909 | 0 | 0 | 0 |
| fixture-educational-continuous | educational | 0.286 | 1.000 | 0.444 | 0 | 0 | 0 |
| fixture-religious-lesson-context | educational | 0.333 | 1.000 | 0.500 | 0 | 0 | 0 |
| fixture-general-mixed | mixed | 0.600 | 1.000 | 0.750 | 0 | 0 | 0 |
| fixture-arabic-rtl | educational | 0.333 | 1.000 | 0.500 | 0 | 0 | 0 |
| fixture-mixed-arabic-english | mixed | 0.400 | 1.000 | 0.571 | 0 | 0 | 0 |
| fixture-quiet-low-motion | educational | 0.667 | 1.000 | 0.800 | 0 | 0 | 0 |
| fixture-late-moment | podcast | 0.167 | 1.000 | 0.286 | 0 | 0 | 0 |
| fixture-few-highlights | educational | 0.333 | 1.000 | 0.500 | 0 | 0 | 0 |
| fixture-many-highlights | podcast | 1.000 | 0.714 | 0.833 | 0 | 2 | 2 |
| fixture-adjacent-distinct | educational | 0.500 | 1.000 | 0.667 | 0 | 0 | 0 |
| fixture-context-truncation | podcast | 0.000 | 0.000 | 0.000 | 100 | 408 | 0 |

## 6. Candidate survival
| stage | before | after | cumulative_survival |
| --- | --- | --- | --- |
| transcript_candidates | 0 | 551 | 0.348 |
| signal_candidates | 551 | 1583 | 1.000 |
| candidate_dedupe | 1583 | 1024 | 0.647 |
| confidence_duration_filter | 1024 | 485 | 0.306 |
| llm_candidate_cap | 485 | 75 | 0.047 |
| semantic_consolidation | 74 | 65 | 0.041 |
| long_highlight_split | 65 | 65 | 0.041 |
| highlight_dedupe | 65 | 65 | 0.041 |
| final_highlight_cap | 65 | 63 | 0.040 |

## 7. Context truncation
| limit | configured | observed | interpretation |
| --- | --- | --- | --- |
| llm_transcript_chars | 24000 | 200519 | Late context cannot be selected by the single LLM pass. |
| llm_segments | 2000 | 2100 | Segment visibility ends before the end of a long video. |
| llm_candidates | 12 | 420 | Candidate caps are global in the current single-pass path. |
| final_highlights | 10 | 12 | Output selection cannot express more than the configured cap. |
Aggregate hidden text characters: 176519. Aggregate hidden segments: 100.

## 8. Single-pass versus simulated multi-pass
| fixture | single_recall | multipass_recall | single_llm_candidates | multipass_discovered |
| --- | --- | --- | --- | --- |
| fixture-long-multitopic | 1.000 | 1.000 | 6 | 12 |
| fixture-educational-continuous | 1.000 | 1.000 | 7 | 16 |
| fixture-religious-lesson-context | 1.000 | 1.000 | 6 | 14 |
| fixture-general-mixed | 1.000 | 1.000 | 5 | 10 |
| fixture-arabic-rtl | 1.000 | 1.000 | 6 | 14 |
| fixture-mixed-arabic-english | 1.000 | 1.000 | 5 | 10 |
| fixture-quiet-low-motion | 1.000 | 1.000 | 3 | 6 |
| fixture-late-moment | 1.000 | 1.000 | 6 | 12 |
| fixture-few-highlights | 1.000 | 1.000 | 3 | 6 |
| fixture-many-highlights | 0.714 | 0.714 | 12 | 28 |
| fixture-adjacent-distinct | 1.000 | 1.000 | 4 | 8 |
| fixture-context-truncation | 0.000 | 1.000 | 12 | 441 |
Aggregate recall delta: 0.083.

## 9. Recommended window configuration
Window seconds: 600.0. Overlap seconds: 0.0.
Selected by the deterministic experiment score: mean F1, 0.25 times gold-span match ratio, minus 0.1 times redundancy, minus a small window-count penalty.
| window_seconds | overlap | f1 | recall | redundancy | windows | score |
| --- | --- | --- | --- | --- | --- | --- |
| 120.0 | 0.0 | 0.563 | 0.893 | 0.000 | 257 | 0.777036 |
| 120.0 | 30.0 | 0.563 | 0.893 | 0.000 | 342 | 0.776186 |
| 120.0 | 60.0 | 0.563 | 0.893 | 0.000 | 500 | 0.774606 |
| 300.0 | 0.0 | 0.579 | 0.976 | 0.000 | 104 | 0.800474 |
| 300.0 | 30.0 | 0.579 | 0.976 | 0.000 | 120 | 0.800314 |
| 300.0 | 60.0 | 0.563 | 0.893 | 0.000 | 131 | 0.778296 |
| 600.0 | 0.0 | 0.579 | 0.976 | 0.000 | 54 | 0.800974 |
| 600.0 | 30.0 | 0.563 | 0.893 | 0.000 | 59 | 0.779016 |
| 600.0 | 60.0 | 0.579 | 0.976 | 0.000 | 61 | 0.800904 |

## 10. Known limitations
- The corpus is small and cannot estimate production quality across real content distributions.
- Boundary errors are measured only for matched spans; unmatched moments are not localized.
- Greedy IoU matching can prefer a slightly better overlapping pair over a globally optimal assignment.
- Religious fixtures test context-preservation structure only; no religious profile is implemented.
- Simulated multi-pass discovery reuses deterministic candidates and does not exercise a real LLM.

## Interpretation warnings
- SYNTHETIC EVALUATION ONLY: results are not real-world performance measurements.
- Gold spans are authored annotations for controlled scenarios, not objective human judgments.
- Canned LLM responses simulate selection from visible candidates; no live provider was called.
- Topic labels are diagnostics and were not supplied to production ranking.
- Sparse transcripts lower segment-coverage ratios by design and do not measure ASR quality.

## Proposed multi-pass architecture
- Partition the complete transcript into sequential windows with configurable overlap.
- Preserve absolute timestamps for every segment and candidate; never rebase window-local times.
- Analyze each window independently with bounded text, segment, and candidate input.
- Persist window status, attempts, fingerprint, and candidate artifact in the existing StageStore.
- Resume from the first incomplete or invalid window while retaining completed window results.
- Aggregate all successful window candidates before global deduplication and semantic consolidation.
- Apply final ranking, diversity selection, and max_highlights only after full-video discovery.
- Treat 24,000 characters and 2,000 segments as per-window limits, not global discovery limits.

## Future implementation breakdown
- Define durable window stage fingerprints and deterministic window IDs.
- Add a single-pass-compatible multi-pass runner behind an explicit opt-in setting.
- Persist per-window artifact metadata through StageStore without a second job system.
- Add global aggregation, cross-window deduplication, and diversity selection.
- Add provider-budget accounting and context-error fallback for window-level requests.
- Verify parity for existing single-pass users and progressive rollout behavior.
