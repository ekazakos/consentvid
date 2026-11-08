-- ConsentVid bot state (Cloudflare D1 / SQLite).

-- One row per video post the bot has handled (keyed by the video post it
-- links, so a video gets at most one reply however it was found).
CREATE TABLE replied (
  video_uri   TEXT PRIMARY KEY,
  video_cid   TEXT NOT NULL,
  target_uri  TEXT NOT NULL,     -- the post replied to (video post or mention)
  target_cid  TEXT NOT NULL,
  root_uri    TEXT NOT NULL,     -- thread root of the reply
  root_cid    TEXT NOT NULL,
  author_did  TEXT NOT NULL,
  status      TEXT NOT NULL,     -- done | dry-run | failed | skipped-consented | skipped-bot
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  reply_uri   TEXT,
  updated_at  TEXT NOT NULL
);

-- Key/value: cursors, rate-limit pause, daily global counter.
CREATE TABLE state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX replied_retry ON replied (status, attempts);

-- Replies per author per UTC day.
CREATE TABLE author_day (
  author_did TEXT NOT NULL,
  day        TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (author_did, day)
);

-- Cached session, so the bot doesn't call createSession every minute
-- (createSession is tightly rate-limited). Single row.
CREATE TABLE session (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  did         TEXT NOT NULL,
  handle      TEXT NOT NULL,
  pds         TEXT NOT NULL,
  access_jwt  TEXT NOT NULL,
  refresh_jwt TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
