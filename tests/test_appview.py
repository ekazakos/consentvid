import json
import sqlite3

from conftest import ALICE, BOB, VIDEO_CID, consent_record, video_record
from consentvid import appview as av

C, V = "org.consentvid.consent", "org.consentvid.video"


def commit(did, coll, rkey, op, record=None, cid=None, t=1):
    return {
        "did": did,
        "time_us": t,
        "kind": "commit",
        "commit": {
            "collection": coll,
            "rkey": rkey,
            "operation": op,
            "cid": cid or VIDEO_CID,
            "record": record,
        },
    }


def setup(tmp_path, **consent_kw):
    conn = av.connect(tmp_path / "a.db")
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(**consent_kw), cid="bafyc1"))
    return conn


def test_ingest_and_all_of_query(tmp_path):
    conn = setup(tmp_path, purposes=["training", "evaluation"])
    q = lambda **kw: len(av.query_consents(conn, **kw))  # noqa: E731
    assert q(purposes=["training"]) == 1
    assert q(purposes=["training", "evaluation"]) == 1
    assert q(purposes=["training"], model_classes=["commercial"]) == 0  # needs BOTH
    assert q(modalities=["face"]) == 0


def test_cross_repo_consent_rejected(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    op, _ = av.handle_event(conn, commit(BOB, C, "c1", "create", consent_record(did=ALICE)))
    assert op == "invalid"
    assert conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0


def test_delete_consent_is_idempotent(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, "c1", "delete"))
    first = conn.execute("SELECT revoked_at FROM consents").fetchone()[0]
    av.handle_event(conn, commit(ALICE, C, "c1", "delete"))
    assert conn.execute("SELECT revoked_at FROM consents").fetchone()[0] == first
    assert av.query_consents(conn) == []
    assert av.revocations(conn)[0]["revoke_reason"] == "consent deleted"


def test_deleting_video_revokes_its_consents(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, V, "v1", "delete"))
    assert av.revocations(conn)[0]["revoke_reason"] == "video deleted"
    assert len(av.query_consents(conn, include_revoked=True)) == 1  # still listable


def test_video_rewrite_unbinds_pinned_consent(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, V, "v1", "update", video_record(title="new"), cid="bafynew"))
    assert av.query_consents(conn) == []


def test_invalid_update_revokes(tmp_path):
    conn = setup(tmp_path)
    op, _ = av.handle_event(conn, commit(ALICE, C, "c1", "update", consent_record(authorshipAttested=False)))
    assert op == "invalid" and av.query_consents(conn) == []


def test_account_events(tmp_path):
    conn = setup(tmp_path)

    def acct(active, status=None):
        return {
            "did": ALICE,
            "kind": "account",
            "time_us": 2,
            "account": {"active": active, "status": status},
        }

    av.handle_event(conn, acct(False, "deactivated"))
    assert av.query_consents(conn) == []
    av.handle_event(conn, acct(True))
    assert len(av.query_consents(conn)) == 1
    av.handle_event(conn, acct(False, "deleted"))
    assert av.revocations(conn)[0]["revoke_reason"] == "account deleted"
    # accounts we've never seen are ignored
    assert av.handle_event(conn, {**acct(False, "deleted"), "did": BOB}) is None


def test_expired_excluded(tmp_path):
    conn = setup(tmp_path, expiresAt="2026-01-02T00:00:00+00:00")
    assert av.query_consents(conn) == []
    assert len(av.query_consents(conn, include_expired=True)) == 1


def test_cursor_roundtrip(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    assert av.get_cursor(conn) is None
    av.set_cursor(conn, 1234)
    assert av.get_cursor(conn) == 1234


def test_migrates_legacy_schema(tmp_path):
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript("""
      CREATE TABLE consents (uri TEXT PRIMARY KEY, did TEXT NOT NULL, rkey TEXT NOT NULL,
        cid TEXT NOT NULL, video_uri TEXT NOT NULL, modalities TEXT, purposes TEXT,
        model_classes TEXT, expires_at TEXT, compensation TEXT, revocable INTEGER,
        created_at TEXT, indexed_at TEXT NOT NULL, revoked_at TEXT);
      INSERT INTO consents VALUES ('u','d','r','c','v','[]','[]','[]','2030-01-01T00:00:00Z',
        'none',1,'x','x',NULL);
    """)
    old.commit()
    old.close()
    conn = av.connect(p)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(consents)")}
    assert {"video_cid", "revoke_reason", "record", "capture_provenance"} <= cols
    assert conn.execute("SELECT expires_at FROM consents").fetchone()[0] == "2030-01-01T00:00:00.000Z"
    assert json.loads(conn.execute("SELECT purposes FROM consents").fetchone()[0]) == []


def test_newest_consent_governs(tmp_path):
    conn = setup(tmp_path, purposes=["training", "evaluation"])
    narrower = consent_record(purposes=["evaluation"], createdAt="2026-06-01T00:00:00+00:00")
    av.handle_event(conn, commit(ALICE, C, "c2", "create", narrower, cid="bafyc2"))
    assert av.query_consents(conn, purposes=["training"]) == []
    assert [r["rkey"] for r in av.query_consents(conn)] == ["c2"]
