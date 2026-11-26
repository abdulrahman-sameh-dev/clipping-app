# YouTube upload and scheduling (optional)

This module is **off by default**. Out of the box the studio never talks to YouTube and
publishing stays manual. Turn it on only if you want to upload rendered clips from the
command line.

What it does:

- Uploads a rendered clip with the YouTube Data API v3 (`videos.insert`, resumable).
- Always sends the privacy status explicitly. The default is `private`.
- Schedules a release by uploading as `private` with `status.publishAt` set to a future
  time. YouTube then makes the video public at that time.
- Never changes the clip's manual `published` checkbox. You still tick it yourself.

What it does not do: no web UI or API endpoint yet (CLI only), no Facebook, no automatic
retries of whole uploads, no editing of videos already on YouTube.

## 1. Google Cloud setup (you do this yourself)

1. Open the Google Cloud console and create a project (or pick one).
2. Enable **YouTube Data API v3** for it (APIs and Services, Library).
3. Configure the **OAuth consent screen**. Add your own Google account as a test user if
   the app is in Testing mode.
4. Create an **OAuth client ID** of type **Desktop app**. Copy the client ID and secret.
5. Sign in with the Google account that owns the YouTube channel you upload to.

Notes you should check against Google's current docs before relying on them:

- Default quota is 100 `videos.insert` calls per day per project
  (<https://developers.google.com/youtube/data/guides/quota_and_compliance_audits>).
- Google's docs say uploads from unverified API projects are not forced to private.
  Because an unspecified privacy status defaults to public, this module always sets it.
- Apps left in Testing mode may have refresh tokens that expire after a short time. If
  uploads start failing with an authentication error, run `clipping-ai youtube auth`
  again.

## 2. Configure `.env`

Secrets live only in `.env` (git-ignored). Never commit them.

```bash
CLIPPING_AI_YOUTUBE_ENABLED=true
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
# Optional
# CLIPPING_AI_YOUTUBE_DEFAULT_PRIVACY=private
# CLIPPING_AI_YOUTUBE_MADE_FOR_KIDS=false
# CLIPPING_AI_YOUTUBE_TOKEN_PATH=data/youtube/token.json
```

`CLIPPING_AI_YOUTUBE_MADE_FOR_KIDS` sets YouTube's "made for kids" declaration for every
upload. It is your legal declaration, so set it to match your content.

## 3. Authorise once, locally

```bash
uv run clipping-ai youtube status
uv run clipping-ai youtube auth
```

`auth` prints a Google URL and opens it in your browser. It listens on `127.0.0.1` only
for the redirect, asks for the upload scope only (`youtube.upload`), and stores the token
in `data/youtube/token.json` with `0600` permissions.

## 4. Upload

Check the request first. `--dry-run` makes no network call.

```bash
uv run clipping-ai youtube upload CLIP_ID --dry-run
```

Upload privately (the default):

```bash
uv run clipping-ai youtube upload CLIP_ID
```

Schedule a release. The time needs a UTC offset, must be at least 15 minutes ahead and
no more than a year away, and the upload is forced to `private`:

```bash
uv run clipping-ai youtube upload CLIP_ID --publish-at 2026-12-01T18:00:00+02:00
```

Public or unlisted:

```bash
uv run clipping-ai youtube upload CLIP_ID --privacy unlisted
uv run clipping-ai youtube upload CLIP_ID --privacy public --confirm-public
```

Rules the module enforces before any request:

- Privacy is always sent explicitly.
- `--publish-at` only works with `private`, in the future, with an explicit offset.
- `public` needs `--confirm-public`.
- Trashed or not-ready clips are refused.
- A clip already uploaded is refused unless you pass `--force`. Uploads are recorded in
  `data/youtube/uploads.json` (video id and time only, no credentials).

## Troubleshooting

- `YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set`: add them to `.env`.
- `YouTube is not authorised yet`: run `clipping-ai youtube auth`.
- `quota ... used up`: wait for the daily quota reset or request more quota from Google.
- `invalidPublishAt`: the scheduled time was rejected. Pick a later time.
