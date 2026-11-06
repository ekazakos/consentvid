"""AppView: index org.consentvid.* records from the whole network into SQLite.

Subscribes to Jetstream (Bluesky's JSON firehose), validates every record on
ingest, and keeps a queryable index at ~/.consentvid/appview.db. The index is a
discovery tool: Jetstream strips repo signatures, so before *using* clips found
here, export a manifest and run ``consentvid resync`` (which verifies proofs).

Revocation semantics in the index:
* consent record deleted          -> consent revoked
* video record deleted            -> video removed, its consents revoked
* account deleted                 -> all its consents revoked
* account deactivated/taken down  -> excluded from queries until reactivated
* consent rewritten into an invalid record -> revoked
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

import websockets

from consentvid.records import CONSENT_NSID, VIDEO_NSID, format_dt, normalize_dt, now_iso
from consentvid.validate import validate_in_repo

JETSTREAM_URL = os.environ.get("CONSENTVID_JETSTREAM_URL", "wss://jetstream2.us-east.bsky.network/subscribe")
CURSOR_KEY = "jetstream_cursor"
# On resume, rewind to cover events in flight when we stopped. Replays are
# harmless: every write below is idempotent.
CURSOR_REWIND_US = 5_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  uri TEXT PRIMARY KEY, did TEXT NOT NULL, rkey TEXT NOT NULL, cid TEXT NOT NULL,
  title TEXT, content_hash TEXT, duration_ms INTEGER, tags TEXT,
  created_at TEXT, indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
  uri TEXT PRIMARY KEY, did TEXT NOT NULL, rkey TEXT NOT NULL, cid TEXT NOT NULL,
  video_uri TEXT NOT NULL, purposes TEXT, model_classes TEXT, modalities TEXT,
  expires_at TEXT, created_at TEXT, indexed_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  did TEXT PRIMARY KEY, active INTEGER NOT NULL, status TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS consents_video_uri ON consents(video_uri);
CREATE INDEX IF NOT EXISTS consents_did ON consents(did);
CREATE INDEX IF NOT EXISTS videos_did ON videos(did);
"""

# Columns added after the first release; _migrate adds any that are missing.
_LATER_COLUMNS = {
    "videos": [
        ("description", "TEXT"),
        ("width", "INTEGER"),
        ("height", "INTEGER"),
        ("language", "TEXT"),
        ("storage_ref", "TEXT"),
        ("mime_type", "TEXT"),
        ("record", "TEXT"),
    ],
    "consents": [
        ("compensation", "TEXT"),
        ("revocable", "INTEGER"),
        ("authorship_attested", "INTEGER"),
        ("rights_basis", "TEXT"),
        ("capture_provenance", "TEXT"),
        ("video_cid", "TEXT"),
        ("revoke_reason", "TEXT"),
        ("record", "TEXT"),
    ],
}


def db_path() -> Path:
    return Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid")) / "appview.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    for table, cols in _LATER_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # Normalise legacy timestamps so string comparison == time comparison.
    for col in ("expires_at", "created_at"):
        for row in conn.execute(f"SELECT uri, {col} AS v FROM consents WHERE {col} IS NOT NULL").fetchall():
            norm = normalize_dt(row["v"])
            if norm and norm != row["v"]:
                conn.execute(f"UPDATE consents SET {col} = ? WHERE uri = ?", (norm, row["uri"]))
    conn.commit()


# ---------------------------------------------------------------- writes -----


def upsert_video(conn, uri, did, rkey, cid, v: dict) -> None:
    conn.execute(
        """
        INSERT INTO videos (uri, did, rkey, cid, title, description, content_hash, duration_ms,
                            width, height, language, tags, storage_ref, mime_type, created_at,
                            indexed_at, record)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, title=excluded.title, description=excluded.description,
          content_hash=excluded.content_hash, duration_ms=excluded.duration_ms,
          width=excluded.width, height=excluded.height, language=excluded.language,
          tags=excluded.tags, storage_ref=excluded.storage_ref, mime_type=excluded.mime_type,
          created_at=excluded.created_at, indexed_at=excluded.indexed_at, record=excluded.record
        """,
        (
            uri,
            did,
            rkey,
            cid,
            v.get("title"),
            v.get("description"),
            v.get("contentHash"),
            v.get("durationMs"),
            v.get("width"),
            v.get("height"),
            v.get("language"),
            json.dumps(v.get("tags") or []),
            json.dumps(v.get("storageRef") or {}),
            v.get("mimeType"),
            v.get("createdAt"),
            now_iso(),
            json.dumps(v),
        ),
    )


def upsert_consent(conn, uri, did, rkey, cid, c: dict) -> None:
    conn.execute(
        """
        INSERT INTO consents (uri, did, rkey, cid, video_uri, video_cid, purposes, model_classes,
                              modalities, expires_at, compensation, revocable, authorship_attested,
                              rights_basis, capture_provenance, created_at, indexed_at, record,
                              revoked_at, revoke_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, video_uri=excluded.video_uri, video_cid=excluded.video_cid,
          purposes=excluded.purposes, model_classes=excluded.model_classes,
          modalities=excluded.modalities, expires_at=excluded.expires_at,
          compensation=excluded.compensation, revocable=excluded.revocable,
          authorship_attested=excluded.authorship_attested, rights_basis=excluded.rights_basis,
          capture_provenance=excluded.capture_provenance, created_at=excluded.created_at,
          indexed_at=excluded.indexed_at, record=excluded.record,
          revoked_at=NULL, revoke_reason=NULL
        """,
        (
            uri,
            did,
            rkey,
            cid,
            c.get("videoUri"),
            c.get("videoCid"),
            json.dumps(c.get("purposes") or []),
            json.dumps(c.get("modelClasses") or []),
            json.dumps(c.get("modalities") or []),
            normalize_dt(c.get("expiresAt")),
            c.get("compensation"),
            int(bool(c.get("revocable"))),
            int(c.get("authorshipAttested") is True),
            c.get("rightsBasis"),
            c.get("captureProvenance"),
            normalize_dt(c.get("createdAt")) or c.get("createdAt"),
            now_iso(),
            json.dumps(c),
        ),
    )


def revoke_consent(conn, uri: str, reason: str) -> None:
    """Idempotent: a replayed delete keeps the original revoked_at."""
    conn.execute(
        "UPDATE consents SET revoked_at = ?, revoke_reason = ? WHERE uri = ? AND revoked_at IS NULL",
        (now_iso(), reason, uri),
    )


def delete_video(conn, uri: str, reason: str = "video deleted") -> None:
    conn.execute("DELETE FROM videos WHERE uri = ?", (uri,))
    conn.execute(
        "UPDATE consents SET revoked_at = ?, revoke_reason = ? WHERE video_uri = ? AND revoked_at IS NULL",
        (now_iso(), reason, uri),
    )


def set_account_status(conn, did: str, active: bool, status: str | None) -> None:
    if not active and status == "deleted":
        conn.execute(
            "UPDATE consents SET revoked_at = ?, revoke_reason = 'account deleted' "
            "WHERE did = ? AND revoked_at IS NULL",
            (now_iso(), did),
        )
        conn.execute("DELETE FROM videos WHERE did = ?", (did,))
    conn.execute(
        "INSERT INTO accounts (did, active, status, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET active=excluded.active, status=excluded.status, "
        "updated_at=excluded.updated_at",
        (did, int(active), status, now_iso()),
    )


def ingest(conn, collection: str, uri: str, did: str, rkey: str, cid: str, value: dict) -> list[str]:
    """Validate and upsert one record. Returns validation errors (empty = stored)."""
    errors = validate_in_repo(collection, value, did)
    if errors:
        return errors
    (upsert_video if collection == VIDEO_NSID else upsert_consent)(conn, uri, did, rkey, cid, value)
    return []


def get_cursor(conn) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return int(row["value"]) if row else None


def set_cursor(conn, time_us: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CURSOR_KEY, str(time_us)),
    )


def handle_event(conn, event: dict) -> tuple[str, str] | None:
    """Apply one Jetstream event (no commit). Returns (op, subject) for events
    that concern us; op is create/update/delete/invalid/account."""
    kind = event.get("kind")
    did = event.get("did")
    if kind == "account" and did:
        acct = event.get("account") or {}
        if not conn.execute(
            "SELECT 1 FROM consents WHERE did = ? UNION SELECT 1 FROM accounts WHERE did = ? LIMIT 1",
            (did, did),
        ).fetchone():
            return None  # not one of our contributors
        set_account_status(conn, did, bool(acct.get("active")), acct.get("status"))
        return ("account", f"{did} active={acct.get('active')} status={acct.get('status')}")
    if kind != "commit":
        return None
    commit = event.get("commit") or {}
    collection, rkey, op = commit.get("collection"), commit.get("rkey"), commit.get("operation")
    if collection not in (VIDEO_NSID, CONSENT_NSID) or not (did and rkey and op):
        return None
    uri = f"at://{did}/{collection}/{rkey}"
    if op in ("create", "update"):
        errors = ingest(conn, collection, uri, did, rkey, commit.get("cid", ""), commit.get("record") or {})
        if errors:
            # Never leave the old terms live after an invalid rewrite.
            if collection == CONSENT_NSID:
                revoke_consent(conn, uri, "rewritten as an invalid record")
            else:
                delete_video(conn, uri, "video rewritten as an invalid record")
            return ("invalid", f"{uri}: {errors[0]}")
        return (op, uri)
    if op == "delete":
        if collection == CONSENT_NSID:
            revoke_consent(conn, uri, "consent deleted")
        else:
            delete_video(conn, uri)
        return (op, uri)
    return None


async def index_loop(
    conn,
    on_event: Callable[[str, str], None] | None = None,
    stop_after: int | None = None,
    max_backoff: float = 60.0,
) -> int:
    """Follow Jetstream forever (or until ``stop_after`` relevant events),
    persisting the cursor so restarts and dropped connections resume without
    missing revocations. Jetstream's replay window is about a day; after a
    longer outage, re-run ``appview backfill`` for known contributors."""
    params = "&".join(f"wantedCollections={c}" for c in (VIDEO_NSID, CONSENT_NSID))
    n, backoff = 0, 1.0
    while True:
        url = f"{JETSTREAM_URL}?{params}"
        cursor = get_cursor(conn)
        if cursor is not None:
            url += f"&cursor={max(cursor - CURSOR_REWIND_US, 0)}"
        try:
            async with websockets.connect(url, ping_interval=30, max_size=2**22) as ws:
                backoff = 1.0
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    result = handle_event(conn, event)
                    if isinstance(event.get("time_us"), int):
                        set_cursor(conn, event["time_us"])
                    conn.commit()
                    if result is None:
                        continue
                    if on_event:
                        on_event(*result)
                    if result[0] != "invalid":
                        n += 1
                    if stop_after and n >= stop_after:
                        return n
        except (OSError, websockets.exceptions.WebSocketException) as e:
            if on_event:
                on_event("reconnect", f"{type(e).__name__}: {e} (retry in {backoff:.0f}s)")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


# ----------------------------------------------------------------- reads -----


def query_consents(
    conn,
    *,
    purposes=(),
    model_classes=(),
    modalities=(),
    provenance=(),
    did: str | None = None,
    include_revoked=False,
    include_expired=False,
    limit: int | None = 50,
) -> list[sqlite3.Row]:
    """Consents granting ALL requested purposes/model classes/modalities, joined
    with their (indexed) video. By default only usable consents: not revoked,
    not expired, account active, and bound to the current video version."""
    where, args = [], []
    for col, wanted in (("purposes", purposes), ("model_classes", model_classes), ("modalities", modalities)):
        if wanted:
            where.append(
                f"NOT EXISTS (SELECT 1 FROM json_each(?) w "
                f"WHERE w.value NOT IN (SELECT value FROM json_each(c.{col})))"
            )
            args.append(json.dumps(list(wanted)))
    if provenance:
        where.append(f"c.capture_provenance IN ({','.join('?' * len(provenance))})")
        args.extend(provenance)
    if did:
        where.append("c.did = ?")
        args.append(did)
    if not include_revoked:
        where += [
            "c.revoked_at IS NULL",
            "c.did NOT IN (SELECT did FROM accounts WHERE active = 0)",
            "(c.video_cid IS NULL OR c.video_cid = v.cid)",
            # The newest existing consent for a video governs; older ones are superseded.
            "NOT EXISTS (SELECT 1 FROM consents n WHERE n.video_uri = c.video_uri "
            "AND n.revoked_at IS NULL AND n.uri != c.uri AND n.created_at > c.created_at)",
        ]
    if not include_expired:
        where.append("(c.expires_at IS NULL OR c.expires_at > ?)")
        args.append(now_iso())
    sql = (
        "SELECT c.*, v.title, v.duration_ms, v.cid AS current_video_cid, v.record AS video_record "
        f"FROM consents c {'LEFT JOIN' if include_revoked else 'JOIN'} videos v ON v.uri = c.video_uri "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.created_at DESC"
        + (" LIMIT ?" if limit else "")
    )
    if limit:
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def revocations(conn, since: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT uri, did, video_uri, revoked_at, revoke_reason FROM consents WHERE revoked_at IS NOT NULL"
    args = []
    if since:
        sql += " AND revoked_at > ?"
        args.append(normalize_dt(since) or since)
    return conn.execute(sql + " ORDER BY revoked_at DESC", args).fetchall()


def stats(conn) -> dict:
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    cursor = get_cursor(conn)
    return {
        "videos": one("SELECT COUNT(*) FROM videos"),
        "consents": one("SELECT COUNT(*) FROM consents"),
        "revoked": one("SELECT COUNT(*) FROM consents WHERE revoked_at IS NOT NULL"),
        "contributors": one("SELECT COUNT(DISTINCT did) FROM consents WHERE revoked_at IS NULL"),
        "inactive_accounts": one("SELECT COUNT(*) FROM accounts WHERE active = 0"),
        "cursor": format_dt(_us_to_dt(cursor)) if cursor else None,
    }


def _us_to_dt(us: int):
    from datetime import datetime

    return datetime.fromtimestamp(us / 1e6, tz=UTC)
