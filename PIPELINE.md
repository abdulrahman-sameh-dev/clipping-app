# Content pipeline (Sheet links, queue, daily review)

Open **Pipeline** in the web UI (`uv run clipping-ai serve`, then `/ui/#/pipeline`).

It has three sections:

1. **Daily review.** Suggestions computed from local data: new links waiting, ready clips that
   are not uploaded, failed clips, days without a scheduled release in the next 7 days, a failed
   or stale sheet sync. It only suggests. It never uploads, schedules or starts processing.
2. **Google Sheet.** One sheet link. Press **Sync now** to read it.
3. **Queue.** Every link from the sheet, stored in `pipeline.db` inside the data directory.
   Each link is `New`, `Sent` or `Skipped`. **Send to clipping** creates a normal video job for
   that link (same as Import URL). A link can be sent once. Syncing again only adds links
   that are not in the queue yet and never changes the status of existing ones.

## Sheet requirements

- Share the sheet as **Anyone with the link can view**, or publish it to the web. Private sheets
  are not supported (no Google login is used for this).
- Only `https://docs.google.com/spreadsheets/...` links are accepted. Nothing else is fetched.
- Columns: a header row is optional. Columns named `link`, `url`, `video`, `رابط` are used first;
  otherwise the first `http(s)` link found in the row is used. Optional `title` and `notes`
  columns are shown in the queue. A Google Form responses sheet works as is.
- At most 2000 rows and 2 MB are read per sync.

## API

`GET/PUT /api/pipeline/source`, `POST /api/pipeline/sync`, `GET /api/pipeline/items`,
`POST /api/pipeline/items/{id}/send|skip|restore`, `GET /api/pipeline/review`.

## Security

The local API has no authentication. Keep it on localhost. Anyone who can reach it can read the
queue and send links to clipping. The sheet link is stored in `pipeline.db` and is not a secret
unless you share the sheet with a private link: treat it like one.

## Not included yet

The review does not use YouTube Analytics (views, watch time). That needs a separate read-only
permission and is a later step.
