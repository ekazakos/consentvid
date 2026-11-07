import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ALICE = "did:plc:aliceaaaaaaaaaaaaaaaaaaa"
BOB = "did:plc:bobbbbbbbbbbbbbbbbbbbbbb"
VIDEO_CID = "bafyreie7sjgnuat5jzpmmnv6wr6hiyvpa4upftobv2rllnci5pleenf2xq"
HASH = "sha256-" + "ab" * 32
# Record keys (type tid). A consent shares its video's key (sidecar rule).
K0, K1, K2, K3 = "3maaaaaaaaaa2", "3maaaaaaaaab2", "3maaaaaaaaac2", "3maaaaaaaaad2"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.consentvid or real credentials in tests."""
    monkeypatch.setenv("CONSENTVID_HOME", str(tmp_path / "home"))
    for var in (
        "CONSENTVID_HANDLE",
        "CONSENTVID_APP_PASSWORD",
        "CONSENTVID_STORAGE",
        "CONSENTVID_R2_ENDPOINT",
        "CONSENTVID_VALIDATE",
    ):
        monkeypatch.delenv(var, raising=False)
    import consentvid.cli as cli
    import consentvid.identity as identity
    import consentvid.resync as resync

    # Retries (with_retry) never really sleep in tests, and every test starts
    # with a full retry budget.
    monkeypatch.setattr(identity, "_sleep", lambda s: None)
    identity.reset_retry_budget()
    import consentvid.verify as verify

    verify._refreshed.clear()
    verify._moved.clear()

    # resync asks the relay for the account status: offline unless a test
    # restores resync._relay_lookup_impl.
    monkeypatch.setattr(resync, "_relay_lookup", lambda did: None)
    resync.clear_relay_cache()

    # Account signals (bot self-label, AI-usage preferences) and the relay
    # witness: offline unless a test restores them.
    import consentvid.signals as signals

    monkeypatch.setattr(signals, "_lookup", lambda did: signals.NONE)
    signals.clear_cache()
    monkeypatch.setattr(resync, "_witness_lookup", lambda did: None)
    resync.clear_witness_cache()
    import consentvid.appview as appview

    # Existing AppView tests predate account-signal checks; tests of the
    # gate turn it back on.
    monkeypatch.setattr(appview, "REQUIRE_SIGNAL_CHECK", False)

    monkeypatch.setattr(cli, "_revoke_sleep", lambda s: None)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(cli, "SESSION_PATH", tmp_path / "home" / "session.txt")
    monkeypatch.setattr(cli, "_load_env", lambda: None)


@pytest.fixture
def cars():
    meta = json.loads((FIXTURES / "cars.json").read_text())
    meta["present"] = (FIXTURES / "consent_present.car").read_bytes()
    meta["absent"] = (FIXTURES / "consent_absent.car").read_bytes()
    return meta


def video_record(**kw):
    rec = {
        "$type": "org.consentvid.video",
        "title": "A clip",
        "contentHash": HASH,
        "storageRef": {"kind": "r2", "value": "https://example.org/videos/x"},
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    rec.update(kw)
    return rec


def consent_record(did=ALICE, rkey=K1, **kw):
    rec = {
        "$type": "org.consentvid.consent",
        "videoUri": f"at://{did}/org.consentvid.video/{rkey}",
        "videoCid": VIDEO_CID,
        "modalities": ["video", "audio"],
        "purposes": ["training"],
        "modelClasses": ["academic"],
        "compensation": "none",
        "revocable": True,
        "authorshipAttested": True,
        "rightsBasis": "ownRecording",
        "captureProvenance": "selfAttested",
        "peoplePresent": "onlyMe",
        "adultAttested": True,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    rec.update(kw)
    return rec


V2_HASH = "bafkreiflvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvm"  # HASH as a raw-codec CID


def video_v2_record(**kw):
    rec = {
        "$type": "org.consentvid.videoV2",
        "title": "A clip",
        "contentHash": V2_HASH,
        "storage": {"$type": "org.consentvid.videoV2#r2Ref", "url": "https://example.org/videos/x"},
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    rec.update(kw)
    return rec


def video_v2_cid(**kw):
    from consentvid.records import compute_cid

    return compute_cid(video_v2_record(**kw))


def consent_v2_record(did=ALICE, rkey=K1, video_cid=None, **kw):
    rec = {
        "$type": "org.consentvid.consentV2",
        "subject": {"uri": f"at://{did}/org.consentvid.videoV2/{rkey}", "cid": video_cid or video_v2_cid()},
        "modalities": ["video", "audio"],
        "purposes": ["training"],
        "modelClasses": ["academic"],
        "compensation": "none",
        "authorshipAttested": True,
        "rightsBasis": "own-recording",
        "captureProvenance": "self-attested",
        "peoplePresent": "only-me",
        "adultAttested": True,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    rec.update(kw)
    return rec


class FakeRepo:
    """In-memory stand-in for com.atproto.repo on one PDS. Like a real PDS:
    a create under an existing key fails, swapRecord is enforced, and
    applyWrites is all-or-nothing."""

    def __init__(self, did=ALICE):
        self.did = did
        self.records = {}  # (collection, rkey) -> (cid, value)
        self.n = 0
        self.calls = []
        self.fail_apply = None  # exception applyWrites raises (nothing written)
        self.head = 1  # repo head commit counter (sync.getLatestCommit)

    def _cid(self):
        self.n += 1
        return f"bafyfake{self.n:04d}"

    def create_record(self, p):
        self.calls.append(("create", p))
        rkey = p.get("rkey") or f"r{self.n + 1}"
        if (p["collection"], rkey) in self.records:
            raise RuntimeError("InvalidRequest: Record already exists")
        cid = self._cid()
        self.records[(p["collection"], rkey)] = (cid, p["record"])
        return SimpleNamespace(uri=f"at://{self.did}/{p['collection']}/{rkey}", cid=cid)

    def put_record(self, p):
        self.calls.append(("put", p))
        key = (p["collection"], p["rkey"])
        if "swapRecord" in p and (self.records.get(key) or (None,))[0] != p["swapRecord"]:
            raise RuntimeError("InvalidSwap: Record was at another version")
        cid = self._cid()
        self.records[key] = (cid, p["record"])
        return SimpleNamespace(uri=f"at://{self.did}/{p['collection']}/{p['rkey']}", cid=cid)

    def delete_record(self, p):
        self.calls.append(("delete", p))
        self.records.pop((p["collection"], p["rkey"]), None)

    def apply_writes(self, p):
        from consentvid.records import compute_cid

        self.calls.append(("apply_writes", p))
        if self.fail_apply:
            raise self.fail_apply
        if "swapCommit" in p and p["swapCommit"] != self.head_cid():
            raise RuntimeError("InvalidSwap: Commit was at another version")
        staged, results = dict(self.records), []
        for w in p["writes"]:
            key = (w["collection"], w["rkey"])
            if w["$type"].endswith("#create"):
                if key in staged:
                    raise RuntimeError("InvalidRequest: Record already exists")
                cid = compute_cid(w["value"])
                staged[key] = (cid, w["value"])
                results.append(SimpleNamespace(uri=f"at://{self.did}/{key[0]}/{key[1]}", cid=cid))
            elif w["$type"].endswith("#delete"):
                if key not in staged:
                    raise RuntimeError("InvalidRequest: Could not find record")
                del staged[key]
                results.append(SimpleNamespace())
        self.records = staged
        self.head += 1
        return SimpleNamespace(commit=SimpleNamespace(cid=self.head_cid(), rev="3maaaaaaaaaz2"), results=results)

    def head_cid(self):
        return f"bafycommit{self.head}"

    def get_latest_commit(self, p):
        self.calls.append(("get_latest_commit", p))
        return SimpleNamespace(cid=self.head_cid(), rev="3maaaaaaaaaz2")

    def get_record(self, p):
        try:
            cid, value = self.records[(p["collection"], p["rkey"])]
        except KeyError:
            raise RuntimeError("RecordNotFound") from None
        return SimpleNamespace(uri=f"at://{self.did}/{p['collection']}/{p['rkey']}", cid=cid, value=value)

    def list_records(self, p):
        recs = [
            SimpleNamespace(uri=f"at://{self.did}/{c}/{k}", cid=cid, value=v)
            for (c, k), (cid, v) in self.records.items()
            if c == p["collection"]
        ]
        return SimpleNamespace(records=recs, cursor=None)


@pytest.fixture
def fake_client():
    repo = FakeRepo()
    client = SimpleNamespace(me=SimpleNamespace(did=ALICE, handle="alice.test"))
    client.com = SimpleNamespace(atproto=SimpleNamespace(repo=repo, sync=repo))
    client.repo = repo
    return client


class MockHttpx:
    """Stands in for the httpx module inside consentvid.storage: every client
    and module-level call goes through one MockTransport(handler), so the
    real code paths (sessions, ``with``/close, raise_for_status) run."""

    def __init__(self, handler):
        import httpx

        self._httpx = httpx
        self.transport = httpx.MockTransport(handler)
        self.ConnectError = httpx.ConnectError
        self.HTTPError = httpx.HTTPError

    def Client(self, **kw):  # noqa: N802 — mirrors httpx.Client
        return self._httpx.Client(transport=self.transport, **kw)

    def _call(self, method, url, **kw):
        kw.pop("timeout", None)
        with self._httpx.Client(transport=self.transport) as c:
            return c.request(method, url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)
