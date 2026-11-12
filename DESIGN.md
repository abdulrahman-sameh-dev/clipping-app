# Design

## Visual Theme

A dim editing room with a phosphor-warm signal palette. The surface is a green-tinted charcoal, not blue-black, with sodium-orange as the action color and a cool teal for live/system signals. Editorial serif headings sit against a precise sans-serif interface. The result should feel instrument-like and human, not like a generic analytics product.

## Color

All colors use OKLCH. The dark theme is the default because the primary scene is a long, focused local review session.

| Token | Value | Role |
| --- | --- | --- |
| `--bg` | `oklch(0.145 0.012 160)` | App background |
| `--bg-elevated` | `oklch(0.175 0.014 160)` | Sidebar, top bar |
| `--surface` | `oklch(0.205 0.015 160)` | Panels and rows |
| `--surface-2` | `oklch(0.24 0.018 160)` | Inputs, raised controls |
| `--line` | `oklch(0.34 0.02 160 / 0.6)` | Dividers and borders |
| `--ink` | `oklch(0.94 0.01 90)` | Primary text |
| `--ink-2` | `oklch(0.78 0.015 90)` | Secondary text |
| `--muted` | `oklch(0.64 0.018 90)` | Metadata, placeholders |
| `--accent` | `oklch(0.76 0.16 55)` | Primary actions, selected state |
| `--accent-ink` | `oklch(0.18 0.03 55)` | Text on accent |
| `--teal` | `oklch(0.74 0.12 190)` | System/live signals |
| `--green` | `oklch(0.76 0.14 150)` | Ready/success |
| `--red` | `oklch(0.68 0.18 25)` | Failure/destructive |
| `--yellow` | `oklch(0.82 0.14 95)` | Waiting/warning |

Status always pairs color with a label and a dot or icon.

## Typography

- UI: `"Inter", "Segoe UI", system-ui, -apple-system, sans-serif`
- Display: `"Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif`
- Mono: `"JetBrains Mono", "SFMono-Regular", Consolas, monospace`
- Arabic: `"Noto Sans Arabic", "Segoe UI", Tahoma, sans-serif`

Display headings use the serif at weights 600–700 with `text-wrap: balance`. Body copy is capped near 70ch. Timecodes, IDs, and byte counts use the mono stack.

## Layout

- Desktop: fixed 248px navigation rail, fluid content column with a 1440px maximum.
- Tablet: navigation becomes a horizontal strip or an off-canvas rail.
- Mobile: off-canvas navigation, single-column panels, stacked actions, horizontally scrollable filter rows.
- Major surfaces use open panels and ruled rows rather than nested cards. Cards are reserved for discrete media objects such as clips and highlight candidates.
- Spacing follows a 4px base with rhythm steps at 8, 12, 16, 24, 32, 48, and 64.

## Components

- `button`: primary, secondary, ghost, danger; consistent 40px height; icon+label support.
- `status-pill`: dot + text; queued, processing, ready, failed, cancelled.
- `signal-rail`: horizontal readout of service, provider, queue, and library state.
- `panel`: bordered surface with a header row and optional footer.
- `job-row`: title, source, duration, status, progress, timestamp, actions.
- `highlight-card`: index, title, timecode, confidence meter, category, excerpt, selection control.
- `clip-card`: media preview, format badge, timecode, quality, status, actions.
- `progress`: determinate bar with stage label; indeterminate for unknown.
- `empty-state`: short explanation plus one primary next action.
- `dialog`: native `<dialog>` for upload, URL import, and clip preview.
- `toast`: short-lived status feedback in a live region.

## Motion

- Page content enters with a short opacity/translate transition; lists stagger only when it helps orientation.
- Progress bars animate width with an ease-out curve.
- Clip previews may play muted on hover when the pointer is fine and motion is allowed.
- Every animation is disabled or reduced under `prefers-reduced-motion: reduce`.
- No bounce, elastic, parallax, or layout-thrashing animation.

## Imagery

No stock imagery. Media is represented by the actual rendered clip when available, or by a restrained format diagram with timecode and format label when it is not. The UI never fabricates thumbnails or source frames.
