import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ALICE = "did:plc:aliceaaaaaaaaaaaaaaaaaaa"
BOB = "did:plc:bobbbbbbbbbbbbbbbbbbbbbb"
VIDEO_CID = "bafyreie7sjgnuat5jzpmmnv6wr6hiyvpa4upftobv2rllnci5pleenf2xq"
HASH = "sha256-" + "ab" * 32


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


def consent_record(did=ALICE, rkey="v1", **kw):
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
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    rec.update(kw)
    return rec


class FakeRepo:
    """In-memory stand-in for com.atproto.repo on one PDS."""

    def __init__(self, did=ALICE):
        self.did = did
        self.records = {}  # (collection, rkey) -> (cid, value)
        self.n = 0
        self.calls = []

    def _cid(self):
        self.n += 1
        return f"bafyfake{self.n:04d}"

    def create_record(self, p):
        self.calls.append(("create", p))
        rkey = p.get("rkey") or f"r{self.n + 1}"
        cid = self._cid()
        self.records[(p["collection"], rkey)] = (cid, p["record"])
        return SimpleNamespace(uri=f"at://{self.did}/{p['collection']}/{rkey}", cid=cid)

    def put_record(self, p):
        self.calls.append(("put", p))
        cid = self._cid()
        self.records[(p["collection"], p["rkey"])] = (cid, p["record"])
        return SimpleNamespace(uri=f"at://{self.did}/{p['collection']}/{p['rkey']}", cid=cid)

    def delete_record(self, p):
        self.calls.append(("delete", p))
        self.records.pop((p["collection"], p["rkey"]), None)

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
    client.com = SimpleNamespace(atproto=SimpleNamespace(repo=repo))
    client.repo = repo
    return client
