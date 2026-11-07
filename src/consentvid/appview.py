"""AppView: index org.consentvid.* records from the whole network into SQLite.

Subscribes to Jetstream v2 (``network.bsky.jetstream.subscribeEvents``, the
JSON firehose), validates every record on ingest, and keeps a queryable index
at ~/.consentvid/appview.db. The index is a discovery tool: Jetstream strips
repo signatures, so before *using* clips found here, export a manifest and run
``consentvid resync`` (which verifies proofs).

Each video has at most one consent: the consent's record key equals the
video's (the sidecar rule). Consents keyed otherwise are stored as invalid
rows and grant nothing.

Revocation semantics in the index:
* consent record deleted          -> consent revoked (and tombstoned)
* video record deleted            -> video removed, its consents revoked
* account deleted                 -> all its consents revoked, videos removed
* account deactivated/taken down  -> excluded from queries until reactivated;
  reactivation queues the repo for a backfill (pending_backfill)
* #sync (broken commit chain)     -> fail closed WITHOUT revoking: the repo is
  hidden from queries (accounts.desync_since) and queued for a backfill. Only
  a backfill that starts after the #sync unhides it; records the backfill no
  longer finds are then revoked like any other delete. A #sync says the
  commit chain broke, not that anything was deleted, so it never writes a
  revocation itself. A replayed #sync (same or older rev) is ignored.
* relay processing status (#account throttled / host-throttled /
  desynchronized)                 -> NOT a deactivation: the relay describes
  its own handling of the repo. The repo is hidden (desync_since) and queued,
  since its live events may be missing; a backfill reads it from the PDS and
  keeps it hidden (re-checked hourly) until the relay reports it active.

The indexer re-reads queued repos itself (index_loop's drain task, one repo
at a time, with backoff), so hiding normally lasts seconds to minutes;
``appview backfill --pending`` forces it.
* consent rewritten into an invalid record -> kept as an invalid row
  (valid=0): never usable, listed in the revocation feed

Revoked rows keep only what the revocation feed needs (uri, did, video_uri,
revoked_at, reason): a withdrawn statement about people is not kept.

Jetstream keeps 36 h of replay. The indexer resumes by Jetstream seq (and skips
events at or below the stored seq, since seq cursors are inclusive). A seq
past the retention floor (HTTP 400 CursorTooOld) or from another archive
(#info FutureCursor) falls back to the stored witnessedAt timestamp, which
Jetstream clamps to its floor (#info OutdatedCursor). Every such clamp records
a gap, which ``appview backfill --all`` repairs.

Results are ordered by sort_at = min(createdAt, first time we saw the record)
(Bluesky's rule for untrusted createdAt): a future-dated record sorts as of
when it was first indexed, so it can't push everyone else past --limit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from consentvid.paths import secure_dir
from consentvid.records import (
    CONSENT_NSID,
    VIDEO_NSID,
    format_dt,
    is_tid,
    normalize_dt,
    now_iso,
    parse_at_uri,
    parse_dt,
)
from consentvid.validate import validate_in_repo

JETSTREAM_URL = os.environ.get(
    "CONSENTVID_JETSTREAM_URL",
    "wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents",
)
_V2_PREFIX = "network.bsky.jetstream.subscribeEvents#"
CURSOR_KEY = "jetstream_cursor"  # unix microseconds (witnessedAt / v1 time_us)
SEQ_KEY = "jetstream_seq"  # Jetstream v2 seq: exact, but only on one instance
HOST_KEY = "jetstream_host"  # the instance SEQ_KEY belongs to
# Timestamp cursors: rewind to cover events in flight when we stopped and
# clock skew between instances. Replays are harmless: every write below is
# idempotent. (Seq cursors are inclusive and need no rewind.)
CURSOR_REWIND_US = 5_000_000
SEQ_MAX = 10**15  # Jetstream: values >= 1e15 are timestamps, not seqs
LOOKBACK_US = 36 * 3600 * 1_000_000  # Jetstream's replay window
GAP_WARN_US = 35 * 3600 * 1_000_000  # a timestamp cursor this old may already be past it
GAP_KEY = "gap_since"
# Bumped on every record_gap call, even one that keeps the earlier gap time,
# so a backfill can tell whether a gap was recorded while it ran (R2-07).
GAP_GEN_KEY = "gap_generation"
# Largest Jetstream message we accept. We never ask Jetstream to drop big
# messages (maxMessageSizeBytes is lossy); a frame over this limit closes the
# connection with 1009, and after MAX_1009_RETRIES at the same cursor the
# indexer records a gap and goes live (R3-06).
MAX_FRAME = 16 * 1024 * 1024
MAX_1009_RETRIES = 3
# A cursor (Jetstream time_us) further ahead than this is implausible (R3-08).
MAX_CURSOR_SKEW_US = 86_400_000_000


class StreamError(ConnectionError):
    """Jetstream sent an error frame (it closes the connection next)."""


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
CREATE TABLE IF NOT EXISTS consent_tombstones (
  uri TEXT PRIMARY KEY, did TEXT NOT NULL, deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_backfill (
  did TEXT PRIMARY KEY, reason TEXT, since TEXT NOT NULL
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
        ("first_seen_at", "TEXT"),  # first time this URI was indexed
        ("sort_at", "TEXT"),  # min(createdAt, first_seen_at): see _sort_at
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
        # grants nothing.
        ("valid", "INTEGER NOT NULL DEFAULT 1"),
        ("invalid_at", "TEXT"),
        ("first_seen_at", "TEXT"),
        ("sort_at", "TEXT"),
    ],
    "accounts": [
        # Set by a #sync event: the repo is hidden until a backfill that
        # started after this time re-reads it (R2-05).
        ("desync_since", "TEXT"),
        # rev of the last #sync applied: a replayed #sync is ignored.
        ("sync_rev", "TEXT"),
    ],
    "pending_backfill": [
        # The indexer re-reads pending repos itself (R3-02): failed attempts
        # so far, and when the next one is due ('' = now).
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_try", "TEXT NOT NULL DEFAULT ''"),
        # Last time the row was (re)queued: a backfill only clears rows
        # queued before it started reading the repo.
        ("queued_at", "TEXT NOT NULL DEFAULT ''"),
    ],
}
# A createdAt this far past the first sighting is treated as untrusted.
SORT_SKEW = timedelta(minutes=2)
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
    for table, col in (("consents", "expires_at"), ("consents", "created_at"), ("videos", "created_at")):
        for row in conn.execute(f"SELECT uri, {col} AS v FROM {table} WHERE {col} IS NOT NULL").fetchall():
            norm = normalize_dt(row["v"])
            if norm and norm != row["v"]:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE uri = ?", (norm, row["uri"]))
    # Rows from before first_seen_at existed: indexed_at is the best guess.
    for table in ("consents", "videos"):
        for row in conn.execute(
            f"SELECT uri, created_at, indexed_at FROM {table} WHERE first_seen_at IS NULL"
        ).fetchall():
            conn.execute(
                f"UPDATE {table} SET first_seen_at = ?, sort_at = ? WHERE uri = ?",
                (row["indexed_at"], _sort_at(row["created_at"], row["indexed_at"]), row["uri"]),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS consents_sort_at ON consents(sort_at)")
    # Revoked rows from before scrubbing existed: drop their contents.
    conn.execute(f"UPDATE consents SET {_SCRUB} WHERE revoked_at IS NOT NULL AND record IS NOT NULL")
    conn.commit()


# ---------------------------------------------------------------- writes -----


def _sort_at(created_at: str | None, first_seen_at: str) -> str:
    """When a record sorts: its createdAt, unless that is more than SORT_SKEW
    after we first saw the record (createdAt is chosen by the contributor and
    untrusted), in which case the first sighting. Bluesky's AppView does the
    same (sortAt = min(createdAt, indexedAt))."""
    created, seen = parse_dt(created_at), parse_dt(first_seen_at)
    if seen is None:
        return normalize_dt(created_at) or first_seen_at
    if created is None or created > seen + SORT_SKEW:
        return format_dt(seen)
    return format_dt(created)


def _first_seen(conn, table: str, uri: str, now: str) -> str:
    row = conn.execute(f"SELECT first_seen_at FROM {table} WHERE uri = ?", (uri,)).fetchone()
    return row["first_seen_at"] if row is not None and row["first_seen_at"] else now


def upsert_video(conn, uri, did, rkey, cid, v: dict) -> None:
    now = now_iso()
    first_seen = _first_seen(conn, "videos", uri, now)
    created = normalize_dt(v.get("createdAt"))
    # The record exists again: a tombstone from an earlier delete is stale.
    conn.execute("DELETE FROM video_tombstones WHERE uri = ?", (uri,))
    conn.execute(
        """
        INSERT INTO videos (uri, did, rkey, cid, title, description, content_hash, duration_ms,
                            width, height, language, tags, storage_ref, mime_type, created_at,
                            indexed_at, record, first_seen_at, sort_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, title=excluded.title, description=excluded.description,
          content_hash=excluded.content_hash, duration_ms=excluded.duration_ms,
          width=excluded.width, height=excluded.height, language=excluded.language,
          tags=excluded.tags, storage_ref=excluded.storage_ref, mime_type=excluded.mime_type,
          created_at=excluded.created_at, indexed_at=excluded.indexed_at, record=excluded.record,
          first_seen_at=excluded.first_seen_at, sort_at=excluded.sort_at
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
            created or v.get("createdAt"),
            now,
            json.dumps(v),
            first_seen,
            _sort_at(created, first_seen),
        ),
    )


def upsert_consent(conn, uri, did, rkey, cid, c: dict, *, valid: bool = True) -> None:
    """Store a consent. ``valid=False`` stores a record that fails validation
    with no terms at all, so it can never be returned as usable."""
    now = now_iso()
    first_seen = _first_seen(conn, "consents", uri, now)
    created = normalize_dt(c.get("createdAt"))
    sort_at = _sort_at(created, first_seen)
    # The record exists again: a tombstone from an earlier delete is stale.
    conn.execute("DELETE FROM consent_tombstones WHERE uri = ?", (uri,))
    if not valid:
        conn.execute(
            """
            INSERT INTO consents (uri, did, rkey, cid, video_uri, purposes, model_classes, modalities,
                                  created_at, indexed_at, record, revoked_at, revoke_reason, valid,
                                  invalid_at, first_seen_at, sort_at)
            VALUES (?, ?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, NULL, NULL, 0, ?, ?, ?)
            ON CONFLICT(uri) DO UPDATE SET
              cid=excluded.cid, video_uri=excluded.video_uri, video_cid=NULL, purposes='[]',
              model_classes='[]', modalities='[]', expires_at=NULL, compensation=NULL,
              revocable=NULL, authorship_attested=NULL, rights_basis=NULL, capture_provenance=NULL,
              created_at=excluded.created_at, indexed_at=excluded.indexed_at, record=excluded.record,
              revoked_at=NULL, revoke_reason=NULL, valid=0,
              invalid_at=CASE WHEN consents.valid = 0 AND consents.revoked_at IS NULL
                              THEN consents.invalid_at ELSE excluded.invalid_at END,
              first_seen_at=excluded.first_seen_at, sort_at=excluded.sort_at
            """,
            (uri, did, rkey, cid, c["videoUri"], created, now, json.dumps(c), now, first_seen, sort_at),
        )
        return
    conn.execute(
        """
        INSERT INTO consents (uri, did, rkey, cid, video_uri, video_cid, purposes, model_classes,
                              modalities, expires_at, compensation, revocable, authorship_attested,
                              rights_basis, capture_provenance, created_at, indexed_at, record,
                              revoked_at, revoke_reason, valid, invalid_at, first_seen_at, sort_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, NULL, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
          cid=excluded.cid, video_uri=excluded.video_uri, video_cid=excluded.video_cid,
          purposes=excluded.purposes, model_classes=excluded.model_classes,
          modalities=excluded.modalities, expires_at=excluded.expires_at,
          compensation=excluded.compensation, revocable=excluded.revocable,
          authorship_attested=excluded.authorship_attested, rights_basis=excluded.rights_basis,
          capture_provenance=excluded.capture_provenance, created_at=excluded.created_at,
          indexed_at=excluded.indexed_at, record=excluded.record,
          revoked_at=NULL, revoke_reason=NULL, valid=1, invalid_at=NULL,
          first_seen_at=excluded.first_seen_at, sort_at=excluded.sort_at
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
            created or c.get("createdAt"),
            now,
            json.dumps(c),
            first_seen,
            sort_at,
        ),
    )


# Everything a revoked row no longer needs (the revocation feed keeps uri,
# did, video_uri, revoked_at and the reason). A withdrawn consent is a
# statement about people; the index should not keep it.
_SCRUB = (
    "record = NULL, purposes = '[]', model_classes = '[]', modalities = '[]', video_cid = NULL, "
    "expires_at = NULL, compensation = NULL, revocable = NULL, authorship_attested = NULL, "
    "rights_basis = NULL, capture_provenance = NULL, created_at = NULL"
)


def revoke_consent(conn, uri: str, reason: str) -> None:
    """Idempotent: a replayed delete keeps the original revoked_at."""
    conn.execute(
        f"UPDATE consents SET revoked_at = ?, revoke_reason = ?, {_SCRUB} WHERE uri = ? AND revoked_at IS NULL",
        (now_iso(), reason, uri),
    )


def tombstone_consent(conn, uri: str, did: str | None = None, now: str | None = None) -> None:
    """Remember that the consent at ``uri`` was deleted, whether or not it
    was ever indexed, so a backfill that listed it before the delete cannot
    bring it back."""
    if did is None:
        try:
            did = parse_at_uri(uri).did
        except ValueError:
            return
    conn.execute(
        "INSERT INTO consent_tombstones (uri, did, deleted_at) VALUES (?, ?, ?) "
        "ON CONFLICT(uri) DO UPDATE SET deleted_at = excluded.deleted_at",
        (uri, did, now or now_iso()),
    )


def delete_consent(conn, uri: str, reason: str = "consent deleted") -> None:
    tombstone_consent(conn, uri)
    revoke_consent(conn, uri, reason)


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
        f"UPDATE consents SET revoked_at = ?, revoke_reason = ?, {_SCRUB} "
        "WHERE video_uri = ? AND revoked_at IS NULL",
        (now, reason, uri),
    )


def _purge_repo(conn, did: str, reason: str, now: str) -> None:
    """Revoke (and tombstone) every consent of ``did`` and remove its videos."""
    conn.execute(
        "INSERT INTO consent_tombstones (uri, did, deleted_at) SELECT uri, did, ? FROM consents WHERE did = ? "
        "ON CONFLICT(uri) DO UPDATE SET deleted_at = excluded.deleted_at",
        (now, did),
    )
    conn.execute(
        f"UPDATE consents SET revoked_at = ?, revoke_reason = ?, {_SCRUB} WHERE did = ? AND revoked_at IS NULL",
        (now, reason, did),
    )
    _tombstone_videos(conn, "did = ?", (did,), now)
    conn.execute("DELETE FROM videos WHERE did = ?", (did,))


def queue_backfill(conn, did: str, reason: str, next_try: str = "") -> None:
    """Queue ``did`` for a re-read, due at ``next_try`` ('' = now). A row
    that already exists keeps its place (since) and its failed attempts."""
    now = now_iso()
    conn.execute(
        "INSERT INTO pending_backfill (did, reason, since, attempts, next_try, queued_at) VALUES (?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET reason = excluded.reason, next_try = excluded.next_try, "
        "queued_at = excluded.queued_at",
        (did, reason, now, next_try, now),
    )


def mark_relay_lag(conn, did: str, status: str | None, next_try: str = "") -> None:
    """The relay reports a processing status (throttled, host-throttled,
    desynchronized) for ``did``: its live events may not reach us, so the
    repo is hidden like after a #sync (desync_since) and queued for a
    re-read. Not a withdrawal, and the account stays active."""
    conn.execute(
        "INSERT INTO accounts (did, active, status, updated_at, desync_since) VALUES (?, 1, NULL, '', ?) "
        "ON CONFLICT(did) DO UPDATE SET desync_since = excluded.desync_since",
        (did, now_iso()),
    )
    queue_backfill(conn, did, relay_lag_reason(status), next_try)


def relay_lag_reason(status: str | None) -> str:
    return f"relay {status}: live events may not be relayed"


def set_account_status(conn, did: str, active: bool, status: str | None) -> None:
    if not active and status == "deleted":
        _purge_repo(conn, did, "account deleted", now_iso())
    conn.execute(
        "INSERT INTO accounts (did, active, status, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET active=excluded.active, status=excluded.status, "
        "updated_at=excluded.updated_at",
        (did, int(active), status, now_iso()),
    )


def resync_repo(conn, did: str, rev: str | None = None) -> bool:
    """A #sync event: the commit chain broke, so the commits we indexed may no
    longer describe the repo. Fail closed without claiming a withdrawal: hide
    the repo (desync_since) and queue a backfill. Only a backfill that starts
    after this unhides it, and records it no longer finds are then revoked
    like any delete. Returns False (and changes nothing) for a replayed #sync
    whose rev is not newer than the last one applied."""
    row = conn.execute("SELECT sync_rev FROM accounts WHERE did = ?", (did,)).fetchone()
    old = row["sync_rev"] if row is not None else None
    if is_tid(rev) and is_tid(old) and rev <= old:
        return False
    now = now_iso()
    conn.execute(
        # A new row says nothing about the account's status: updated_at ''
        # lets the next backfill set it.
        "INSERT INTO accounts (did, active, status, updated_at, desync_since, sync_rev) VALUES (?, 1, NULL, '', ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET desync_since = excluded.desync_since, "
        "sync_rev = COALESCE(excluded.sync_rev, accounts.sync_rev)",
        (did, now, rev if is_tid(rev) else None),
    )
    queue_backfill(conn, did, "commit chain broken (#sync)")
    return True


def clear_desync(conn, did: str, started: str) -> None:
    """A backfill that started at ``started`` re-read the repo: a #sync seen
    before then no longer hides it."""
    conn.execute(
        "UPDATE accounts SET desync_since = NULL WHERE did = ? AND desync_since IS NOT NULL AND desync_since < ?",
        (did, started),
    )


def ingest(conn, collection: str, uri: str, did: str, rkey: str, cid: str, value: dict) -> list[str]:
    """Validate and store one record. Returns validation errors (empty = stored
    as usable). An invalid record never leaves older terms usable: a consent
    for a video in the same repo is stored as an invalid row, any other
    invalid consent revokes the row it replaces, and an invalid video is
    removed together with its consents."""
    errors = validate_in_repo(collection, value, did, rkey=rkey)
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


def plausible_seq(seq) -> bool:
    return isinstance(seq, int) and not isinstance(seq, bool) and 0 < seq < SEQ_MAX


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _raw_cursor(conn) -> str | None:
    return _meta(conn, CURSOR_KEY)


def get_cursor(conn) -> int | None:
    """The stored timestamp cursor (unix µs), or None if there is none or it
    is implausible (an already-poisoned database recovers by resuming live)."""
    raw = _raw_cursor(conn)
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if plausible_time_us(value) else None


def get_seq(conn) -> int | None:
    raw = _meta(conn, SEQ_KEY)
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if plausible_seq(value) else None


def get_gap(conn) -> str | None:
    return _meta(conn, GAP_KEY)


def gap_token(conn) -> tuple[str | None, str | None]:
    """The stored gap and its generation: changes whenever a gap is
    recorded, even when the earliest gap time stays the same."""
    return _meta(conn, GAP_KEY), _meta(conn, GAP_GEN_KEY)


def set_cursor(conn, time_us: int) -> None:
    _set_meta(conn, CURSOR_KEY, time_us)


def record_gap(conn, at_time_us: int | None = None) -> None:
    """Remember that events after ``at_time_us`` (default: the stored
    cursor, else now) may be missing. The earliest gap is kept."""
    when = at_time_us or get_cursor(conn) or _now_us()
    try:
        generation = int(_meta(conn, GAP_GEN_KEY) or 0) + 1
    except ValueError:
        generation = 1
    _set_meta(conn, GAP_GEN_KEY, generation)
    old = get_gap(conn)
    try:
        if old is not None and int(old) <= when:
            return
    except ValueError:
        pass
    _set_meta(conn, GAP_KEY, when)


def clear_gap(conn) -> None:
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (GAP_KEY, GAP_GEN_KEY))


def clear_gap_if(conn, token: tuple[str | None, str | None]) -> bool:
    """Clear the gap only if it is still the one ``token`` (from gap_token)
    describes: a gap recorded meanwhile covers events after what a running
    backfill already read, so it must survive. Returns True if cleared (or
    there was none)."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if gap_token(conn) != token:
            conn.rollback()
            return False
        clear_gap(conn)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _drop_cursors(conn) -> None:
    conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (CURSOR_KEY, SEQ_KEY, HOST_KEY))


def _drop_seq_cursor(conn) -> None:
    """Forget the seq cursor but keep the witnessedAt timestamp, so the next
    connection resumes by time (Jetstream clamps an old one to its floor)."""
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (SEQ_KEY, HOST_KEY))


def _dt_to_us(value) -> int | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    return (dt - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def parse_frame(obj) -> dict | None:
    """Normalise one Jetstream frame into the event shape handle_event takes
    ({'kind', 'did', 'seq', 'time_us', 'commit'|'account'|...}), or None to
    skip it. Accepts Jetstream v2 (xrpc.v1.json framing: {"$type":
    "message", "payload": {"$type": "...subscribeEvents#commit", ...}}) and
    the legacy v1 shape (top-level 'kind'). Raises StreamError for an error
    frame."""
    if not isinstance(obj, dict):
        return None
    frame_type = obj.get("$type")
    if frame_type is None and "kind" in obj:
        return obj  # Jetstream v1
    if frame_type == "error":
        raise StreamError(f"Jetstream error {str(obj.get('error'))[:80]}: {str(obj.get('message'))[:200]}")
    if frame_type != "message":
        return None
    p = obj.get("payload")
    if not isinstance(p, dict) or not isinstance(p.get("$type"), str):
        return None
    if not p["$type"].startswith(_V2_PREFIX):
        return None
    kind = p["$type"][len(_V2_PREFIX) :]
    if kind == "info":
        return {"kind": "info", "name": p.get("name"), "message": p.get("message")}
    base = {"did": p.get("did"), "seq": p.get("seq"), "time_us": _dt_to_us(p.get("witnessedAt") or p.get("time"))}
    if kind == "commit":
        commit = {k: p.get(k) for k in ("rev", "operation", "collection", "rkey", "cid", "record") if k in p}
        return {"kind": "commit", **base, "rev": p.get("rev"), "commit": commit}
    if kind == "account":
        acct = p.get("account") if isinstance(p.get("account"), dict) else {}
        return {"kind": "account", **base, "account": {"active": acct.get("active"), "status": acct.get("status")}}
    if kind == "sync":
        # The subscribeRepos#sync body (did, rev, blocks) sits under 'sync'.
        body = p.get("sync") if isinstance(p.get("sync"), dict) else {}
        return {"kind": "sync", **base, "rev": body.get("rev", p.get("rev"))}
    if kind == "identity":
        return {"kind": kind, **base}
    return None  # unknown message types: skip (forward compatibility)


def known_did(conn, did: str) -> bool:
    """Is ``did`` one of our contributors (or queued for a backfill)?"""
    return (
        conn.execute(
            "SELECT 1 FROM consents WHERE did = ? UNION SELECT 1 FROM videos WHERE did = ? "
            "UNION SELECT 1 FROM accounts WHERE did = ? UNION SELECT 1 FROM pending_backfill WHERE did = ? LIMIT 1",
            (did, did, did, did),
        ).fetchone()
        is not None
    )


def handle_event(conn, event: dict) -> tuple[str, str] | None:
    """Apply one (normalised) Jetstream event (no commit). Returns (op,
    subject) for events that concern us; op is create/update/delete/invalid/
    account/sync."""
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    did = event.get("did")
    if kind == "account" and isinstance(did, str) and did:
        acct = event.get("account") or {}
        if not isinstance(acct, dict) or not known_did(conn, did):
            return None  # not one of our contributors
        active = bool(acct.get("active"))
        status = acct.get("status")
        if is_operational((active, status)):
            # The relay's own processing state (R3-01), not the account's:
            # never a deactivation. Its events may be missing, though, so
            # hide the repo until the indexer has re-read it.
            mark_relay_lag(conn, did, status)
            return ("account", f"{did}: relay reports {status}; hidden until the indexer re-reads the repo")
        was = conn.execute("SELECT active, desync_since FROM accounts WHERE did = ?", (did,)).fetchone()
        set_account_status(conn, did, active, status)
        if active and was is not None and (not was["active"] or was["desync_since"] is not None):
            # Records may have changed while we ignored the account (or
            # while the relay was not forwarding its events).
            queue_backfill(conn, did, "account reactivated" if not was["active"] else "relay caught up")
        return ("account", f"{did} active={acct.get('active')} status={status}")
    if kind == "sync" and isinstance(did, str) and did:
        if not known_did(conn, did):
            return None
        rev = event.get("rev")
        if not resync_repo(conn, did, rev if isinstance(rev, str) else None):
            return None  # a replayed #sync
        return (
            "sync",
            f"{did}: commit chain broken; its consents are hidden until the indexer re-reads the repo "
            "(normally within a minute; `appview backfill --pending` forces it)",
        )
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
            delete_consent(conn, uri)
        else:
            delete_video(conn, uri)
        return (op, uri)
    return None


def _is_v2(url: str) -> bool:
    return "/xrpc/" in urlsplit(url).path


# Event kinds the indexer acts on. #identity is not needed (handles are
# resolved on demand) and would cost a database commit per frame.
STREAM_KINDS = ("commit", "account", "sync")


def stream_url(base: str | None = None) -> str:
    """The subscription URL: our two collections, no lossy size limit. On
    Jetstream v2, collections= filters only commits; kinds= asks for exactly
    the commit, account and sync events the indexer uses."""
    base = base or JETSTREAM_URL
    if _is_v2(base):
        params = "&".join(
            [f"collections={c}" for c in (VIDEO_NSID, CONSENT_NSID)] + [f"kinds={k}" for k in STREAM_KINDS]
        )
    else:  # legacy v1 /subscribe
        params = "&".join(f"wantedCollections={c}" for c in (VIDEO_NSID, CONSENT_NSID))
    return f"{base}?{params}"


def _resume_cursor(conn, base: str, on_event) -> int | None:
    """The cursor to connect with: the stored seq when it belongs to this
    Jetstream instance, else the stored timestamp (rewound). A timestamp
    older than GAP_WARN_US may already be past the replay window: the gap
    is recorded up front."""
    host = urlsplit(base).hostname
    if _is_v2(base) and _meta(conn, HOST_KEY) == host:
        seq = get_seq(conn)
        if seq is not None:
            return seq
    time_us = get_cursor(conn)
    raw = _raw_cursor(conn)
    if time_us is None:
        if raw is not None and on_event:
            on_event("invalid", f"ignoring implausible stored cursor {raw!r}; resuming live")
        return None
    if _now_us() - time_us > GAP_WARN_US:
        record_gap(conn, time_us)
        conn.commit()
        if on_event:
            on_event("gap", _gap_message(time_us, "the stored cursor is older than Jetstream's 36 h replay"))
    return max(time_us - CURSOR_REWIND_US, 0)


def _gap_message(at_us, why: str) -> str:
    return (
        f"{why}: events after {_fmt_cursor(_int_or_none(at_us))} may be missing. "
        "Repair with `consentvid appview backfill --all`."
    )


def _cursor_too_old(e: BaseException) -> bool:
    """A v2 handshake refused with HTTP 400 CursorTooOld."""
    response = getattr(e, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(e, "status_code", None)  # websockets < 14
    if status != 400:
        return False
    body = getattr(response, "body", b"") or b""
    if isinstance(body, str):
        body = body.encode()
    return b"CursorTooOld" in body or "CursorTooOld" in str(e)


def _note_position(conn, event: dict, host: str | None, on_event) -> None:
    seq, time_us = event.get("seq"), event.get("time_us")
    if seq is not None:
        if plausible_seq(seq):
            _set_meta(conn, SEQ_KEY, seq)
            if host:
                _set_meta(conn, HOST_KEY, host)
        elif on_event:
            on_event("invalid", f"implausible seq {str(seq)[:40]!r}; cursor not moved")
    if time_us is not None or "time_us" in event:
        if plausible_time_us(time_us):
            set_cursor(conn, time_us)
        elif on_event and seq is None:
            on_event("invalid", f"implausible time_us {str(time_us)[:40]!r}; cursor not moved")


async def index_loop(
    conn,
    on_event: Callable[[str, str], None] | None = None,
    stop_after: int | None = None,
    max_backoff: float = 60.0,
    drain: Callable[[str], object] | None = None,
    drain_interval: float = 5.0,
) -> int:
    """Follow Jetstream forever (or until ``stop_after`` relevant events),
    persisting the cursor so restarts and dropped connections resume without
    missing revocations. Resumes by seq on the same Jetstream instance, else
    by witnessedAt time. Jetstream keeps 36 h of replay; a resume past it
    (CursorTooOld / OutdatedCursor) records a gap, shown by ``appview stats``
    and ``doctor``, which ``appview backfill --all`` repairs.

    ``drain(did)``: re-read one pending repo (#sync, reactivation, relay
    lag). With it, a background task works through pending_backfill every
    ``drain_interval`` seconds, one repo at a time in a worker thread, so a
    hidden repo comes back without an operator (R3-02). The global gap is
    never cleared by it: only ``backfill --all`` does that."""
    task = None
    if drain is not None:
        task = asyncio.create_task(_drain_loop(conn, drain, drain_interval, on_event))
    try:
        return await _index_loop(conn, on_event, stop_after, max_backoff)
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — stopping anyway
                pass


async def _index_loop(conn, on_event, stop_after, max_backoff) -> int:
    n, backoff = 0, min(1.0, max_backoff)
    too_big_at, too_big_count = None, 0
    base = JETSTREAM_URL
    host = urlsplit(base).hostname
    if _meta(conn, HOST_KEY) not in (None, host):
        # Seqs are per instance: another instance's seq would skip or replay.
        conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (SEQ_KEY, HOST_KEY))
        conn.commit()
    while True:
        url = stream_url(base)
        cursor = _resume_cursor(conn, base, on_event)
        if cursor is not None:
            url += f"&cursor={cursor}"
        by_seq = cursor is not None and cursor < SEQ_MAX  # else a timestamp (or live)
        # Seq cursors are inclusive: skip what we already applied (R2-05).
        last_seq = get_seq(conn) if _is_v2(base) and _meta(conn, HOST_KEY) == host else None
        kwargs = {"ping_interval": 30, "max_size": MAX_FRAME + 64 * 1024}
        if _is_v2(base):
            kwargs["subprotocols"] = ["xrpc.v1.json"]
        try:
            async with websockets.connect(url, **kwargs) as ws:
                backoff = 1.0
                async for raw in ws:
                    try:
                        obj = json.loads(raw)
                    except (ValueError, RecursionError):
                        continue
                    event = parse_frame(obj)  # StreamError: reconnect
                    if event is None:
                        continue
                    if event.get("kind") == "info":
                        name = str(event.get("name"))[:40]
                        if name == "OutdatedCursor":
                            record_gap(conn)
                            conn.commit()
                            if on_event:
                                on_event("gap", _gap_message(get_gap(conn), "Jetstream clamped an outdated cursor"))
                        elif name == "FutureCursor":
                            # Our seq is from another (or a rebuilt) archive and
                            # Jetstream started at its live tip: everything since
                            # our last event would be skipped (R2-04).
                            _drop_seq_cursor(conn)
                            conn.commit()
                            last_seq = None
                            if by_seq and get_cursor(conn) is not None:
                                if on_event:
                                    on_event("info", "FutureCursor: reconnecting from the stored timestamp")
                                raise StreamError("FutureCursor: seq cursor unknown to this archive")
                            record_gap(conn)
                            conn.commit()
                            if on_event:
                                on_event("gap", _gap_message(get_gap(conn), "Jetstream started at its live tip (FutureCursor)"))
                        elif on_event:
                            on_event("info", f"{name}: {str(event.get('message'))[:200]}")
                        continue
                    seq = event.get("seq")
                    if last_seq is not None and plausible_seq(seq) and seq <= last_seq:
                        continue  # already applied (inclusive cursor / overlap)
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
                    _note_position(conn, event, host if _is_v2(base) else None, on_event)
                    conn.commit()
                    if _is_v2(base) and plausible_seq(seq):
                        last_seq = seq
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
            if _cursor_too_old(e):
                # The seq is past Jetstream's retention. A timestamp cursor is
                # clamped to the retention floor instead of refused, so resume
                # from the stored witnessedAt: that replays everything still
                # available (revocations included) and keeps the gap minimal.
                # The gap is recorded either way; a backfill repairs it.
                record_gap(conn)
                gap = get_gap(conn)
                if by_seq and get_cursor(conn) is not None:
                    _drop_seq_cursor(conn)
                    why = "Jetstream refused the seq cursor (CursorTooOld); resuming from its retention floor"
                else:
                    _drop_cursors(conn)  # nothing older to fall back to: go live
                    why = "Jetstream refused the cursor (CursorTooOld)"
                conn.commit()
                if on_event:
                    on_event("gap", _gap_message(gap, why))
            elif _close_code(e) == 1009:
                # A frame over our limit: replaying from the same cursor would
                # hit it again forever, and no later revocation would apply.
                at = (get_seq(conn), get_cursor(conn))
                too_big_count = too_big_count + 1 if at == too_big_at else 1
                too_big_at = at
                if too_big_count >= MAX_1009_RETRIES:
                    too_big_at, too_big_count = None, 0
                    if at != (None, None):
                        # Record the gap and drop the cursor: the next
                        # connection is a live tail (no cursor, no rewind).
                        record_gap(conn, at[1])
                        _drop_cursors(conn)
                        conn.commit()
                    if on_event:
                        on_event(
                            "gap",
                            f"a Jetstream message over {MAX_FRAME} bytes at cursor {_fmt_cursor(at[1])} failed "
                            f"{MAX_1009_RETRIES} times; skipping to live. Events after it may be "
                            "missing: run `consentvid appview backfill --all`.",
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


# ------------------------------------------------------- relay discovery -----

DEFAULT_RELAY = "https://relay1.us-east.bsky.network"
RELAY_PAGE_LIMIT = 2000
# Statuses a relay reports about its OWN processing of a repo, not about the
# account (atproto.com/specs/account: for these "active may be true"; the
# indigo relay reports active=false for them anyway). 'host-throttled' is
# set whenever the PDS exceeds the relay's per-host account limit (100 for a
# new host by default), which a community or university PDS easily does.
# They never mean withdrawal or takedown: the PDS stays authoritative, and
# only the live firehose may lag. Every other inactive status (including
# unknown strings and None) stays authoritative, so unknown values fail closed.
RELAY_OPERATIONAL_STATUSES = frozenset({"throttled", "host-throttled", "desynchronized"})


def is_operational(answer) -> bool:
    """Is ``answer`` ((active, status) from getRepoStatus or an #account
    event) a relay-processing status rather than an account status?"""
    return (
        isinstance(answer, tuple)
        and len(answer) == 2
        and answer[0] is False
        and answer[1] in RELAY_OPERATIONAL_STATUSES
    )


def _relay_get(relay: str, nsid_path: str, params: dict):
    """GET a relay XRPC endpoint through the size/time-capped client.
    Returns (status, parsed JSON or None)."""
    import httpx

    from consentvid.identity import _http, capped_send

    client = _http()
    request = client.build_request("GET", f"{relay.rstrip('/')}/xrpc/{nsid_path}", params=params)
    resp = capped_send(client, request)
    try:
        body = json.loads(resp.content) if resp.content else None
    except (ValueError, RecursionError):
        body = None
    if resp.status_code >= 500:
        raise httpx.HTTPStatusError(f"{nsid_path}: HTTP {resp.status_code}", request=request, response=resp)
    return resp.status_code, body


def list_repos_by_collection(relay: str, collection: str, *, max_pages: int | None = None) -> list[str]:
    """DIDs the relay lists as holding records of ``collection``
    (com.atproto.sync.listReposByCollection). A candidate list: it can be
    stale, so every DID is still read from its own PDS. Non-DIDs are dropped;
    a repeated cursor or too many pages raises."""
    from consentvid.errors import ConsentVidError
    from consentvid.identity import MAX_PAGES, with_retry
    from consentvid.records import is_did

    max_pages = max_pages or MAX_PAGES
    out, seen, cursor = [], set(), None
    for _ in range(max_pages):
        params = {"collection": collection, "limit": RELAY_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        status, body = with_retry(
            lambda params=params: _relay_get(relay, "com.atproto.sync.listReposByCollection", params),
            what="listReposByCollection",
        )
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("repos", []), list):
            raise ConsentVidError(f"relay {relay} answered listReposByCollection with HTTP {status}")
        for repo in body.get("repos") or []:
            did = repo.get("did") if isinstance(repo, dict) else None
            if is_did(did) and did not in out:
                out.append(did)
        cursor = body.get("cursor")
        if not cursor or not isinstance(cursor, str):
            return out
        if cursor in seen:
            raise ConsentVidError(f"relay {relay} repeated listReposByCollection cursor {cursor[:40]!r}")
        seen.add(cursor)
    raise ConsentVidError(f"relay {relay} returned more than {max_pages} pages for {collection}")


def relay_repo_status(relay: str, did: str) -> tuple[bool, str | None]:
    """(active, status) of ``did`` per the relay (com.atproto.sync.getRepoStatus).
    Raises RepoUnavailable(did, "not-found") for RepoNotFound (a relay only
    knows repos it crawls, so that alone doesn't prove a deletion), and
    ConsentVidError / network errors on any other failure."""
    from consentvid.errors import ConsentVidError, RepoUnavailable
    from consentvid.identity import with_retry

    status, body = with_retry(
        lambda: _relay_get(relay, "com.atproto.sync.getRepoStatus", {"did": did}), what="getRepoStatus"
    )
    if status in (400, 404) and isinstance(body, dict) and body.get("error") == "RepoNotFound":
        raise RepoUnavailable(did, "not-found", f"relay {relay} doesn't know {did}")
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("active"), bool):
        raise ConsentVidError(f"relay {relay} answered getRepoStatus with HTTP {status}")
    st = body.get("status")
    return body["active"], st if isinstance(st, str) else None


def known_dids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT did FROM consents UNION SELECT did FROM videos UNION SELECT did FROM accounts "
        "UNION SELECT did FROM pending_backfill"
    )
    return {r["did"] for r in rows}


def pending_dids(conn) -> list[str]:
    return [r["did"] for r in conn.execute("SELECT did FROM pending_backfill ORDER BY since")]


def pending_rows(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT did, reason, since, attempts, next_try FROM pending_backfill ORDER BY since"
    ).fetchall()


def clear_pending(conn, did: str, started: str | None = None) -> None:
    """Forget the pending re-read of ``did``. With ``started`` (when a
    backfill began reading the repo), only a row queued before then: one
    queued while the backfill ran (a #sync, a reactivation) still needs a
    re-read."""
    if started is None:
        conn.execute("DELETE FROM pending_backfill WHERE did = ?", (did,))
    else:
        conn.execute("DELETE FROM pending_backfill WHERE did = ? AND queued_at < ?", (did, started))


def due_pending(conn, now: str | None = None) -> str | None:
    """The oldest pending DID whose next attempt is due."""
    row = conn.execute(
        "SELECT did FROM pending_backfill WHERE next_try <= ? ORDER BY since, did LIMIT 1", (now or now_iso(),)
    ).fetchone()
    return row["did"] if row is not None else None


DRAIN_BACKOFF_BASE_S = 30
DRAIN_BACKOFF_MAX_S = 6 * 3600
RELAY_LAG_RECHECK_S = 3600


def note_drain_failure(conn, did: str) -> str:
    """Count a failed re-read of ``did`` and push its next attempt back
    (30 s, 60 s, ... at most 6 h). Returns when it is due."""
    row = conn.execute("SELECT attempts FROM pending_backfill WHERE did = ?", (did,)).fetchone()
    attempts = (row["attempts"] if row is not None else 0) + 1
    delay = min(DRAIN_BACKOFF_BASE_S * 2**attempts, DRAIN_BACKOFF_MAX_S)
    due = format_dt(datetime.now(UTC) + timedelta(seconds=delay))
    conn.execute("UPDATE pending_backfill SET attempts = ?, next_try = ? WHERE did = ?", (attempts, due, did))
    return due


async def drain_once(conn, drain: Callable[[str], object], on_event=None) -> str | None:
    """One drain tick: re-read the oldest due pending repo with ``drain(did)``
    in a worker thread (``drain`` opens its own database connection and
    settles the pending row itself on success). A failure is counted and
    retried later with backoff. Returns the DID it worked on, if any."""
    did = due_pending(conn)
    if did is None:
        return None
    try:
        await asyncio.to_thread(drain, did)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — one bad repo must not stop the indexer
        due = note_drain_failure(conn, did)
        conn.commit()
        if on_event:
            on_event("drain", f"{did}: re-read failed ({type(e).__name__}: {e}); next try {due}")
        return did
    if on_event:
        on_event("drain", f"{did}: re-read")
    return did


async def _drain_loop(conn, drain, interval: float, on_event) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await drain_once(conn, drain, on_event)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — e.g. a busy database: try again next tick
            if conn.in_transaction:
                conn.rollback()
            if on_event:
                on_event("drain", f"drain tick failed ({type(e).__name__}: {e}); retrying")


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
    not expired, account active, and bound to the current video version.
    Only sidecar consents are ever stored as valid (validate_in_repo checks
    the record key), so there is at most one per video."""
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
            # Inactive accounts, and repos whose commit chain broke (#sync)
            # and haven't been re-read yet.
            "c.did NOT IN (SELECT did FROM accounts WHERE active = 0 OR desync_since IS NOT NULL)",
            "c.video_cid = v.cid",
        ]
    if not include_expired:
        where.append("(c.expires_at IS NULL OR c.expires_at > ?)")
        args.append(now_iso())
    sql = (
        "SELECT c.*, v.title, v.duration_ms, v.cid AS current_video_cid, v.record AS video_record "
        f"FROM consents c {'LEFT JOIN' if include_revoked else 'JOIN'} videos v ON v.uri = c.video_uri "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.sort_at DESC, c.uri"
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
        "desynced_repos": one("SELECT COUNT(*) FROM accounts WHERE desync_since IS NOT NULL"),
        "pending_backfill": one("SELECT COUNT(*) FROM pending_backfill"),
        "pending_retrying": one("SELECT COUNT(*) FROM pending_backfill WHERE attempts > 0"),
        "cursor": _fmt_cursor(cursor),
        "seq": get_seq(conn),
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
