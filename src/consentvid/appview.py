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
* consent rewritten into an invalid record -> kept as an invalid row
  (valid=0): never usable, listed in the revocation feed, and, as the
  contributor's newest statement, it still supersedes older consents for the
  same video (an invalid newer consent grants nothing; it must not revive
  older, broader terms)
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

import websockets

from consentvid.paths import secure_dir
from consentvid.records import CONSENT_NSID, VIDEO_NSID, format_dt, normalize_dt, now_iso, parse_at_uri
from consentvid.validate import validate_in_repo

JETSTREAM_URL = os.environ.get("CONSENTVID_JETSTREAM_URL", "wss://jetstream2.us-east.bsky.network/subscribe")
CURSOR_KEY = "jetstream_cursor"
# On resume, rewind to cover events in flight when we stopped. Replays are
# harmless: every write below is idempotent.
CURSOR_REWIND_US = 5_000_000
GAP_KEY = "gap_since"
# Largest Jetstream message we accept. Jetstream is asked to drop bigger ones
# itself (maxMessageSizeBytes); our websocket limit sits just above that, so a
# frame Jetstream re-encodes from a spec-legal record never wedges the
# indexer. If one still trips the limit (close code 1009) MAX_1009_RETRIES
# times at the same cursor, the indexer records a gap and goes live (R3-06).
MAX_FRAME = 16 * 1024 * 1024
MAX_1009_RETRIES = 3
# A cursor (Jetstream time_us) further ahead than this is implausible (R3-08).
MAX_CURSOR_SKEW_US = 86_400_000_000

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
CREATE TABLE IF NOT EXISTS video_tombstones (
  uri TEXT PRIMARY KEY, did TEXT NOT NULL, deleted_at TEXT NOT NULL
);
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
        # 0: the repo holds this record but it fails validation. Such a row
        # grants nothing, but still counts for supersession.
        ("valid", "INTEGER NOT NULL DEFAULT 1"),
        ("invalid_at", "TEXT"),
    ],
}
# How long a statement waits for another connection's write lock. The default
# (5 s) made a busy database look like a bad event (R2-05).
BUSY_TIMEOUT_S = 60.0


def db_path() -> Path:
    return Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid")) / "appview.db"


def connect(path: Path | None = None, *, timeout: float = BUSY_TIMEOUT_S) -> sqlite3.Connection:
    p = path or db_path()
    if path is None:
        secure_dir(p.parent)  # CONSENTVID_HOME also holds the session and keys
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
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


def upsert_consent(conn, uri, did, rkey, cid, c: dict, *, valid: bool = True) -> None:
    """Store a consent. ``valid=False`` stores a record that fails validation
    with no terms at all (only what supersession needs), so it can never be
    returned as usable; its createdAt is NULL when it doesn't parse, which the
    supersession rule treats as newest (fail closed)."""
    now = now_iso()
    if not valid:
        conn.execute(
            """
            INSERT INTO consents (uri, did, rkey, cid, video_uri, purposes, model_classes, modalities,
                                  created_at, indexed_at, record, revoked_at, revoke_reason, valid,
                                  invalid_at)
            VALUES (?, ?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, NULL, NULL, 0, ?)
            ON CONFLICT(uri) DO UPDATE SET
              cid=excluded.cid, video_uri=excluded.video_uri, video_cid=NULL, purposes='[]',
              model_classes='[]', modalities='[]', expires_at=NULL, compensation=NULL,
              revocable=NULL, authorship_attested=NULL, rights_basis=NULL, capture_provenance=NULL,
              created_at=excluded.created_at, indexed_at=excluded.indexed_at, record=excluded.record,
              revoked_at=NULL, revoke_reason=NULL, valid=0,
              invalid_at=CASE WHEN consents.valid = 0 AND consents.revoked_at IS NULL
                              THEN consents.invalid_at ELSE excluded.invalid_at END
            """,
            (uri, did, rkey, cid, c["videoUri"], normalize_dt(c.get("createdAt")), now, json.dumps(c), now),
        )
        return
    conn.execute(
        """
        INSERT INTO consents (uri, did, rkey, cid, video_uri, video_cid, purposes, model_classes,
                              modalities, expires_at, compensation, revocable, authorship_attested,
                              rights_basis, capture_provenance, created_at, indexed_at, record,
                              revoked_at, revoke_reason, valid, invalid_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, NULL)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, video_uri=excluded.video_uri, video_cid=excluded.video_cid,
          purposes=excluded.purposes, model_classes=excluded.model_classes,
          modalities=excluded.modalities, expires_at=excluded.expires_at,
          compensation=excluded.compensation, revocable=excluded.revocable,
          authorship_attested=excluded.authorship_attested, rights_basis=excluded.rights_basis,
          capture_provenance=excluded.capture_provenance, created_at=excluded.created_at,
          indexed_at=excluded.indexed_at, record=excluded.record,
          revoked_at=NULL, revoke_reason=NULL, valid=1, invalid_at=NULL
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
            now,
            json.dumps(c),
        ),
    )


def revoke_consent(conn, uri: str, reason: str) -> None:
    """Idempotent: a replayed delete keeps the original revoked_at."""
    conn.execute(
        "UPDATE consents SET revoked_at = ?, revoke_reason = ? WHERE uri = ? AND revoked_at IS NULL",
        (now_iso(), reason, uri),
    )


def _tombstone_videos(conn, where: str, args: tuple, now: str) -> None:
    """Remember when videos were removed, so a backfill that listed them
    before the removal does not bring them back (R3-07)."""
    conn.execute(
        f"INSERT INTO video_tombstones (uri, did, deleted_at) SELECT uri, did, ? FROM videos WHERE {where} "
        "ON CONFLICT(uri) DO UPDATE SET deleted_at = excluded.deleted_at",
        (now, *args),
    )


def delete_video(conn, uri: str, reason: str = "video deleted") -> None:
    now = now_iso()
    try:
        did = parse_at_uri(uri).did
    except ValueError:
        did = None
    if did is None:
        _tombstone_videos(conn, "uri = ?", (uri,), now)
    else:  # also when the row was never indexed: the delete still happened now
        conn.execute(
            "INSERT INTO video_tombstones (uri, did, deleted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(uri) DO UPDATE SET deleted_at = excluded.deleted_at",
            (uri, did, now),
        )
    conn.execute("DELETE FROM videos WHERE uri = ?", (uri,))
    conn.execute(
        "UPDATE consents SET revoked_at = ?, revoke_reason = ? WHERE video_uri = ? AND revoked_at IS NULL",
        (now, reason, uri),
    )


def set_account_status(conn, did: str, active: bool, status: str | None) -> None:
    if not active and status == "deleted":
        now = now_iso()
        conn.execute(
            "UPDATE consents SET revoked_at = ?, revoke_reason = 'account deleted' "
            "WHERE did = ? AND revoked_at IS NULL",
            (now, did),
        )
        _tombstone_videos(conn, "did = ?", (did,), now)
        conn.execute("DELETE FROM videos WHERE did = ?", (did,))
    conn.execute(
        "INSERT INTO accounts (did, active, status, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET active=excluded.active, status=excluded.status, "
        "updated_at=excluded.updated_at",
        (did, int(active), status, now_iso()),
    )


def ingest(conn, collection: str, uri: str, did: str, rkey: str, cid: str, value: dict) -> list[str]:
    """Validate and store one record. Returns validation errors (empty = stored
    as usable). An invalid record never leaves older terms usable: a consent
    for a video in the same repo is stored as an invalid row (it still
    supersedes), any other invalid consent revokes the row it replaces, and an
    invalid video is removed together with its consents."""
    errors = validate_in_repo(collection, value, did)
    if not errors:
        (upsert_video if collection == VIDEO_NSID else upsert_consent)(conn, uri, did, rkey, cid, value)
        return []
    if collection == CONSENT_NSID and _same_repo_video(value, did):
        upsert_consent(conn, uri, did, rkey, cid, value, valid=False)
    else:
        invalidate(conn, collection, uri)
    return errors


def _same_repo_video(value, did: str) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("videoUri"), str):
        return False
    try:
        return parse_at_uri(value["videoUri"], VIDEO_NSID).did == did
    except ValueError:
        return False


def invalidate(conn, collection: str, uri: str) -> None:
    """Make sure the record at ``uri`` is no longer usable (it was rewritten
    into something we can't store)."""
    if collection == CONSENT_NSID:
        revoke_consent(conn, uri, "rewritten as an invalid record")
    else:
        delete_video(conn, uri, "video rewritten as an invalid record")


def _now_us() -> int:
    return time.time_ns() // 1000


def plausible_time_us(time_us) -> bool:
    """A Jetstream time_us we may persist as the cursor: a positive int no
    more than a day in the future (R3-08: an untrusted stream must not store
    a cursor that later crashes datetime conversion)."""
    return (
        isinstance(time_us, int)
        and not isinstance(time_us, bool)
        and 0 < time_us <= _now_us() + MAX_CURSOR_SKEW_US
    )


def _raw_cursor(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return row["value"] if row else None


def get_cursor(conn) -> int | None:
    """The stored cursor, or None if there is none or it is implausible (an
    already-poisoned database recovers by resuming live)."""
    raw = _raw_cursor(conn)
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if plausible_time_us(value) else None


def get_gap(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (GAP_KEY,)).fetchone()
    return row["value"] if row else None


def set_cursor(conn, time_us: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CURSOR_KEY, str(time_us)),
    )


def handle_event(conn, event: dict) -> tuple[str, str] | None:
    """Apply one Jetstream event (no commit). Returns (op, subject) for events
    that concern us; op is create/update/delete/invalid/account."""
    if not isinstance(event, dict):
        return None
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
    if not isinstance(commit, dict):
        return None
    collection, rkey, op = commit.get("collection"), commit.get("rkey"), commit.get("operation")
    if collection not in (VIDEO_NSID, CONSENT_NSID) or not (did and rkey and op):
        return None
    if not (isinstance(did, str) and isinstance(rkey, str)):
        return None
    try:
        parse_at_uri(f"at://{did}/{collection}/{rkey}")
    except ValueError:
        return None
    uri = f"at://{did}/{collection}/{rkey}"
    if op in ("create", "update"):
        cid = commit.get("cid", "")
        # ingest never leaves the old terms live after an invalid rewrite.
        errors = ingest(conn, collection, uri, did, rkey, cid if isinstance(cid, str) else "", commit.get("record") or {})
        if errors:
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
    params += f"&maxMessageSizeBytes={MAX_FRAME}"
    n, backoff = 0, min(1.0, max_backoff)
    too_big_at, too_big_count = None, 0
    while True:
        url = f"{JETSTREAM_URL}?{params}"
        cursor = get_cursor(conn)
        raw = _raw_cursor(conn)
        if cursor is None and raw is not None and on_event:
            on_event("invalid", f"ignoring implausible stored cursor {raw!r}; resuming live")
        if cursor is not None:
            url += f"&cursor={max(cursor - CURSOR_REWIND_US, 0)}"
        try:
            async with websockets.connect(url, ping_interval=30, max_size=MAX_FRAME + 64 * 1024) as ws:
                backoff = 1.0
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    try:
                        result = handle_event(conn, event)
                    except sqlite3.OperationalError:
                        # The database, not the event: locked, busy, disk full.
                        # Don't advance the cursor; reconnect and replay it.
                        conn.rollback()
                        raise
                    except Exception as e:  # noqa: BLE001 — a poison event must not halt the index
                        conn.rollback()
                        result = ("invalid", f"event skipped: {type(e).__name__}: {e}")
                    time_us = event.get("time_us")
                    if plausible_time_us(time_us):
                        set_cursor(conn, time_us)
                    elif on_event and "time_us" in event:
                        on_event("invalid", f"implausible time_us {str(time_us)[:40]!r}; cursor not moved")
                    conn.commit()
                    if result is None:
                        continue
                    if on_event:
                        on_event(*result)
                    if result[0] != "invalid":
                        n += 1
                    if stop_after and n >= stop_after:
                        return n
        except (OSError, websockets.exceptions.WebSocketException, sqlite3.OperationalError) as e:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if _close_code(e) == 1009:
                # A frame over our limit: replaying from the same cursor would
                # hit it again forever, and no later revocation would apply.
                at = get_cursor(conn)
                too_big_count = too_big_count + 1 if at == too_big_at else 1
                too_big_at = at
                if too_big_count >= MAX_1009_RETRIES:
                    too_big_at, too_big_count = None, 0
                    if at is not None:
                        # Record the gap and drop the cursor: the next
                        # connection is a live tail (no cursor, no rewind).
                        conn.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (GAP_KEY, str(at)),
                        )
                        conn.execute("DELETE FROM meta WHERE key = ?", (CURSOR_KEY,))
                        conn.commit()
                    if on_event:
                        on_event(
                            "gap",
                            f"a Jetstream message over {MAX_FRAME} bytes at cursor {at} failed "
                            f"{MAX_1009_RETRIES} times; skipping to live. Events after {at} may be "
                            "missing: run `consentvid appview backfill` for your contributors.",
                        )
            else:
                too_big_at, too_big_count = None, 0
            if on_event:
                on_event("reconnect", f"{type(e).__name__}: {e} (retry in {backoff:.0f}s)")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


def _close_code(e: BaseException) -> int | None:
    if not isinstance(e, websockets.exceptions.ConnectionClosed):
        return None
    for frame in (getattr(e, "rcvd", None), getattr(e, "sent", None)):
        if frame is not None and getattr(frame, "code", None) == 1009:
            return 1009
    return None


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
    if "attested" in provenance:
        raise ValueError(
            "'attested' cannot be verified by the AppView (nothing verifies capture provenance yet; "
            "it is a self-declared string)"
        )
    if provenance:
        where.append(f"c.capture_provenance IN ({','.join('?' * len(provenance))})")
        args.extend(provenance)
    if did:
        where.append("c.did = ?")
        args.append(did)
    where.append("c.valid = 1")  # an invalid record grants nothing, ever
    if not include_revoked:
        where += [
            "c.revoked_at IS NULL",
            "c.did NOT IN (SELECT did FROM accounts WHERE active = 0)",
            "(c.video_cid IS NULL OR c.video_cid = v.cid)",
            # The newest existing consent for a video (valid or not) governs; older
            # ones are superseded. Ties on createdAt break on rkey, the same order
            # export and resync use; an unparseable createdAt (NULL) counts as newest.
            "NOT EXISTS (SELECT 1 FROM consents n WHERE n.video_uri = c.video_uri "
            "AND n.did = c.did AND n.revoked_at IS NULL AND n.uri != c.uri "
            "AND (n.created_at IS NULL OR n.created_at > c.created_at "
            "OR (n.created_at = c.created_at AND n.rkey > c.rkey)))",
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
    """Withdrawn consents, plus consents that exist but are invalid (they
    grant nothing; e.g. rewritten into an invalid record)."""
    when = "COALESCE(revoked_at, invalid_at)"
    sql = (
        f"SELECT uri, did, video_uri, {when} AS revoked_at, "
        "COALESCE(revoke_reason, 'invalid record: grants nothing') AS revoke_reason "
        "FROM consents WHERE (revoked_at IS NOT NULL OR valid = 0)"
    )
    args = []
    if since:
        sql += f" AND {when} > ?"
        args.append(normalize_dt(since) or since)
    return conn.execute(sql + f" ORDER BY {when} DESC", args).fetchall()


def stats(conn) -> dict:
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    cursor = get_cursor(conn)
    return {
        "videos": one("SELECT COUNT(*) FROM videos"),
        "consents": one("SELECT COUNT(*) FROM consents"),
        "revoked": one("SELECT COUNT(*) FROM consents WHERE revoked_at IS NOT NULL"),
        "invalid": one("SELECT COUNT(*) FROM consents WHERE valid = 0 AND revoked_at IS NULL"),
        "contributors": one("SELECT COUNT(DISTINCT did) FROM consents WHERE revoked_at IS NULL AND valid = 1"),
        "inactive_accounts": one("SELECT COUNT(*) FROM accounts WHERE active = 0"),
        "cursor": _fmt_cursor(cursor),
        "gap_since": _fmt_cursor(_int_or_none(get_gap(conn))) if get_gap(conn) else None,
    }


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _fmt_cursor(cursor):
    if not cursor:
        return None
    try:
        return format_dt(_us_to_dt(cursor))
    except (ValueError, OverflowError, OSError, TypeError):
        return str(cursor)


def _us_to_dt(us: int):
    from datetime import datetime

    return datetime.fromtimestamp(us / 1e6, tz=UTC)
