# Content pipeline (Sheet links, queue, daily review, auto-publish, analytics)

Open **Pipeline** in the web UI (`uv run clipping-ai serve`, then `/ui/#/pipeline`).

Sections:

1. **Daily review.** Suggestions computed from local data. It only suggests.
2. **Google Sheet.** One sheet link, read with your own Google sign-in (read-only). **Sync now**.
3. **Queue.** Every link from the sheet, stored in `pipeline.db` in the data directory.
4. **Clips and auto-publish.** Every ready clip with its publish state, a "skip auto-publish"
   box, and the auto-publish plan.
5. **YouTube Analytics.** A refresh button and rule-based suggestions.

## Duplicate links

- **Same link twice in the sheet, or on re-sync:** one queue item only. Links are compared by
  video, not by text: `youtu.be/ID`, `youtube.com/watch?v=ID&utm_source=x`, `/shorts/ID` and
  `/embed/ID` are the same video. Other sites: tracking parameters (`utm_*`, `fbclid`, ...),
  the fragment, `www.` and a trailing slash are ignored. Ignored duplicates are counted in the
  sync message. Existing items keep their status.
- **A link you already clipped:** **Send to clipping** does not start a second job. The library
  reuses the existing job when it is queued, processing or ready, the item is marked
  "Sent · Already in your library", and the message says how many clips exist.
- If the earlier job **failed** or was moved to the **trash**, sending the link creates a fresh
  job (that is the retry path).
- A link can be sent once per queue item.

## Private sheet (Google sign-in, read-only)

The sheet stays private. By default **nothing is read** until you sign in:

```
uv run clipping-ai pipeline auth sheets
```

This opens Google's consent page on this computer for **one read-only scope**
(`spreadsheets.readonly`). It uses the same OAuth client as YouTube uploads
(`YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` in `.env`). Also enable the **Google Sheets API**
for that Google Cloud project. The token is stored in `data/google/sheets_token.json`
(mode 0600, git-ignored) and is never returned by the API or shown in the UI.

- Use the normal sheet link (`.../spreadsheets/d/ID/edit#gid=N`); the tab from `gid` is read,
  otherwise the first tab. Published-to-web links are not used in login mode.
- Once signed in, the tool never falls back to the public CSV export.
- Link-shared sheets are only read when you tick "Also allow sheets shared by link". Off by
  default. Do not share a sheet with names or emails by link.
- Sheet columns: as before (a `link`/`url`/`video`/`رابط` column, otherwise the first link in
  each row; optional title and notes).

## Auto-publish

**Plan auto-publish** shows which ready clips would be uploaded and when. Nothing is uploaded
until you press **Confirm: upload privately and schedule**.

- Every clip is uploaded as **private with a publish time**; YouTube makes it public at that
  time. Nothing goes public immediately.
- Slots: one per day at the hour you set (default 18:00, this computer's time zone), skipping
  days that already have a scheduled release and never less than 30 minutes ahead. You can
  raise "per day" up to 5.
- At most **5 clips per press**. One upload costs about 1,600 of YouTube's default 10,000
  daily quota units. If YouTube reports the quota is used up or the sign-in fails, the rest of
  the batch is skipped.
- A clip is never part of a plan if it is already uploaded, is marked published by hand, or has
  "Skip auto-publish" ticked. A clip uploaded twice is refused by the upload layer too.
- Requires `CLIPPING_AI_YOUTUBE_ENABLED=true` and `uv run clipping-ai youtube auth`.

**Publish state** column (the marker that avoids mix-ups): Not uploaded, Marked published
(manual), **Scheduled** (with date), Went live (scheduled), Public, Unlisted, Private (no date).

There is no background publisher. Publishing happens only when you press the button.

## YouTube Analytics

```
uv run clipping-ai pipeline auth analytics
```

One read-only scope (`yt-analytics.readonly`); enable the **YouTube Analytics API** in the same
Cloud project. **Refresh analytics** reads views, watch time, likes and comments for the last
28 days for the videos this tool uploaded (limited to one refresh per minute). Suggestions are
simple rules, not AI:

- Only videos that are live and at least 2 days old are judged; at least 3 are needed.
- **Strong:** views at least 1.5x the median. Suggestion: make more content like it.
- **Weak:** views at most 0.5x the median. Suggestion: review title, hook or topic.
- **Low retention:** average watch time under 35% of the clip length. Suggestion: stronger
  first seconds.
- If all judged videos are weak, the suggestion is to change the type of content.

Nothing changes by itself based on these numbers.

## API

`GET/PUT /api/pipeline/source`, `PUT /api/pipeline/settings`, `POST /api/pipeline/sync`,
`GET /api/pipeline/items`, `POST /api/pipeline/items/{id}/send|skip|restore`,
`GET /api/pipeline/review`, `GET /api/pipeline/clips`, `PUT /api/pipeline/clips/{id}/exclude`,
`GET /api/pipeline/autopublish/plan`, `POST /api/pipeline/autopublish/run`,
`GET /api/pipeline/analytics`, `POST /api/pipeline/analytics/refresh`.

## Security

The local API has **no authentication**. Keep it on localhost. Anyone who can reach it can
read the queue, trigger a sync or analytics refresh, and press auto-publish (which uploads to
your channel). Auto-publish is limited to private scheduled uploads, 5 per run, with an explicit
confirm flag, but it is still your channel. Do not expose the port (no tunnel, no 0.0.0.0).
The Google tokens are local files; delete `data/google/` to sign out, or revoke access at
https://myaccount.google.com/permissions.
