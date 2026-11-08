# ConsentVid hashtag bot

A Cloudflare Worker that replies, once, to Bluesky video posts that ask for it.
The reply links to the consent page for that exact video:
`https://consentvid.org/app/?post=<at-uri>`.

**Status: built and tested, not deployed.** It ships with `DRY_RUN = "true"`.

## What it does

A cron trigger runs it every minute. Each run:

1. **Finds candidates:**
   - **Mentions** of the bot, from `app.bsky.notification.listNotifications`:
     - A mention in a video post links that post.
     - A mention in a reply under the author's **own** video post links the video post. The reply goes under the mention, and the thread root is kept.
     - A mention under someone else's video is ignored. Only the author can consent to their video.
   - **Posts tagged `#consentvid`** that carry a video, from `app.bsky.feed.searchPosts`:
     - The query is `q=#consentvid`, `tag=consentvid`, `sort=latest`, with a `since` window that overlaps the previous run by 15 minutes.
     - The tag counts whether it is a rich-text facet or in the record's `tags` field, in any case.
     - The video counts as `app.bsky.embed.video` or as the media of `recordWithMedia`.
2. **Skips:**
   - its own posts;
   - videos it already handled (D1 `replied`, keyed by the video post, so each video gets at most one reply however it was found);
   - authors carrying a `bot` label;
   - videos that already have an `org.consentvid.videoV2` record whose `#bskyPostRef` points at that post (read from the author's own PDS);
   - authors over the per-author daily limit.
3. **Replies:**
   - posts a proper reply, with UTF-8 byte-offset link facets, `langs: ["en"]`, under 300 graphemes;
   - or, with `DRY_RUN`, only logs the reply it would post.

**Limits** (all in `wrangler.toml` `[vars]`):

| Limit | Default |
|---|---|
| Replies per run | 20 |
| Replies per UTC day | 300 |
| Replies per author per day | 5 |

**Failure handling:**
- A failed post is retried up to 3 times.
- Candidates not reached because of a cap or a rate limit are kept for a later run.
- On HTTP 429 the bot pauses until `ratelimit-reset`, and the pause costs no attempt.

**Login:**
- The session is cached in D1. The bot calls `refreshSession` when the access token expires.
- It falls back to `createSession` at most once every 10 minutes, because createSession is tightly rate-limited.
- Calls go to the account's own PDS, and App View reads are proxied with `atproto-proxy`.

**Why polling instead of the firehose:** it is free and has no server to keep alive. Missed time is caught up automatically: if the Worker is down for an hour, the next run searches that hour.

## Cost

It runs on Cloudflare's **free plan**:

| Resource | Bot usage | Free-plan allowance |
|---|---|---|
| Worker requests | ~1,440 cron runs/day | 100,000 per day |
| D1 storage | A few rows per reply | 5 GB |
| D1 reads/writes | A few rows per reply | 5M reads, 100k writes per day |
| CPU time | Mostly waiting on the network, which doesn't count | 10 ms per run |

Bluesky's API is free. Posting is limited to about 1,600 records an hour, far above the daily cap.

## Wiring it up later

You need Node 22.13 or later and the Cloudflare account that already holds R2.

1. **Create the bot account** on bsky.app, for example with an email alias. Set a profile picture if you like.
2. **Use the domain as its handle:** in Bluesky, go to Settings → Handle → "I have my own domain" and enter `consentvid.org`.
   - Bluesky shows a TXT value.
   - In Namecheap, go to Domain List → consentvid.org → Advanced DNS, and add a TXT record:
     - Host: `_atproto`
     - Value: `did=did:plc:…`, exactly as Bluesky shows it.
   - Leave the existing `_lexicon` and `_github-pages-challenge-…` records alone.
   - Click "Verify" in Bluesky.
3. **App password:** in Bluesky, go to Settings → Privacy and security → App passwords → Add. Don't use the account password.
4. **Install and log in to Cloudflare:**
   ```sh
   cd bot
   npm ci
   npx wrangler login
   ```
5. **Create the database** and put the printed `database_id` into `wrangler.toml`:
   ```sh
   npx wrangler d1 create consentvid-bot
   npm run migrate        # wrangler d1 migrations apply consentvid-bot --remote
   ```
6. **Add the secrets.** They are stored encrypted by Cloudflare and never go in a file:
   ```sh
   npx wrangler secret put BOT_HANDLE         # consentvid.org
   npx wrangler secret put BOT_APP_PASSWORD   # the app password from step 3
   ```
7. **Label the account as a bot.** This adds the `bot` self-label and an "automated account" description, and keeps the other profile fields:
   ```sh
   BOT_HANDLE=consentvid.org BOT_APP_PASSWORD=… npm run setup-profile -- --dry-run   # preview
   BOT_HANDLE=consentvid.org BOT_APP_PASSWORD=… npm run setup-profile
   ```
8. **Deploy in dry-run mode first:**
   ```sh
   npm run deploy
   npx wrangler tail     # watch "DRY_RUN would reply to …" lines
   ```
   Post a test video tagged `#consentvid` from another account and check the log line.
9. **Go live:** set `DRY_RUN = "false"` in `wrangler.toml`, then run `npm run deploy` again. Posts that were only logged during the dry run still get their real reply.

To stop it, either:
- set `DRY_RUN = "true"` and deploy again; or
- remove the cron schedule from the Worker (`crons = []`) and deploy.

## Development

```sh
cd bot
npm ci --ignore-scripts
npm test                           # node:test; D1 is stood in by node:sqlite running the real migration
npx wrangler dev --test-scheduled  # local run: curl "http://localhost:8787/__scheduled"
```

- `src/bot.js`: one run (candidates, skips, caps, replies).
- `src/post.js`: video and tag detection, reply text and facets.
- `src/xrpc.js`: fetch-based XRPC, session cache and refresh, 429 handling, PDS resolution.
- `src/store.js`: D1 queries.
- `migrations/`: the D1 schema.
- `scripts/setup.mjs`: the one-time profile label.

**Session tokens:** the session tokens (access and refresh JWTs) are kept in the bot's D1 database, which only this Cloudflare account can read. The app password stays in Worker secrets. Revoking the app password in Bluesky invalidates them all.
