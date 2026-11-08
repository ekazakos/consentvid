import json
import sqlite3

import pytest

from conftest import ALICE, BOB, K0, K1, K2, K3, VIDEO_CID, consent_record, video_record
from consentvid import _core
from consentvid import appview as av
from consentvid import cli as _cli
from consentvid.records import RECORD_NSIDS

_REAL_REPO_STATUSES = _cli._repo_statuses  # before the autouse stub replaces it
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


@pytest.fixture(autouse=True)
def _repo_status_active(monkeypatch):
    """Backfills ask the PDS and the relay for the repo status: offline, both
    say active unless a test says otherwise."""
    from consentvid import cli

    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(True, None), (True, None)])


def setup(tmp_path, **consent_kw):
    conn = av.connect(tmp_path / "a.db")
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(**consent_kw), cid="bafyc1"))
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
    op, _ = av.handle_event(conn, commit(BOB, C, K1, "create", consent_record(did=ALICE)))
    assert op == "invalid"
    assert conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0


def test_delete_consent_is_idempotent(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, K1, "delete"))
    first = conn.execute("SELECT revoked_at FROM consents").fetchone()[0]
    av.handle_event(conn, commit(ALICE, C, K1, "delete"))
    assert conn.execute("SELECT revoked_at FROM consents").fetchone()[0] == first
    assert av.query_consents(conn) == []
    assert av.revocations(conn)[0]["revoke_reason"] == "consent deleted"


def test_deleting_video_revokes_its_consents(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, V, K1, "delete"))
    assert av.revocations(conn)[0]["revoke_reason"] == "video deleted"
    assert len(av.query_consents(conn, include_revoked=True)) == 1  # still listable


def test_video_rewrite_unbinds_pinned_consent(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, V, K1, "update", video_record(title="new"), cid="bafynew"))
    assert av.query_consents(conn) == []


def test_invalid_update_revokes(tmp_path):
    conn = setup(tmp_path)
    op, _ = av.handle_event(conn, commit(ALICE, C, K1, "update", consent_record(authorshipAttested=False)))
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


def test_replacing_terms_rewrites_the_sidecar(tmp_path):
    """R1-01: one consent per video, under the video's own record key."""
    conn = setup(tmp_path, purposes=["training", "evaluation"])
    narrower = consent_record(purposes=["evaluation"], createdAt="2026-06-01T00:00:00+00:00")
    av.handle_event(conn, commit(ALICE, C, K1, "update", narrower, cid="bafyc2"))
    assert av.query_consents(conn, purposes=["training"]) == []
    assert [r["rkey"] for r in av.query_consents(conn)] == [K1]


def test_query_returns_the_sidecar_consent(tmp_path):
    conn = setup(tmp_path)
    (row,) = av.query_consents(conn)
    assert row["uri"] == f"at://{ALICE}/{C}/{K1}" and row["video_uri"] == f"at://{ALICE}/{V}/{K1}"


# ------------------------------------------- R1-07: poison events/records


def test_index_loop_survives_poison_events(tmp_path, monkeypatch):
    import asyncio

    mallory = "did:plc:mallorymallorymallory1"

    def ev(did, coll, rkey, rec, t, op="create", cid=VIDEO_CID):
        return json.dumps(
            {
                "did": did,
                "time_us": t,
                "kind": "commit",
                "commit": {"collection": coll, "rkey": rkey, "operation": op, "cid": cid, "record": rec},
            }
        )

    stream = [
        ev(ALICE, V, K1, video_record(), 1_000),
        ev(ALICE, C, K1, consent_record(), 2_000, cid="bafyc1"),
        ev(mallory, C, "x", consent_record(did=mallory, purposes=[{}]), 3_000),  # unhashable items
        ev(mallory, C, "y", consent_record(did=mallory, createdAt="9999-12-31T23:59:59-05:00"), 3_100),
        ev(mallory, C, "boom", consent_record(did=mallory), 3_200),  # handle_event raises (below)
        "not json {",
        "[1, 2, 3]",
        json.dumps({"kind": "commit", "time_us": "soon", "commit": []}),
        ev(ALICE, C, K1, None, 4_000, op="delete"),  # Alice revokes after the poison
    ]

    class FakeWS:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                for m in stream:
                    yield m

            return gen()

    real_handle = av.handle_event

    def handle(conn, event):
        if isinstance(event, dict) and (event.get("commit") or {}).get("rkey") == "boom":
            conn.execute("INSERT INTO meta (key, value) VALUES ('partial', 'write')")
            raise OverflowError("poison")
        return real_handle(conn, event)

    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: FakeWS(url))
    monkeypatch.setattr(av, "handle_event", handle)
    conn = av.connect(tmp_path / "a.db")
    seen = []
    n = asyncio.run(av.index_loop(conn, on_event=lambda op, x: seen.append(op), stop_after=3))
    assert n == 3
    assert conn.execute("SELECT revoked_at FROM consents WHERE rkey = ?", (K1,)).fetchone()[0] is not None
    assert av.get_cursor(conn) == 4_000  # past every poison event
    assert conn.execute("SELECT value FROM meta WHERE key = 'partial'").fetchone() is None  # rolled back
    assert seen.count("invalid") == 4  # 3 poison records + one implausible time_us (R3-08)


def test_handle_event_ignores_non_objects(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    for event in ([], "x", 3, None, {"kind": "commit", "commit": []}):
        assert av.handle_event(conn, event) is None
    bad_rkey = commit(ALICE, C, "../x", "create", consent_record())
    assert av.handle_event(conn, bad_rkey) is None


# ------------------------------------- R1-08: backfill trusts no foreign URI


def test_backfill_ignores_foreign_uris(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    mallory = "did:plc:mallorymallorymallory1"
    conn = av.connect()  # CONSENTVID_HOME is a tmp dir (conftest)
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyalice"))
    conn.commit()
    before = dict(conn.execute("SELECT cid, video_uri, revoked_at FROM consents WHERE did = ?", (ALICE,)).fetchone())

    forged = consent_record(did=ALICE, purposes=["training", "evaluation"])
    listing = {
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyforged", value=forged)],
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid="bafyforgedv", value=video_record(title="x"))],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: mallory)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: listing.get(coll, []))
    result = CliRunner().invoke(cli.main, ["appview", "backfill", "mallory.test"])
    assert result.exit_code == 0, result.output
    assert "0 record(s) indexed, 2 skipped" in result.output
    conn = av.connect()
    after = dict(conn.execute("SELECT cid, video_uri, revoked_at FROM consents WHERE did = ?", (ALICE,)).fetchone())
    assert after == before
    assert conn.execute("SELECT cid FROM videos WHERE did = ?", (ALICE,)).fetchone()[0] == VIDEO_CID


def test_backfill_continues_after_a_failing_repo(monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli, identity

    def resolve(r):
        if r == "bad.test":
            raise RuntimeError("plc down")
        return ALICE

    monkeypatch.setattr(identity, "resolve_did", resolve)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: [])
    result = CliRunner().invoke(cli.main, ["appview", "backfill", "bad.test", "alice.test"])
    assert result.exit_code == 0 and "alice.test: 0 record(s) indexed" in result.output


def test_misskeyed_consent_is_invalid_and_hides_nothing(tmp_path):
    """R1-01: a consent whose key isn't its video's is ignored (stored as an
    invalid row: it neither grants nor supersedes anything)."""
    conn = setup(tmp_path)
    op, why = av.handle_event(conn, commit(ALICE, C, K2, "create", consent_record(purposes=["evaluation"]), cid="bafyc2"))
    assert op == "invalid" and "sidecar" in why
    assert [r["rkey"] for r in av.query_consents(conn)] == [K1]
    op, _ = av.handle_event(conn, commit(ALICE, C, K0, "create", consent_record(revocable=False), cid="bafyc0"))
    assert op == "invalid" and [r["rkey"] for r in av.query_consents(conn)] == [K1]


def test_non_tid_record_key_is_invalid(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    op, why = av.handle_event(conn, commit(ALICE, V, "self", "create", video_record()))
    assert op == "invalid" and "not a TID" in why
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_invalid_rewrite_of_the_sidecar_grants_nothing(tmp_path):
    conn = setup(tmp_path, modelClasses=["academic", "commercial"])
    assert len(av.query_consents(conn, model_classes=["commercial"])) == 1
    op, _ = av.handle_event(conn, commit(ALICE, C, K1, "update", consent_record(authorshipAttested=False)))
    assert op == "invalid"
    assert av.query_consents(conn) == []
    # Even the "show everything" listing never returns the invalid row itself.
    assert av.query_consents(conn, include_revoked=True, include_expired=True) == []
    av.handle_event(conn, commit(ALICE, C, K1, "delete"))
    assert av.query_consents(conn, model_classes=["commercial"]) == []


def test_appview_export_ignores_misskeyed_consents(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K2, "create", consent_record(), cid="bafyc2"))  # wrong key
    conn.commit()
    monkeypatch.setattr(identity, "display_name", lambda did: did)
    out = tmp_path / "m.json"
    r = CliRunner().invoke(cli.main, ["appview", "export", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["distribution"] == []
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()
    r = CliRunner().invoke(cli.main, ["appview", "export", str(out)])
    assert len(json.loads(out.read_text())["distribution"]) == 1


def test_foreign_invalid_consent_cannot_supersede(tmp_path):
    """Bob's invalid consent naming Alice's video must not hide Alice's."""
    conn = setup(tmp_path)
    newer = consent_record(did=ALICE, createdAt="2026-06-01T00:00:00Z")
    av.handle_event(conn, commit(BOB, C, "x", "create", newer))
    assert [r["rkey"] for r in av.query_consents(conn)] == [K1]


# ------------------------------------------ R2-05: lock is not poison


def _fake_ws(monkeypatch, stream):
    class FakeWS:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                for m in stream:
                    yield m

            return gen()

    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: FakeWS(url))


def test_locked_database_does_not_lose_a_revocation(tmp_path, monkeypatch):
    import asyncio

    p = tmp_path / "a.db"
    conn = av.connect(p, timeout=0.05)
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()
    delete = commit(ALICE, C, K1, "delete", t=5_000)
    _fake_ws(monkeypatch, [json.dumps(delete)])

    other = sqlite3.connect(p)
    other.execute("BEGIN IMMEDIATE")  # e.g. a backfill holding the write lock
    seen, sleeps = [], []

    async def fake_sleep(s):
        sleeps.append(av.get_cursor(conn))
        other.rollback()  # the other writer finishes

    monkeypatch.setattr(av.asyncio, "sleep", fake_sleep)
    n = asyncio.run(av.index_loop(conn, on_event=lambda op, x: seen.append(op), stop_after=1))
    assert n == 1
    assert sleeps == [None]  # cursor NOT advanced past the delete while locked
    assert "reconnect" in seen and "invalid" not in seen
    assert av.get_cursor(conn) == 5_000
    assert len(av.revocations(conn)) == 1


def test_backfill_never_holds_the_write_lock_during_network_reads(monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    conn.commit()
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }
    fetches = []

    def list_all(client, did, coll):
        probe = sqlite3.connect(av.db_path(), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if a writer holds it
            probe.rollback()
        finally:
            probe.close()
        fetches.append(coll)
        return listing.get(coll, [])

    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", list_all)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0 and "2 record(s) indexed" in r.output, r.output
    assert fetches == list(RECORD_NSIDS)


def test_backfill_warns_and_continues_on_a_looping_pds(monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    class LoopingRepo:
        def list_records(self, params):
            return SimpleNamespace(records=[SimpleNamespace(uri="x", cid="y", value={})], cursor="same")

    looping = SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(repo=LoopingRepo())))
    empty = SimpleNamespace(
        com=SimpleNamespace(
            atproto=SimpleNamespace(repo=SimpleNamespace(list_records=lambda p: SimpleNamespace(records=[], cursor=None)))
        )
    )
    monkeypatch.setattr(identity, "resolve_did", lambda r: {"loop.test": BOB, "alice.test": ALICE}[r])
    monkeypatch.setattr(identity, "public_client", lambda did: looping if did == BOB else empty)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "loop.test", "alice.test"])
    assert r.exit_code == 0, r.output
    assert "repeated listRecords cursor" in r.output and "alice.test: 0 record(s) indexed" in r.output


# ---------------------------- R2-14: backfill of an invalid rewrite revokes


def test_backfill_of_invalid_rewrite_leaves_nothing_usable(monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(modelClasses=["commercial"]), cid="bafyc1"))
    conn.commit()
    assert len(av.query_consents(conn, model_classes=["commercial"])) == 1
    rewritten = consent_record(modelClasses=["commercial"], authorshipAttested=False)
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1b", value=rewritten)],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: listing.get(coll, []))
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0, r.output
    conn = av.connect()
    assert av.query_consents(conn, model_classes=["commercial"]) == []
    assert [row["uri"] for row in av.revocations(conn)] == [f"at://{ALICE}/{C}/{K1}"]


def test_backfill_of_invalid_video_rewrite_removes_it(monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()
    bad_video = video_record(storageRef={"kind": "url", "value": "https://127.1/x"})
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid="bafyv2", value=bad_video)],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: listing.get(coll, []))
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0, r.output
    conn = av.connect()
    assert av.query_consents(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


# ------------------- R3-07: backfill must not undo concurrent index writes


def _backfill(monkeypatch, list_all):
    from click.testing import CliRunner

    from consentvid import cli, identity

    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", list_all)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0, r.output
    return r


def test_backfill_does_not_undo_concurrent_revocation(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()
    stale = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }

    def list_all(client, did, coll):
        if coll == C:  # the contributor deletes c1; `appview index` applies it mid-listing
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, C, K1, "delete", t=9_000_000))
            av.set_cursor(idx, 9_000_000)
            idx.commit()
            idx.close()
        return stale.get(coll, [])

    r = _backfill(monkeypatch, list_all)
    assert "changed during backfill" in r.output
    conn = av.connect()
    assert [x["uri"] for x in av.query_consents(conn)] == []
    assert [x["uri"] for x in av.revocations(conn)] == [f"at://{ALICE}/{C}/{K1}"]


def test_backfill_does_not_sweep_consent_created_during_listing(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    conn.commit()
    stale = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())], C: []}

    def list_all(client, did, coll):
        if coll == C:  # a new consent arrives through the firehose after our listing
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc2", t=9_000_000))
            idx.commit()
            idx.close()
        return stale.get(coll, [])

    _backfill(monkeypatch, list_all)
    conn = av.connect()
    assert [x["uri"] for x in av.query_consents(conn)] == [f"at://{ALICE}/{C}/{K1}"]
    assert av.revocations(conn) == []


def test_backfill_does_not_resurrect_video_deleted_during_listing(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    conn.commit()
    stale = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())], C: []}

    def list_all(client, did, coll):
        if coll == V:
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, V, K1, "delete", t=9_000_000))
            idx.commit()
            idx.close()
        return stale.get(coll, [])

    _backfill(monkeypatch, list_all)
    assert av.connect().execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


# ------------------ R3-08: implausible Jetstream time_us is never the cursor


def test_implausible_time_us_does_not_move_the_cursor(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av.set_cursor(conn, 1_000)
    conn.commit()
    stream = [
        json.dumps({**commit(ALICE, V, K1, "create", video_record()), "time_us": 2**62}),
        json.dumps({**commit(ALICE, V, K2, "create", video_record()), "time_us": 0}),
        json.dumps({**commit(ALICE, V, K3, "create", video_record()), "time_us": True}),
    ]
    _fake_ws(monkeypatch, stream)
    seen = []
    n = asyncio.run(av.index_loop(conn, on_event=lambda op, x: seen.append((op, x)), stop_after=3))
    assert n == 3 and av.get_cursor(conn) == 1_000
    assert sum("implausible time_us" in x for _, x in seen) == 3
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 3  # events still applied
    av.stats(conn)


def test_poisoned_cursor_is_ignored_and_stats_survive(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    av.set_cursor(conn, 2**62)
    conn.commit()
    assert av.get_cursor(conn) is None
    assert av.stats(conn)["cursor"] is None
    assert av._fmt_cursor(2**62) == str(2**62)


def test_poisoned_cursor_does_not_crash_cli(monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli

    conn = av.connect()
    av.set_cursor(conn, 2**62)
    conn.commit()
    r = CliRunner().invoke(cli.main, ["appview", "stats"])
    assert r.exit_code == 0, r.output


# ------------- R3-06: an oversized Jetstream frame must not wedge the index


def test_oversized_frame_reports_a_gap_and_goes_live(tmp_path, monkeypatch):
    import asyncio

    import websockets

    monkeypatch.setattr(av, "MAX_FRAME", 64 * 1024)
    conn = av.connect(tmp_path / "a.db")
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    av.set_cursor(conn, 1_000_000)
    conn.commit()
    big = json.dumps(commit(ALICE, C, K2, "create", {"pad": "x" * (200 * 1024)}, t=2_000_000))
    delete = json.dumps(commit(ALICE, C, K1, "delete", t=3_000_000))
    paths = []

    async def handler(ws):
        path = ws.request.path
        paths.append(path)
        if "cursor=" in path:  # replay: the oversized frame comes first, every time
            await ws.send(big)
        await ws.send(delete)  # live tail: only new events
        await ws.wait_closed()

    events = []

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0, max_size=None) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(av, "JETSTREAM_URL", f"ws://127.0.0.1:{port}/subscribe")
            return await asyncio.wait_for(
                av.index_loop(conn, on_event=lambda op, x: events.append((op, x)), stop_after=1, max_backoff=0.01),
                timeout=20,
            )

    assert asyncio.run(main()) == 1
    assert not any("maxMessageSizeBytes" in p for p in paths)  # never the lossy opt-in (R1-08)
    assert sum("cursor=" in p for p in paths) == av.MAX_1009_RETRIES  # bounded replays
    assert "cursor=" not in paths[-1]  # then a live tail
    assert any(op == "gap" and "over" in x and "backfill --all" in x for op, x in events)
    assert av.get_gap(conn) == "1000000" and av.stats(conn)["gap_since"]
    assert av.query_consents(conn) == [] and len(av.revocations(conn)) == 1


# ------------------- R1-07: a live delete of a never-indexed consent sticks


def test_backfill_does_not_resurrect_never_indexed_consent_deleted_during_listing(monkeypatch):
    from types import SimpleNamespace

    av.connect().commit()  # the index has never seen the consent
    stale = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }

    def list_all(client, did, coll):
        if coll == C:  # deleted after the PDS answered; the firehose applies it now
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, C, K1, "delete", t=9_000_000))
            idx.commit()
            idx.close()
        return stale.get(coll, [])

    _backfill(monkeypatch, list_all)
    conn = av.connect()
    assert av.query_consents(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM consent_tombstones").fetchone()[0] == 1


def test_recreated_consent_clears_its_tombstone(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, K1, "delete"))
    assert conn.execute("SELECT COUNT(*) FROM consent_tombstones").fetchone()[0] == 1
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc9"))
    assert conn.execute("SELECT COUNT(*) FROM consent_tombstones").fetchone()[0] == 0
    assert len(av.query_consents(conn)) == 1


# ------------------------------------------- R1-11 / R1-12: accounts, scrub


def test_account_deletion_reaches_video_only_contributors(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    op, _ = av.handle_event(conn, {"kind": "account", "did": ALICE, "account": {"active": False, "status": "deleted"}})
    assert op == "account"
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM video_tombstones").fetchone()[0] == 1


def test_revoked_rows_keep_no_record_contents(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, K1, "delete"))
    row = conn.execute("SELECT * FROM consents").fetchone()
    assert row["record"] is None and row["purposes"] == "[]" and row["created_at"] is None
    assert row["video_cid"] is None and row["capture_provenance"] is None
    (rev,) = av.revocations(conn)
    assert rev["uri"] == f"at://{ALICE}/{C}/{K1}" and rev["video_uri"] == f"at://{ALICE}/{V}/{K1}"
    assert len(av.query_consents(conn, include_revoked=True)) == 1
    av.stats(conn)


def test_migration_scrubs_old_revoked_rows(tmp_path):
    p = tmp_path / "a.db"
    conn = setup(tmp_path)  # same path
    conn.execute("UPDATE consents SET revoked_at = '2026-01-01T00:00:00.000Z', revoke_reason = 'x'")
    conn.commit()
    conn.close()
    conn = av.connect(p)
    assert conn.execute("SELECT record FROM consents").fetchone()[0] is None


# ------------------------------------------------ R1-08: Jetstream v2


def v2(kind, **payload):
    return {"$type": "message", "payload": {"$type": f"network.bsky.jetstream.subscribeEvents#{kind}", **payload}}


def v2_commit(did, coll, rkey, op, record=None, cid=None, seq=1, at="2026-11-07T10:00:00.000001Z"):
    p = {"seq": seq, "did": did, "time": at, "witnessedAt": at, "rev": "3maaaaaaaaaa2", "operation": op,
         "collection": coll, "rkey": rkey}
    if op != "delete":
        p.update(record=record, cid=cid or VIDEO_CID)
    return v2("commit", **p)


def test_parse_frame_v2_shapes():
    e = av.parse_frame(v2_commit(ALICE, V, K1, "create", video_record(), seq=7))
    assert e["kind"] == "commit" and e["seq"] == 7 and e["did"] == ALICE
    assert e["commit"]["rkey"] == K1 and e["commit"]["record"]["title"] == "A clip"
    assert e["time_us"] == 1794045600000001
    acct = av.parse_frame(v2("account", seq=8, did=ALICE, time="2026-11-07T10:00:00Z",
                             account={"seq": 99, "did": ALICE, "time": "x", "active": False, "status": "takendown"}))
    assert acct["kind"] == "account" and acct["account"] == {"active": False, "status": "takendown"}
    sync = av.parse_frame(v2("sync", seq=9, did=ALICE, time="2026-11-07T10:00:00Z", sync={"did": ALICE}))
    assert sync["kind"] == "sync" and sync["seq"] == 9
    info = av.parse_frame(v2("info", name="OutdatedCursor", message="m"))
    assert info == {"kind": "info", "name": "OutdatedCursor", "message": "m"}
    assert av.parse_frame(v2("somethingNew", seq=1)) is None  # forward compatible
    assert av.parse_frame({"$type": "message", "payload": {"$type": "other.lexicon#commit"}}) is None
    v1 = commit(ALICE, V, K1, "create", video_record())
    assert av.parse_frame(v1) is v1  # legacy shape still accepted
    with pytest.raises(av.StreamError):
        av.parse_frame({"$type": "error", "error": "ConsumerTooSlow", "message": "bye"})


def test_stream_url_is_v2_without_lossy_options():
    url = av.stream_url("wss://js.example/xrpc/network.bsky.jetstream.subscribeEvents")
    from urllib.parse import parse_qs, urlsplit

    wanted = parse_qs(urlsplit(url).query)["collections"]
    # Both record versions, plus the two account-signal collections.
    assert wanted == [
        "org.consentvid.video", "org.consentvid.videoV2", "org.consentvid.consent", "org.consentvid.consentV2",
        "community.lexicon.preference.ai", "app.bsky.actor.profile",
    ]
    # R2-06: exactly the kinds the indexer acts on; never #identity.
    assert "&kinds=commit&kinds=account&kinds=sync" in url and "identity" not in url
    assert "maxMessageSizeBytes" not in url and "wanted" not in url
    v1 = av.stream_url("wss://js.example/subscribe")
    assert "wantedCollections=" in v1 and "kinds" not in v1


class _Socket:
    def __init__(self, frames):
        self.frames = frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for f in self.frames:
                yield json.dumps(f)

        return gen()


def test_v2_commits_are_ingested_and_seq_stored(tmp_path, monkeypatch):
    import asyncio

    calls = []
    frames = [
        v2_commit(ALICE, V, K1, "create", video_record(), seq=41),
        v2_commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1", seq=42),
    ]
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: calls.append((url, kw)) or _Socket(frames))
    conn = av.connect(tmp_path / "a.db")
    assert asyncio.run(av.index_loop(conn, stop_after=2)) == 2
    assert av.get_seq(conn) == 42 and av._meta(conn, av.HOST_KEY) == "jetstream.us-east.bsky.network"
    assert av.get_cursor(conn) == 1794045600000001
    assert len(av.query_consents(conn)) == 1
    url, kw = calls[0]
    assert "/xrpc/network.bsky.jetstream.subscribeEvents?" in url and kw["subprotocols"] == ["xrpc.v1.json"]
    # Next connection resumes from the stored seq (inclusive; no rewind):
    # the replayed seq 42 is skipped, the new 43 applied.
    calls.clear()
    frames.append(v2_commit(ALICE, V, K2, "create", video_record(), seq=43))
    asyncio.run(av.index_loop(conn, stop_after=1))
    assert calls[0][0].endswith("&cursor=42")


def test_seq_from_another_instance_is_not_reused(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    recent = av._now_us() - 60_000_000
    av._set_meta(conn, av.SEQ_KEY, 42)
    av._set_meta(conn, av.HOST_KEY, "jetstream.elsewhere.example")  # not one of ours
    av.set_cursor(conn, recent)
    conn.commit()
    calls = []
    frames = [v2_commit(ALICE, V, K1, "create", video_record(), seq=5)]
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: calls.append(url) or _Socket(frames))
    asyncio.run(av.index_loop(conn, stop_after=1))
    assert calls[0].endswith(f"&cursor={recent - av.CURSOR_REWIND_US}")  # witnessedAt, not seq 42


def _too_old():
    import websockets
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    body = b'{"error":"CursorTooOld","message":"floor is 1000"}'
    return websockets.exceptions.InvalidStatus(Response(400, "Bad Request", Headers(), body))


async def _no_sleep(s):
    return None


def test_cursor_too_old_resumes_from_the_retention_floor(tmp_path, monkeypatch):
    """R2-03: a seq past retention falls back to the stored timestamp, which
    Jetstream clamps to its floor (OutdatedCursor), instead of a live tail."""
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    stamp = av._now_us() - 3600_000_000
    av._set_meta(conn, av.SEQ_KEY, 7)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    av.set_cursor(conn, stamp)
    conn.commit()
    calls = []

    def connect(url, **kw):
        calls.append(url)
        if url.endswith("&cursor=7"):
            raise _too_old()
        return _Socket([v2("info", name="OutdatedCursor", message="clamped"),
                        v2_commit(ALICE, V, K1, "create", video_record(), seq=2000)])

    monkeypatch.setattr(av.websockets, "connect", connect)
    monkeypatch.setattr(av.asyncio, "sleep", _no_sleep)
    events = []
    assert asyncio.run(av.index_loop(conn, on_event=lambda op, x: events.append((op, x)), stop_after=1)) == 1
    assert calls[0].endswith("&cursor=7")
    assert calls[1].endswith(f"&cursor={stamp - av.CURSOR_REWIND_US}")  # the timestamp, not live
    assert av.get_gap(conn) == str(stamp) and av.stats(conn)["gap_since"]
    assert any(op == "gap" and "CursorTooOld" in x and "backfill --all" in x for op, x in events)
    assert av.get_seq(conn) == 2000


def test_cursor_too_old_without_a_timestamp_goes_live(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av._set_meta(conn, av.SEQ_KEY, 7)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    conn.commit()
    calls = []

    def connect(url, **kw):
        calls.append(url)
        if "cursor=" in url:
            raise _too_old()
        return _Socket([v2_commit(ALICE, V, K1, "create", video_record(), seq=2000)])

    monkeypatch.setattr(av.websockets, "connect", connect)
    monkeypatch.setattr(av.asyncio, "sleep", _no_sleep)
    assert asyncio.run(av.index_loop(conn, stop_after=1)) == 1
    assert calls[0].endswith("&cursor=7") and "cursor=" not in calls[1]
    assert av.get_gap(conn) is not None


def test_future_cursor_reconnects_from_the_stored_timestamp(tmp_path, monkeypatch):
    """R2-04: a seq unknown to the archive must not silently skip to the tip."""
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    stamp = av._now_us() - 600_000_000
    av._set_meta(conn, av.SEQ_KEY, 99)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    av.set_cursor(conn, stamp)
    conn.commit()
    calls, seen_at_reconnect = [], []

    def connect(url, **kw):
        calls.append(url)
        if url.endswith("&cursor=99"):
            return _Socket([v2("info", name="FutureCursor", message="beyond the next seq"),
                            v2_commit(ALICE, V, K1, "create", video_record(), seq=5)])
        seen_at_reconnect.append((av._meta(conn, av.SEQ_KEY), av._meta(conn, av.HOST_KEY)))
        return _Socket([v2_commit(ALICE, V, K1, "create", video_record(), seq=6)])

    monkeypatch.setattr(av.websockets, "connect", connect)
    monkeypatch.setattr(av.asyncio, "sleep", _no_sleep)
    events = []
    assert asyncio.run(av.index_loop(conn, on_event=lambda op, x: events.append((op, x)), stop_after=1)) == 1
    assert calls[1].endswith(f"&cursor={stamp - av.CURSOR_REWIND_US}")
    assert seen_at_reconnect == [(None, None)]  # seq and host cleared
    assert ("info", "FutureCursor: reconnecting from the stored timestamp") in events
    assert av.get_gap(conn) is None  # nothing was skipped
    assert av.get_seq(conn) == 6


def test_future_cursor_without_a_timestamp_records_a_gap(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av._set_meta(conn, av.SEQ_KEY, 99)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    conn.commit()
    frames = [v2("info", name="FutureCursor", message="m"), v2_commit(ALICE, V, K1, "create", video_record(), seq=5)]
    calls = []
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: calls.append(url) or _Socket(frames))
    events = []
    assert asyncio.run(av.index_loop(conn, on_event=lambda op, x: events.append(op), stop_after=1)) == 1
    assert len(calls) == 1 and av.get_gap(conn) is not None and "gap" in events
    assert av.get_seq(conn) == 5  # the new archive's seq from here on


def test_events_at_or_below_the_stored_seq_are_skipped(tmp_path, monkeypatch):
    """R2-05: the v2 seq cursor is inclusive; overlap is not applied twice."""
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av._set_meta(conn, av.SEQ_KEY, 42)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    conn.commit()
    frames = [
        v2_commit(ALICE, V, K1, "create", video_record(title="replayed"), seq=41),
        v2_commit(ALICE, V, K1, "create", video_record(title="replayed"), seq=42),
        v2_commit(ALICE, V, K2, "create", video_record(title="new"), seq=43),
    ]
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: _Socket(frames))
    seen = []
    assert asyncio.run(av.index_loop(conn, on_event=lambda op, x: seen.append(x), stop_after=1)) == 1
    assert seen == [f"at://{ALICE}/{V}/{K2}"]
    assert [r["title"] for r in conn.execute("SELECT title FROM videos")] == ["new"]
    assert av.get_seq(conn) == 43


def test_outdated_cursor_info_records_a_gap(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av.set_cursor(conn, av._now_us() - 3600_000_000)
    conn.commit()
    frames = [v2("info", name="OutdatedCursor", message="clamped"), v2_commit(ALICE, V, K1, "create", video_record())]
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: _Socket(frames))
    events = []
    asyncio.run(av.index_loop(conn, on_event=lambda op, x: events.append(op), stop_after=1))
    assert av.get_gap(conn) is not None and "gap" in events


def test_old_timestamp_cursor_records_the_gap_up_front(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    old = av._now_us() - 40 * 3600_000_000
    av.set_cursor(conn, old)
    conn.commit()
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: _Socket([v2_commit(ALICE, V, K1, "create", video_record())]))
    asyncio.run(av.index_loop(conn, stop_after=1))
    assert av.get_gap(conn) == str(old)


def _sync(rev="3maaaaaaaaaz2", seq=3):
    return av.parse_frame(v2("sync", seq=seq, did=ALICE, time="2026-11-07T10:00:00Z",
                             sync={"did": ALICE, "rev": rev, "seq": 1}))


def test_sync_event_hides_the_repo_without_revoking(tmp_path):
    """R2-05: a broken commit chain is not a withdrawal."""
    conn = setup(tmp_path)
    assert _sync()["rev"] == "3maaaaaaaaaz2"
    op, _ = av.handle_event(conn, _sync())
    assert op == "sync"
    assert av.query_consents(conn) == []  # fail closed
    assert av.revocations(conn) == []  # ...but no false withdrawal signal
    row = conn.execute("SELECT record FROM consents").fetchone()
    assert row["record"] is not None  # not scrubbed
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert av.pending_dids(conn) == [ALICE] and av.stats(conn)["pending_backfill"] == 1
    assert av.stats(conn)["desynced_repos"] == 1
    # An #account event doesn't unhide it; nor do new commits.
    av.handle_event(conn, {"kind": "account", "did": ALICE, "account": {"active": True, "status": None}})
    av.handle_event(conn, commit(ALICE, C, K1, "update", consent_record(), cid="bafyc2"))
    assert av.query_consents(conn) == []
    # Strangers' #sync events are ignored.
    assert av.handle_event(conn, {"kind": "sync", "did": BOB}) is None


def test_replayed_sync_changes_nothing(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, _sync())
    av.clear_desync(conn, ALICE, "9999-01-01T00:00:00.000Z")  # as a backfill would
    av.clear_pending(conn, ALICE)
    assert len(av.query_consents(conn)) == 1
    assert av.handle_event(conn, _sync()) is None  # same rev, redelivered
    assert av.handle_event(conn, _sync(rev="3maaaaaaaaay2")) is None  # older rev
    assert len(av.query_consents(conn)) == 1 and av.pending_dids(conn) == []
    assert av.handle_event(conn, _sync(rev="3maaaaaaabaa2"))[0] == "sync"  # a newer one applies
    assert av.query_consents(conn) == []


def test_backfill_after_sync_unhides_and_revokes_only_missing_records(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    for k in (K1, K2):
        av.handle_event(conn, commit(ALICE, V, k, "create", video_record()))
        av.handle_event(conn, commit(ALICE, C, k, "create", consent_record(rkey=k), cid="bafyc1"))
    av.handle_event(conn, _sync())
    conn.commit()
    assert av.query_consents(conn) == []
    listing = {  # K2's consent is gone from the repo
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{k}", cid=VIDEO_CID, value=video_record()) for k in (K1, K2)],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }
    _backfill(monkeypatch, lambda client, did, coll: listing.get(coll, []))
    conn = av.connect()
    assert [r["rkey"] for r in av.query_consents(conn)] == [K1]
    assert [r["uri"] for r in av.revocations(conn)] == [f"at://{ALICE}/{C}/{K2}"]
    assert av.stats(conn)["desynced_repos"] == 0


def test_backfill_started_before_a_sync_does_not_unhide(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, _sync())
    av.clear_desync(conn, ALICE, "2000-01-01T00:00:00.000Z")  # a backfill that started earlier
    assert av.query_consents(conn) == []


def test_reactivated_account_is_queued_for_backfill(tmp_path):
    conn = setup(tmp_path)
    acct = lambda active, status=None: {"kind": "account", "did": ALICE, "account": {"active": active, "status": status}}  # noqa: E731
    av.handle_event(conn, acct(False, "deactivated"))
    assert av.pending_dids(conn) == []
    av.handle_event(conn, acct(True))
    assert av.pending_dids(conn) == [ALICE]


# ------------------------------------------- R1-09 / R1-10: discovery, status


def test_relay_listing_pages_and_filters(monkeypatch):
    from consentvid.errors import ConsentVidError

    pages = {
        None: {"repos": [{"did": ALICE}, {"did": "not-a-did"}, {"x": 1}], "cursor": "p2"},
        "p2": {"repos": [{"did": BOB}, {"did": ALICE}]},
    }
    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (200, pages[params.get("cursor")]))
    assert av.list_repos_by_collection("https://relay.example", C) == [ALICE, BOB]
    looping = {None: {"repos": [], "cursor": "a"}, "a": {"repos": [], "cursor": "a"}}
    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (200, looping[params.get("cursor")]))
    with pytest.raises(ConsentVidError, match="repeated"):
        av.list_repos_by_collection("https://relay.example", C)


def test_backfill_all_reaches_each_did_once_and_repairs_the_gap(monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli

    carol = "did:plc:carolcarolcarolcarolcar"
    conn = av.connect()
    av.handle_event(conn, commit(carol, V, K1, "create", video_record()))  # already indexed
    av.record_gap(conn, 1_000_000)
    av.queue_backfill(conn, BOB, "test")
    conn.commit()
    listed = {V: [ALICE, BOB], C: [ALICE]}
    monkeypatch.setattr(av, "list_repos_by_collection", lambda relay, nsid: listed.get(nsid, []))
    seen = []

    def backfill(conn, repo, relay=None):
        seen.append(repo)
        av.clear_pending(conn, repo)  # _backfill_repo settles the pending row itself
        conn.commit()
        return repo, 0, 0

    monkeypatch.setattr(cli, "_backfill_repo", backfill)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "--all"])
    assert r.exit_code == 0, r.output
    assert sorted(seen) == sorted([ALICE, BOB, carol])
    conn = av.connect()
    assert av.get_gap(conn) is None and av.pending_dids(conn) == [] and "Gap repaired" in r.output


def test_backfill_needs_a_target():
    from click.testing import CliRunner

    from consentvid import cli

    r = CliRunner().invoke(cli.main, ["appview", "backfill"])
    assert r.exit_code != 0 and "--all" in r.output


@pytest.mark.parametrize(
    "answers, expected",
    [
        ([(True, None), (False, "takendown")], (False, "takendown")),
        ([(False, "deactivated"), (False, "deleted")], (False, "deleted")),
        ([None, None], None),
        ([(True, None), None], None),
        ([(True, None), (True, None)], (True, None)),
    ],
)
def test_combine_status_is_most_restrictive(answers, expected):
    from consentvid import cli

    assert cli._combine_status(answers) == expected


@pytest.mark.parametrize(
    "statuses, active",
    [
        ([(True, None), (False, "takendown")], 0),  # relay says takendown: stays inactive
        ([None, None], "unchanged"),
        ([(True, None), (True, None)], 1),
    ],
)
def test_backfill_takes_account_status_from_pds_and_relay(monkeypatch, statuses, active):
    from types import SimpleNamespace

    from consentvid import cli

    conn = av.connect()
    av.set_account_status(conn, ALICE, False, "takendown")
    conn.execute("UPDATE accounts SET updated_at = '2000-01-01T00:00:00.000Z'")
    conn.commit()
    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: statuses)
    listing = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())], C: []}
    _backfill(monkeypatch, lambda client, did, coll: listing.get(coll, []))
    row = av.connect().execute("SELECT active, updated_at FROM accounts WHERE did = ?", (ALICE,)).fetchone()
    if active == "unchanged":
        assert row["active"] == 0 and row["updated_at"] == "2000-01-01T00:00:00.000Z"
    else:
        assert row["active"] == active


def test_relay_repo_status_reports_not_found(monkeypatch):
    from consentvid.errors import RepoUnavailable

    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (404, {"error": "RepoNotFound"}))
    with pytest.raises(RepoUnavailable):
        av.relay_repo_status("https://relay.example", ALICE)
    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (200, {"did": ALICE, "active": True}))
    assert av.relay_repo_status("https://relay.example", ALICE) == (True, None)


@pytest.mark.parametrize(
    "pds, relay, expected",
    [
        ("active", "not-found", [(True, None), None]),  # an uncrawled PDS is not a deletion
        ("not-found", "not-found", [(False, "deleted"), (False, "deleted")]),
        ("not-found", "fail", [None, None]),
        ("active", "takendown", [(True, None), (False, "takendown")]),
    ],
)
def test_repo_statuses_treat_not_found_carefully(monkeypatch, pds, relay, expected):
    from types import SimpleNamespace

    from atproto_client.exceptions import BadRequestError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    from consentvid import cli, identity
    from consentvid.errors import RepoUnavailable

    monkeypatch.setattr(identity, "_sleep", lambda s: None)

    def get_repo_status(p):
        if pds == "not-found":
            raise BadRequestError(
                Response(success=False, status_code=400, content=XrpcError(error="RepoNotFound", message="x"), headers={})
            )
        return SimpleNamespace(active=True, status=None)

    client = SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(sync=SimpleNamespace(get_repo_status=get_repo_status))))
    monkeypatch.setattr(identity, "public_client", lambda did: client)

    def relay_status(r, did):
        if relay == "not-found":
            raise RepoUnavailable(did, "not-found")
        if relay == "fail":
            raise OSError("down")
        return (False, "takendown")

    monkeypatch.setattr(av, "relay_repo_status", relay_status)
    assert _REAL_REPO_STATUSES(ALICE, "https://relay.example") == expected
    assert cli._repo_statuses is not _REAL_REPO_STATUSES  # the autouse stub stays in place


# ------------------- R2-01: inactive repos are recorded without listing them


def _could_not_find_repo():
    from atproto_client.exceptions import BadRequestError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    return BadRequestError(
        Response(
            success=False,
            status_code=400,
            content=XrpcError(error="InvalidRequest", message=f"Could not find repo: {ALICE}"),
            headers={},
        )
    )


def _backfill_all(monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli, identity

    monkeypatch.setattr(av, "list_repos_by_collection", lambda relay, nsid: [ALICE])
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    return CliRunner().invoke(cli.main, ["appview", "backfill", "--all"])


def test_backfill_of_a_deactivated_repo_records_it_and_clears_the_gap(monkeypatch):
    from consentvid import cli

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    av.record_gap(conn, 1_000_000)
    conn.commit()
    listed = []

    def list_all(client, did, coll):
        listed.append(coll)
        raise _could_not_find_repo()  # what eurosky.social answers for a deactivated repo

    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(False, "deactivated"), (False, "deactivated")])
    monkeypatch.setattr(_core, "_list_all", list_all)
    r = _backfill_all(monkeypatch)
    assert r.exit_code == 0, r.output
    assert listed == []  # never listed
    assert "account deactivated; not listed" in r.output and "0 failed" in r.output
    conn = av.connect()
    assert conn.execute("SELECT active, status FROM accounts").fetchone()[:] == (0, "deactivated")
    assert av.query_consents(conn) == []
    assert av.revocations(conn) == []  # kept, hidden: reactivation re-reads it
    assert av.get_gap(conn) is None


def test_backfill_of_a_tombstoned_did_purges_it(monkeypatch):
    from consentvid import cli
    from consentvid.errors import RepoUnavailable

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()

    def tombstoned(did):
        raise RepoUnavailable(did, "deleted", "tombstoned")

    def relay_status(relay, did):
        raise RepoUnavailable(did, "not-found")

    monkeypatch.setattr(cli, "_repo_statuses", _REAL_REPO_STATUSES)
    monkeypatch.setattr(av, "relay_repo_status", relay_status)
    monkeypatch.setattr(_core, "_list_all", lambda *a: pytest.fail("listed a deleted repo"))
    r = _backfill_all_with(monkeypatch, tombstoned)
    assert r.exit_code == 0, r.output
    assert "0 failed" in r.output
    conn = av.connect()
    assert av.revocations(conn)[0]["revoke_reason"] == "account deleted"
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def _backfill_all_with(monkeypatch, public_client):
    from click.testing import CliRunner

    from consentvid import cli, identity

    monkeypatch.setattr(av, "list_repos_by_collection", lambda relay, nsid: [ALICE])
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", public_client)
    return CliRunner().invoke(cli.main, ["appview", "backfill", "--all"])


def test_undecided_status_and_a_listing_failure_is_still_a_failure(monkeypatch):
    from consentvid import cli

    conn = av.connect()
    av.record_gap(conn, 1_000_000)
    conn.commit()

    def list_all(client, did, coll):
        raise _could_not_find_repo()

    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [None, None])
    monkeypatch.setattr(_core, "_list_all", list_all)
    r = _backfill_all(monkeypatch)
    assert "1 failed" in r.output
    assert av.get_gap(av.connect()) == "1000000"


# ------------------- R2-07: a gap recorded during backfill --all survives


@pytest.mark.parametrize("mid_run_gap", [True, False])
def test_backfill_all_keeps_a_gap_recorded_while_it_ran(monkeypatch, mid_run_gap):
    from click.testing import CliRunner

    from consentvid import cli

    conn = av.connect()
    av.record_gap(conn, 1_000_000)
    conn.commit()
    monkeypatch.setattr(av, "list_repos_by_collection", lambda relay, nsid: [ALICE])

    def backfill(conn, repo, relay=None):
        if mid_run_gap:  # the live indexer, in another process
            other = av.connect()
            av.record_gap(other, 2_000_000)  # newer: the stored time stays 1_000_000
            other.commit()
        return repo, 0, 0

    monkeypatch.setattr(cli, "_backfill_repo", backfill)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "--all"])
    assert r.exit_code == 0, r.output
    gap = av.get_gap(av.connect())
    if mid_run_gap:
        assert gap == "1000000" and "a new gap was recorded during this run" in r.output
        assert "Gap repaired" not in r.output
    else:
        assert gap is None and "Gap repaired" in r.output


# ------------------- R2-19: order by min(createdAt, first sighting)


def test_future_dated_consents_do_not_push_others_past_the_limit(tmp_path, monkeypatch):
    conn = av.connect(tmp_path / "a.db")
    clock = iter(f"2026-03-0{d}T00:00:0{i}.000Z" for d in (1, 2) for i in range(10))
    monkeypatch.setattr(av, "now_iso", lambda: next(clock))
    for k in (K0, K1, K2):  # bob first, "from the future"
        av.handle_event(conn, commit(BOB, V, k, "create", video_record()))
        av.handle_event(conn, commit(BOB, C, k, "create", consent_record(did=BOB, rkey=k, createdAt="9999-12-31T00:00:00.000Z")))
    for i, k in enumerate((K0, K1, K2)):  # then alice, normally dated
        av.handle_event(conn, commit(ALICE, V, k, "create", video_record()))
        av.handle_event(conn, commit(ALICE, C, k, "create", consent_record(rkey=k, createdAt=f"2026-03-02T00:00:0{i}.000Z")))
    rows = av.query_consents(conn, limit=3)
    assert [r["did"] for r in rows] == [ALICE] * 3
    assert [r["rkey"] for r in rows] == [K2, K1, K0]
    assert rows[0]["created_at"] == "2026-03-02T00:00:02.000Z"  # kept for display
    bob = conn.execute("SELECT first_seen_at, sort_at FROM consents WHERE did = ? AND rkey = ?", (BOB, K0)).fetchone()
    assert bob["sort_at"] == bob["first_seen_at"] == "2026-03-01T00:00:01.000Z"
    # A re-upsert keeps the first sighting.
    monkeypatch.setattr(av, "now_iso", lambda: "2026-04-01T00:00:00.000Z")
    av.handle_event(conn, commit(BOB, C, K0, "update", consent_record(did=BOB, rkey=K0, createdAt="9999-12-31T00:00:00.000Z")))
    again = conn.execute("SELECT first_seen_at, sort_at, indexed_at FROM consents WHERE did = ? AND rkey = ?", (BOB, K0)).fetchone()
    assert again["first_seen_at"] == "2026-03-01T00:00:01.000Z" and again["indexed_at"] == "2026-04-01T00:00:00.000Z"


def test_migration_fills_first_seen_and_sort_at(tmp_path):
    p = tmp_path / "a.db"
    conn = setup(tmp_path)
    conn.execute("UPDATE consents SET first_seen_at = NULL, sort_at = NULL, created_at = '9999-01-01T00:00:00.000Z'")
    conn.commit()
    conn = av.connect(tmp_path / "a.db")
    row = conn.execute("SELECT first_seen_at, sort_at, indexed_at FROM consents").fetchone()
    assert row["first_seen_at"] == row["indexed_at"] == row["sort_at"]
    assert p.exists()


# ------------------- R3-03: a consent must pin its video version


def test_consent_without_video_cid_is_stored_invalid_and_never_returned(tmp_path):
    conn = av.connect(tmp_path / "a.db")
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    unpinned = consent_record()
    del unpinned["videoCid"]
    op, detail = av.handle_event(conn, commit(ALICE, C, K1, "create", unpinned, cid="bafyc1"))
    assert op == "invalid" and "videoCid is required" in detail
    assert conn.execute("SELECT valid FROM consents").fetchone()[0] == 0
    assert av.query_consents(conn) == []
    # Even a row that predates the rule (video_cid NULL, valid=1) grants nothing.
    conn.execute("UPDATE consents SET valid = 1, video_cid = NULL")
    assert av.query_consents(conn) == []


# ------------------- R3-01: relay processing statuses are not account statuses


def _lagging_backfill(monkeypatch, relay_answer, listing=None):
    from types import SimpleNamespace

    from consentvid import cli

    listing = listing or {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }
    listed = []

    def list_all(client, did, coll):
        listed.append(coll)
        return listing.get(coll, [])

    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(True, None), relay_answer])
    monkeypatch.setattr(_core, "_list_all", list_all)
    return listed


@pytest.mark.parametrize("status", ["throttled", "host-throttled", "desynchronized"])
def test_backfill_lists_a_relay_throttled_repo_but_keeps_it_hidden(monkeypatch, status):
    conn = av.connect()
    av.record_gap(conn, 1_000_000)
    conn.commit()
    listed = _lagging_backfill(monkeypatch, (False, status))
    r = _backfill_all(monkeypatch)
    assert r.exit_code == 0, r.output
    assert listed == list(RECORD_NSIDS)  # read from the PDS, not skipped as inactive
    assert f"relay reports {status}; listed from the PDS but hidden until the relay catches up" in r.output
    assert "1 repo(s) backfilled, 0 failed" in r.output and av.get_gap(av.connect()) is None
    conn = av.connect()
    acct = conn.execute("SELECT active, status, desync_since FROM accounts WHERE did = ?", (ALICE,)).fetchone()
    assert acct["active"] == 1 and acct["status"] is None and acct["desync_since"] is not None
    assert conn.execute("SELECT COUNT(*) FROM consents WHERE valid = 1").fetchone()[0] == 1
    assert av.query_consents(conn) == []  # hidden: the relay isn't forwarding its events
    assert av.revocations(conn) == []
    (row,) = av.pending_rows(conn)
    assert row["did"] == ALICE and row["reason"] == f"relay {status}: live events may not be relayed"
    assert row["next_try"] > av.now_iso()  # re-checked in an hour, not in a tight loop


def test_backfill_unhides_once_the_relay_is_active_again(monkeypatch):
    _lagging_backfill(monkeypatch, (False, "throttled"))
    assert _backfill_all(monkeypatch).exit_code == 0
    _lagging_backfill(monkeypatch, (True, None))
    r = _backfill_all(monkeypatch)
    assert r.exit_code == 0, r.output
    conn = av.connect()
    assert len(av.query_consents(conn)) == 1 and av.pending_dids(conn) == []


@pytest.mark.parametrize("status", ["takendown", "mystery"])
def test_backfill_still_treats_account_statuses_as_inactive(monkeypatch, status):
    listed = _lagging_backfill(monkeypatch, (False, status))
    r = _backfill_all(monkeypatch)
    assert r.exit_code == 0, r.output
    assert listed == [] and f"account {status}; not listed" in r.output
    assert av.connect().execute("SELECT active FROM accounts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "answers, expected",
    [
        ([(True, None), (False, "throttled")], (True, None)),
        ([(True, None), (False, "host-throttled")], (True, None)),
        ([(True, None), (False, "desynchronized")], (True, None)),
        ([None, (False, "throttled")], None),
        ([(False, "takendown"), (False, "throttled")], (False, "takendown")),
        ([(True, None), (False, None)], (False, None)),
    ],
)
def test_combine_status_ignores_relay_processing_statuses(answers, expected):
    from consentvid import cli

    assert cli._combine_status(answers) == expected


def test_relay_throttling_vouches_that_the_repo_exists(monkeypatch):
    from types import SimpleNamespace

    from atproto_client.exceptions import BadRequestError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    from consentvid import identity

    monkeypatch.setattr(identity, "_sleep", lambda s: None)

    def get_repo_status(p):
        raise BadRequestError(
            Response(success=False, status_code=400, content=XrpcError(error="RepoNotFound", message="x"), headers={})
        )

    client = SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(sync=SimpleNamespace(get_repo_status=get_repo_status))))
    monkeypatch.setattr(identity, "public_client", lambda did: client)
    monkeypatch.setattr(av, "relay_repo_status", lambda r, did: (False, "throttled"))
    # Not a deletion: the relay knows the repo (it is throttling it).
    assert _REAL_REPO_STATUSES(ALICE, "https://relay.example") == [None, (False, "throttled")]


def _acct(active, status=None):
    return {"kind": "account", "did": ALICE, "account": {"active": active, "status": status}}


@pytest.mark.parametrize("status", ["desynchronized", "throttled", "host-throttled"])
def test_account_event_with_a_relay_status_hides_without_deactivating(tmp_path, status):
    conn = setup(tmp_path)
    op, detail = av.handle_event(conn, _acct(False, status))
    assert op == "account" and status in detail
    acct = conn.execute("SELECT active, desync_since FROM accounts WHERE did = ?", (ALICE,)).fetchone()
    assert acct["active"] == 1 and acct["desync_since"] is not None
    assert av.pending_dids(conn) == [ALICE]
    assert av.query_consents(conn) == [] and av.revocations(conn) == []
    assert av.stats(conn)["inactive_accounts"] == 0


def test_account_active_after_relay_lag_queues_a_reread(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, _acct(False, "desynchronized"))
    av.clear_pending(conn, ALICE)
    av.handle_event(conn, _acct(True))
    assert av.pending_dids(conn) == [ALICE]
    assert av.query_consents(conn) == []  # still hidden until the re-read


def test_account_takedown_still_deactivates(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, _acct(False, "takendown"))
    assert conn.execute("SELECT active FROM accounts").fetchone()[0] == 0
    assert av.pending_dids(conn) == []


# ------------------- R3-02: the indexer drains pending_backfill itself


def _stub_listing(monkeypatch, listing=None):
    from types import SimpleNamespace

    from consentvid import identity

    listing = listing or {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyc1", value=consent_record())],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: r)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: listing.get(coll, []))


def test_sync_then_one_drain_tick_unhides_without_a_cli_call(monkeypatch):
    import asyncio

    from consentvid import cli

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    av.handle_event(conn, _sync())
    conn.commit()
    assert av.query_consents(conn) == []
    _stub_listing(monkeypatch)
    events = []
    assert asyncio.run(av.drain_once(conn, cli._drain_repo, lambda op, x: events.append((op, x)))) == ALICE
    assert events == [("drain", f"{ALICE}: re-read")]
    assert len(av.query_consents(conn)) == 1
    assert av.pending_dids(conn) == [] and av.stats(conn)["desynced_repos"] == 0


def test_a_failing_drain_backs_off(tmp_path):
    import asyncio

    conn = setup(tmp_path)
    av.handle_event(conn, _sync())
    conn.commit()
    calls, events = [], []

    def drain(did):
        calls.append(did)
        raise OSError("PDS down")

    assert asyncio.run(av.drain_once(conn, drain, lambda op, x: events.append(x))) == ALICE
    (row,) = av.pending_rows(conn)
    assert row["attempts"] == 1 and row["next_try"] > av.now_iso()
    assert "re-read failed (OSError: PDS down)" in events[0]
    # Not retried before next_try ...
    assert asyncio.run(av.drain_once(conn, drain)) is None and calls == [ALICE]
    # ... and a second failure doubles the delay, keeping the count.
    conn.execute("UPDATE pending_backfill SET next_try = ''")
    first_due = row["next_try"]
    asyncio.run(av.drain_once(conn, drain))
    (row,) = av.pending_rows(conn)
    assert row["attempts"] == 2 and row["next_try"] > first_due
    # A new event makes it due again but keeps the attempts.
    av.handle_event(conn, _sync(rev="3maaaaaaabaa2"))
    (row,) = av.pending_rows(conn)
    assert row["attempts"] == 2 and row["next_try"] == ""


def test_drain_backoff_is_capped():
    conn = av.connect()
    av.queue_backfill(conn, ALICE, "test")
    conn.execute("UPDATE pending_backfill SET attempts = 40")
    due = av.note_drain_failure(conn, ALICE)
    from datetime import UTC, datetime, timedelta

    assert av.parse_dt(due) <= datetime.now(UTC) + timedelta(hours=6, seconds=5)


def test_reactivation_and_sync_for_one_did_drain_once(tmp_path):
    import asyncio

    conn = setup(tmp_path)
    av.handle_event(conn, _acct(False, "deactivated"))
    av.handle_event(conn, _acct(True))
    av.handle_event(conn, _sync())
    conn.commit()
    assert av.pending_dids(conn) == [ALICE]
    calls = []

    def drain(did):
        calls.append(did)
        c = av.connect(tmp_path / "a.db")
        av.clear_pending(c, did)
        c.commit()
        c.close()

    asyncio.run(av.drain_once(conn, drain))
    assert asyncio.run(av.drain_once(conn, drain)) is None
    assert calls == [ALICE]


def test_drain_runs_in_a_thread_with_its_own_connection(monkeypatch):
    import asyncio
    import threading

    from consentvid import cli

    conn = av.connect()
    av.queue_backfill(conn, ALICE, "test")
    conn.commit()
    seen = []

    def backfill(c, repo, relay=None):
        seen.append((c, threading.get_ident()))
        av.clear_pending(c, repo)
        c.commit()
        return repo, 0, 0

    monkeypatch.setattr(cli, "_backfill_repo", backfill)
    asyncio.run(av.drain_once(conn, cli._drain_repo))
    ((used, thread),) = seen
    assert used is not conn and thread != threading.get_ident()
    assert av.pending_dids(conn) == []


def test_drain_keeps_a_relay_lagging_repo_pending_for_an_hour(monkeypatch):
    import asyncio

    from consentvid import cli

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, _acct(False, "desynchronized"))
    conn.commit()
    _stub_listing(monkeypatch)
    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(True, None), (False, "desynchronized")])
    asyncio.run(av.drain_once(conn, cli._drain_repo))
    (row,) = av.pending_rows(conn)
    assert row["attempts"] == 0 and row["next_try"] > av.now_iso()
    assert av.query_consents(conn) == []
    assert asyncio.run(av.drain_once(conn, cli._drain_repo)) is None  # not before the hour


def test_index_loop_drains_in_the_background_and_stops_cleanly(monkeypatch):
    import asyncio

    from consentvid import cli

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, K1, "create", consent_record(), cid="bafyc1"))
    conn.commit()
    _stub_listing(monkeypatch)
    drained = []

    def drain(did):
        cli._drain_repo(did)
        drained.append(did)

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                yield json.dumps(v2("sync", seq=3, did=ALICE, time="2026-11-07T10:00:00Z",
                                    sync={"did": ALICE, "rev": "3maaaaaaaaaz2", "seq": 1}))
                for _ in range(500):
                    if drained:
                        break
                    await asyncio.sleep(0.01)
                yield json.dumps(v2_commit(ALICE, V, K2, "create", video_record(), seq=4))

            return gen()

    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: Socket())
    tasks_before = []

    async def run():
        n = await av.index_loop(conn, stop_after=2, drain=drain, drain_interval=0.01)
        # Count only drain tasks: a GC'd async generator may get an aclose() task.
        tasks_before.append(sum(1 for t in asyncio.all_tasks()
                                if t.get_coro().__name__ == "_drain_loop"))
        return n

    assert asyncio.run(run()) == 2
    assert drained == [ALICE] and tasks_before == [0]  # the drain task is gone
    assert len(av.query_consents(conn)) == 1


def test_index_loop_without_drain_starts_no_task(monkeypatch):
    import asyncio

    conn = av.connect()
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: _Socket([v2_commit(ALICE, V, K1, "create", video_record())]))
    counts = []
    real = av._index_loop

    async def spy(*a, **kw):
        counts.append(len(asyncio.all_tasks()))
        return await real(*a, **kw)

    monkeypatch.setattr(av, "_index_loop", spy)
    assert asyncio.run(av.index_loop(conn, stop_after=1)) == 1
    assert counts == [1]


@pytest.mark.parametrize("flag, expect_drain", [([], True), (["--no-drain"], False)])
def test_cli_index_passes_the_drain_unless_disabled(monkeypatch, flag, expect_drain):
    from click.testing import CliRunner

    from consentvid import cli

    got = {}

    async def fake_loop(conn, **kw):
        got.update(kw)
        return 0

    monkeypatch.setattr(av, "index_loop", fake_loop)
    r = CliRunner().invoke(cli.main, ["appview", "index", "--stop-after", "1", *flag])
    assert r.exit_code == 0, r.output
    assert (got["drain"] is cli._drain_repo) if expect_drain else got["drain"] is None


def test_stats_lists_pending_rows_with_attempts():
    from click.testing import CliRunner

    from consentvid import cli

    conn = av.connect()
    av.queue_backfill(conn, ALICE, "commit chain broken (#sync)")
    av.note_drain_failure(conn, ALICE)
    conn.commit()
    assert av.stats(conn)["pending_retrying"] == 1
    r = CliRunner().invoke(cli.main, ["appview", "stats"])
    assert r.exit_code == 0, r.output
    assert f"{ALICE}: commit chain broken (#sync) (1 failed attempt(s), next try" in r.output


def test_sync_message_says_the_indexer_rereads_it(tmp_path):
    conn = setup(tmp_path)
    _, detail = av.handle_event(conn, _sync())
    assert "until the indexer re-reads the repo" in detail and "backfill --pending` forces it" in detail


def test_backfill_keeps_a_sync_that_arrived_while_it_listed(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, K1, "create", video_record()))
    av.handle_event(conn, _sync())
    conn.commit()
    listing = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/{K1}", cid=VIDEO_CID, value=video_record())], C: []}

    def list_all(client, did, coll):
        if coll == C:  # a newer #sync arrives mid-listing
            idx = av.connect()
            av.handle_event(idx, _sync(rev="3maaaaaaabaa2"))
            idx.commit()
            idx.close()
        return listing.get(coll, [])

    _backfill(monkeypatch, list_all)
    conn = av.connect()
    assert av.pending_dids(conn) == [ALICE] and av.stats(conn)["desynced_repos"] == 1
