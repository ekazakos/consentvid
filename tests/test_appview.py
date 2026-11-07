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
        ev(ALICE, V, "v1", video_record(), 1_000),
        ev(ALICE, C, "c1", consent_record(), 2_000, cid="bafyc1"),
        ev(mallory, C, "x", consent_record(did=mallory, purposes=[{}]), 3_000),  # unhashable items
        ev(mallory, C, "y", consent_record(did=mallory, createdAt="9999-12-31T23:59:59-05:00"), 3_100),
        ev(mallory, C, "boom", consent_record(did=mallory), 3_200),  # handle_event raises (below)
        "not json {",
        "[1, 2, 3]",
        json.dumps({"kind": "commit", "time_us": "soon", "commit": []}),
        ev(ALICE, C, "c1", None, 4_000, op="delete"),  # Alice revokes after the poison
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
    assert conn.execute("SELECT revoked_at FROM consents WHERE rkey = 'c1'").fetchone()[0] is not None
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
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyalice"))
    conn.commit()
    before = dict(conn.execute("SELECT cid, video_uri, revoked_at FROM consents WHERE did = ?", (ALICE,)).fetchone())

    forged = consent_record(did=ALICE, purposes=["training", "evaluation"])
    listing = {
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/c1", cid="bafyforged", value=forged)],
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid="bafyforgedv", value=video_record(title="x"))],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: mallory)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: listing[coll])
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
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: [])
    result = CliRunner().invoke(cli.main, ["appview", "backfill", "bad.test", "alice.test"])
    assert result.exit_code == 0 and "alice.test: 0 record(s) indexed" in result.output


def test_equal_created_at_tie_breaks_on_rkey(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, "c2", "create", consent_record(purposes=["evaluation"]), cid="bafyc2"))
    assert [r["rkey"] for r in av.query_consents(conn)] == ["c2"]


# ------------------------------- R2-02: invalid newer consent supersedes


def test_invalid_newer_consent_hides_the_older_one(tmp_path):
    conn = setup(tmp_path, modelClasses=["academic", "commercial"])
    assert len(av.query_consents(conn, model_classes=["commercial"])) == 1
    newer = consent_record(createdAt="2026-06-01T00:00:00+00:00", authorshipAttested=False)
    op, _ = av.handle_event(conn, commit(ALICE, C, "c2", "create", newer, cid="bafyc2"))
    assert op == "invalid"
    assert av.query_consents(conn) == []
    assert av.query_consents(conn, model_classes=["commercial"]) == []
    # Even the "show everything" listing never returns the invalid row itself.
    rows = av.query_consents(conn, include_revoked=True, include_expired=True)
    assert [r["rkey"] for r in rows] == ["c1"]
    # Deleting the invalid record lets the older consent govern again.
    av.handle_event(conn, commit(ALICE, C, "c2", "delete"))
    assert [r["rkey"] for r in av.query_consents(conn, model_classes=["commercial"])] == ["c1"]


def test_unparseable_created_at_counts_as_newest(tmp_path):
    conn = setup(tmp_path)
    av.handle_event(conn, commit(ALICE, C, "c2", "create", consent_record(createdAt="soon"), cid="bafyc2"))
    assert av.query_consents(conn) == []


def test_older_invalid_consent_does_not_hide_newer(tmp_path):
    conn = setup(tmp_path)
    older = consent_record(createdAt="2025-01-01T00:00:00Z", revocable=False)
    av.handle_event(conn, commit(ALICE, C, "c0", "create", older, cid="bafyc0"))
    assert [r["rkey"] for r in av.query_consents(conn)] == ["c1"]


def test_appview_export_skips_superseded_by_invalid(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyc1"))
    newer = consent_record(createdAt="2026-06-01T00:00:00Z", adultAttested=False)
    av.handle_event(conn, commit(ALICE, C, "c2", "create", newer, cid="bafyc2"))
    conn.commit()
    monkeypatch.setattr(identity, "display_name", lambda did: did)
    out = tmp_path / "m.json"
    r = CliRunner().invoke(cli.main, ["appview", "export", str(out)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["distribution"] == []


def test_foreign_invalid_consent_cannot_supersede(tmp_path):
    """Bob's invalid consent naming Alice's video must not hide Alice's."""
    conn = setup(tmp_path)
    newer = consent_record(did=ALICE, createdAt="2026-06-01T00:00:00Z")
    av.handle_event(conn, commit(BOB, C, "x", "create", newer))
    assert [r["rkey"] for r in av.query_consents(conn)] == ["c1"]


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
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyc1"))
    conn.commit()
    delete = commit(ALICE, C, "c1", "delete", t=5_000)
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
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    conn.commit()
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/c1", cid="bafyc1", value=consent_record())],
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
        return listing[coll]

    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_list_all", list_all)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0 and "2 record(s) indexed" in r.output, r.output
    assert fetches == [V, C]


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
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(modelClasses=["commercial"]), cid="bafyc1"))
    conn.commit()
    assert len(av.query_consents(conn, model_classes=["commercial"])) == 1
    rewritten = consent_record(modelClasses=["commercial"], authorshipAttested=False)
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/c1", cid="bafyc1b", value=rewritten)],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: listing[coll])
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0, r.output
    conn = av.connect()
    assert av.query_consents(conn, model_classes=["commercial"]) == []
    assert [row["uri"] for row in av.revocations(conn)] == [f"at://{ALICE}/{C}/c1"]


def test_backfill_of_invalid_video_rewrite_removes_it(monkeypatch):
    from types import SimpleNamespace

    from click.testing import CliRunner

    from consentvid import cli, identity

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyc1"))
    conn.commit()
    bad_video = video_record(storageRef={"kind": "url", "value": "https://127.1/x"})
    listing = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid="bafyv2", value=bad_video)],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/c1", cid="bafyc1", value=consent_record())],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: listing[coll])
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
    monkeypatch.setattr(cli, "_list_all", list_all)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0, r.output
    return r


def test_backfill_does_not_undo_concurrent_revocation(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyc1"))
    conn.commit()
    stale = {
        V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid=VIDEO_CID, value=video_record())],
        C: [SimpleNamespace(uri=f"at://{ALICE}/{C}/c1", cid="bafyc1", value=consent_record())],
    }

    def list_all(client, did, coll):
        if coll == C:  # the contributor deletes c1; `appview index` applies it mid-listing
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, C, "c1", "delete", t=9_000_000))
            av.set_cursor(idx, 9_000_000)
            idx.commit()
            idx.close()
        return stale[coll]

    r = _backfill(monkeypatch, list_all)
    assert "changed during backfill" in r.output
    conn = av.connect()
    assert [x["uri"] for x in av.query_consents(conn)] == []
    assert [x["uri"] for x in av.revocations(conn)] == [f"at://{ALICE}/{C}/c1"]


def test_backfill_does_not_sweep_consent_created_during_listing(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    conn.commit()
    stale = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid=VIDEO_CID, value=video_record())], C: []}

    def list_all(client, did, coll):
        if coll == C:  # a new consent arrives through the firehose after our listing
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, C, "c2", "create", consent_record(), cid="bafyc2", t=9_000_000))
            idx.commit()
            idx.close()
        return stale[coll]

    _backfill(monkeypatch, list_all)
    conn = av.connect()
    assert [x["uri"] for x in av.query_consents(conn)] == [f"at://{ALICE}/{C}/c2"]
    assert av.revocations(conn) == []


def test_backfill_does_not_resurrect_video_deleted_during_listing(monkeypatch):
    from types import SimpleNamespace

    conn = av.connect()
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    conn.commit()
    stale = {V: [SimpleNamespace(uri=f"at://{ALICE}/{V}/v1", cid=VIDEO_CID, value=video_record())], C: []}

    def list_all(client, did, coll):
        if coll == V:
            idx = av.connect()
            av.handle_event(idx, commit(ALICE, V, "v1", "delete", t=9_000_000))
            idx.commit()
            idx.close()
        return stale[coll]

    _backfill(monkeypatch, list_all)
    assert av.connect().execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


# ------------------ R3-08: implausible Jetstream time_us is never the cursor


def test_implausible_time_us_does_not_move_the_cursor(tmp_path, monkeypatch):
    import asyncio

    conn = av.connect(tmp_path / "a.db")
    av.set_cursor(conn, 1_000)
    conn.commit()
    stream = [
        json.dumps({**commit(ALICE, V, "v1", "create", video_record()), "time_us": 2**62}),
        json.dumps({**commit(ALICE, V, "v2", "create", video_record()), "time_us": 0}),
        json.dumps({**commit(ALICE, V, "v3", "create", video_record()), "time_us": True}),
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
    av.handle_event(conn, commit(ALICE, V, "v1", "create", video_record()))
    av.handle_event(conn, commit(ALICE, C, "c1", "create", consent_record(), cid="bafyc1"))
    av.set_cursor(conn, 1_000_000)
    conn.commit()
    big = json.dumps(commit(ALICE, C, "spam", "create", {"pad": "x" * (200 * 1024)}, t=2_000_000))
    delete = json.dumps(commit(ALICE, C, "c1", "delete", t=3_000_000))
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
    assert all("maxMessageSizeBytes=65536" in p for p in paths)
    assert sum("cursor=" in p for p in paths) == av.MAX_1009_RETRIES  # bounded replays
    assert "cursor=" not in paths[-1]  # then a live tail
    assert any(op == "gap" and "1000000" in x and "backfill" in x for op, x in events)
    assert av.get_gap(conn) == "1000000" and av.stats(conn)["gap_since"]
    assert av.query_consents(conn) == [] and len(av.revocations(conn)) == 1
