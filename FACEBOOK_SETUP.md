# Facebook Page publishing (Reels)

Post a clip to one of your Facebook Pages as a Reel, from the clip page. Everything runs on
this computer. Reels go to a **Page**, not to a personal profile.

## What the app does

- **Connections** (Pipeline page, "Facebook" section): Connect, choose the Page, Disconnect.
- **Clip page**, "Facebook" panel: *Post now* or *Schedule on Facebook* (Facebook publishes at
  that time, even if this app is closed).
- Before anything is sent, a box names the Page and the time. Nothing is sent until you press
  the button in that box. The server also refuses any request without `confirm: true`.
- A clip that was already sent is refused (no double posts).
- Limit: Facebook allows 30 API-published Reels per Page per 24 hours. The app counts what it
  sent and stops at 30.
- Tokens are saved in `data/facebook/token.json` (permission 0600, git-ignored). No page or
  API route returns them.

## One-time setup: create the Meta app (about 10 minutes)

Meta changes its screens often. If a name below is different, look for the closest one.

1. Open <https://developers.facebook.com/apps/> with the Facebook account that is **admin of
   your Page**. Click **Create App**.
2. Give it any name (for example "My Clipping Tool"). When it asks for a use case, choose the
   option for managing Pages / "Other", and the app type **Business**.
3. In the app, add the product **Facebook Login** (it may be called *Facebook Login for
   Business*). Open its **Settings**.
4. Turn on **Client OAuth Login** and **Web OAuth Login**.
5. In **Valid OAuth Redirect URIs**, add exactly:
   `http://localhost:8765/api/facebook/callback`
   (change 8765 if you use another port). Save.
6. Go to **App settings > Basic**. Copy the **App ID** and **App Secret**.
7. Put them in `.env` next to the app:
   ```
   FACEBOOK_APP_ID=your_app_id
   FACEBOOK_APP_SECRET=your_app_secret
   ```
8. Keep the app in **Development** mode. You are its admin, so the permissions work for you
   without App Review. Do not switch it to Live.
9. Restart the server and open the app at `http://localhost:8765/ui/` (use `localhost`, the same
   name as in step 5). Pipeline > Facebook > **Connect**. Facebook asks which Pages to allow:
   select your Page, then continue.
10. Choose the Page in the list. Open a finished clip and use the Facebook panel.

If Connect fails with "URL blocked" or "redirect_uri": the address in step 5 does not match the
address in your browser bar. They must be the same, character by character.

## Permissions asked at sign-in

`pages_show_list`, `pages_manage_posts`, `pages_read_engagement`, `publish_video`.

## Not verified yet (first real test needed)

These come from Meta's documentation and search excerpts. They were not run against Facebook:

- The exact screens and names in the Meta dashboard (steps 2 to 5).
- That `http://localhost` is accepted as a redirect address in Development mode.
- The API version (`v21.0`). Change `FACEBOOK_API_VERSION` if Meta retires it.
- The scheduling window: at least 10 minutes and at most 29 days ahead. The app checks these;
  if Meta says otherwise its message is shown.
- Reel video limits (vertical 9:16, about 3 to 90 seconds). Clips exported with the vertical
  format should fit.

First real test: connect, post one clip as a scheduled Reel for 15 minutes ahead, then look at
the Page in Meta Business Suite > Content > Scheduled.

## Instagram (planned, not built)

Plan agreed in chat:

- Account type: Instagram **Creator** (or Business). Needed for publishing from tools.
- Same Meta app and the same Connections/clip-page design.
- **Post now** works anytime. **Timed posting** only happens while this app is running (Instagram
  has no server-side scheduling we can rely on). If a planned time passes while the app is
  closed, the app does not post by itself. It asks you first.
- Research says Instagram can take a local file upload, but this was not checked in Meta's own
  docs. Confirm the upload method before building.
