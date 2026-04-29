"""AppView: subscribes to ATproto Jetstream and indexes consentvid records into SQLite.

Jetstream is Bluesky's JSON-formatted firehose: a WebSocket that streams every
commit on the network as a JSON message. We filter to org.consentvid.* and
upsert into a local SQLite database.

Database lives at ~/.consentvid/appview.db.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import websockets


JETSTREAM_URL = "wss://jetstream2.us-east.bsky.network/subscribe"
VIDEO_NSID = "org.consentvid.video"
CONSENT_NSID = "org.consentvid.consent"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  uri          TEXT PRIMARY KEY,
  did          TEXT NOT NULL,
  rkey         TEXT NOT NULL,
  cid          TEXT NOT NULL,
  title        TEXT,
  description  TEXT,
  content_hash TEXT,
  duration_ms  INTEGER,
  width        INTEGER,
  height       INTEGER,
  language     TEXT,
  tags         TEXT,
  storage_ref  TEXT,
  created_at   TEXT,
  indexed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
  uri           TEXT PRIMARY KEY,
  did           TEXT NOT NULL,
  rkey          TEXT NOT NULL,
  cid           TEXT NOT NULL,
  video_uri     TEXT NOT NULL,
  modalities    TEXT,
  purposes      TEXT,
  model_classes TEXT,
  expires_at    TEXT,
  compensation  TEXT,
  revocable     INTEGER,
  created_at    TEXT,
  indexed_at    TEXT NOT NULL,
  revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS consents_video_uri ON consents(video_uri);
CREATE INDEX IF NOT EXISTS consents_did       ON consents(did);
CREATE INDEX IF NOT EXISTS videos_did         ON videos(did);
"""


def db_path() -> Path:
    return Path.home() / ".consentvid" / "appview.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_video(conn, uri, did, rkey, cid, value):
    conn.execute(
        """
        INSERT INTO videos (uri, did, rkey, cid, title, description, content_hash,
                            duration_ms, width, height, language, tags, storage_ref,
                            created_at, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, title=excluded.title, description=excluded.description,
          content_hash=excluded.content_hash, duration_ms=excluded.duration_ms,
          width=excluded.width, height=excluded.height, language=excluded.language,
          tags=excluded.tags, storage_ref=excluded.storage_ref,
          created_at=excluded.created_at, indexed_at=excluded.indexed_at
        """,
        (
            uri, did, rkey, cid,
            value.get("title"), value.get("description"), value.get("contentHash"),
            value.get("durationMs"), value.get("width"), value.get("height"),
            value.get("language"),
            json.dumps(value.get("tags") or []),
            json.dumps(value.get("storageRef") or {}),
            value.get("createdAt"),
            _now(),
        ),
    )


def upsert_consent(conn, uri, did, rkey, cid, value):
    conn.execute(
        """
        INSERT INTO consents (uri, did, rkey, cid, video_uri, modalities, purposes,
                              model_classes, expires_at, compensation, revocable,
                              created_at, indexed_at, revoked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, video_uri=excluded.video_uri,
          modalities=excluded.modalities, purposes=excluded.purposes,
          model_classes=excluded.model_classes, expires_at=excluded.expires_at,
          compensation=excluded.compensation, revocable=excluded.revocable,
          created_at=excluded.created_at, indexed_at=excluded.indexed_at,
          revoked_at=NULL
        """,
        (
            uri, did, rkey, cid,
            value.get("videoUri"),
            json.dumps(value.get("modalities") or []),
            json.dumps(value.get("purposes") or []),
            json.dumps(value.get("modelClasses") or []),
            value.get("expiresAt"),
            value.get("compensation", "none"),
            1 if value.get("revocable") else 0,
            value.get("createdAt"),
            _now(),
        ),
    )


def mark_deleted(conn, uri: str) -> None:
    """Mark a consent record revoked, or remove a video record entirely."""
    conn.execute("UPDATE consents SET revoked_at = ? WHERE uri = ?", (_now(), uri))
    conn.execute("DELETE FROM videos WHERE uri = ?", (uri,))


async def index_loop(
    conn,
    on_event: Callable[[str, str], None] | None = None,
    stop_after: int | None = None,
) -> int:
    """Subscribe to Jetstream and persist org.consentvid.* events. Runs forever
    unless stop_after is set (process N events then return)."""
    params = "&".join(
        f"wantedCollections={c}" for c in (VIDEO_NSID, CONSENT_NSID)
    )
    url = f"{JETSTREAM_URL}?{params}"
    n = 0
    async with websockets.connect(url, ping_interval=30) as ws:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("kind") != "commit":
                continue
            commit = event.get("commit", {})
            collection = commit.get("collection")
            rkey = commit.get("rkey")
            did = event.get("did")
            op = commit.get("operation")
            if not (collection and rkey and did and op):
                continue
            uri = f"at://{did}/{collection}/{rkey}"
            if op in ("create", "update"):
                cid = commit.get("cid", "")
                value = commit.get("record", {}) or {}
                if collection == VIDEO_NSID:
                    upsert_video(conn, uri, did, rkey, cid, value)
                elif collection == CONSENT_NSID:
                    upsert_consent(conn, uri, did, rkey, cid, value)
                else:
                    continue
            elif op == "delete":
                mark_deleted(conn, uri)
            else:
                continue
            conn.commit()
            n += 1
            if on_event:
                on_event(op, uri)
            if stop_after and n >= stop_after:
                break
    return n
