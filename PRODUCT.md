# Product

## Register

product

## Users

A single local operator working at a personal editing workstation, often late at night or during a focused review session. They ingest long recordings, inspect transcripts, choose highlight candidates, and export short-form clips without sending media to a hosted editor. The job to be done is: turn a prepared local video into a small set of reviewable, downloadable clips with as little ceremony as possible.

## Product Purpose

Clipping Studio is the browser surface for the local Clipping AI backend. It makes the existing pipeline legible and controllable: prepare videos, transcribe them, detect highlights, choose what to export, render clips, and monitor work. Success means the operator can move from source video to rendered clips without guessing what the backend is doing, without leaving the local machine, and without losing the ability to cancel, retry, or recover work.

## Brand Personality

Calm, exacting, local-first. The interface should feel like a quiet control room and an editorial desk at the same time: signal-rich but not noisy, confident but never theatrical. It should reward attention with clear states, honest progress, and useful detail.

## Anti-references

- Generic SaaS admin templates with interchangeable metric cards.
- Neon "AI dashboard" visuals, gradient text, glassmorphism, and decorative charts.
- Consumer social-video editors that hide the source, invent effects, or imply editing controls the backend does not support.
- Dense developer consoles that expose raw JSON as the primary experience.

## Design Principles

1. Show the pipeline, not just the result. Every job should make its current stage, progress, and next possible action obvious.
2. Keep the source sacred. The UI never implies trimming, replacement audio, synthetic media, or edits that the backend cannot perform.
3. Make the local machine visible. Provider readiness, queue state, storage paths, and usage are part of the product, not hidden settings.
4. Prefer deliberate density over decorative space. The operator is reviewing real material and needs scannable lists, timecodes, and status at a glance.
5. Recovery is a first-class action. Cancel, retry, refresh, and re-run are always reachable when the job state allows them.

## Accessibility & Inclusion

Target WCAG 2.2 AA. Support keyboard-only navigation, visible focus, semantic landmarks, native dialogs, reduced motion, and text alternatives for status. Support English and Arabic interface direction with RTL layout, Arabic typography, and `dir="auto"` for transcript and highlight content. Never rely on color alone to communicate job status.
