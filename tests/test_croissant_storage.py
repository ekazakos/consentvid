import json
import logging
from pathlib import Path

import pytest

from conftest import ALICE, K1, VIDEO_CID, consent_record, video_record
from consentvid.croissant import LICENSE_URL, Clip, build_manifest
from consentvid.policy import covers
from consentvid.storage import public_url


def clip(storage=None, **ckw):
    v = video_record(mimeType="video/mp4", **({"storageRef": storage} if storage else {}))
    return Clip(
        f"at://{ALICE}/org.consentvid.consent/{K1}",
        "bafyc",
        consent_record(**ckw),
        f"at://{ALICE}/org.consentvid.video/{K1}",
        VIDEO_CID,
        v,
        ALICE,
        "alice.test",
    )


def test_manifest_shape():
    m, skipped = build_manifest([clip(), clip({"kind": "local", "value": "x.mov"})], name="t")
    assert len(m["distribution"]) == 1 and len(skipped) == 1
    fo = m["distribution"][0]
    assert m["license"] == LICENSE_URL and m["isLiveDataset"] is True
    assert fo["encodingFormat"] == "video/mp4" and fo["sha256"] == "ab" * 32
    assert fo["cv:consentReceipt"]["cid"] == "bafyc" and fo["cv:videoCid"] == VIDEO_CID
    assert not any(k.startswith("cr:consent") for k in fo)


def test_rai_limitations_do_not_claim_attested_is_verified():
    """R3-05"""
    m, _ = build_manifest([clip()], name="d")
    assert "unless captureProvenance is 'attested'" not in m["rai:dataLimitations"]
    assert "does not yet verify capture-time provenance" in m["rai:dataLimitations"]


def test_manifest_validates_as_croissant(tmp_path):
    mlc = pytest.importorskip("mlcroissant")
    logging.disable(logging.WARNING)
    m, _ = build_manifest([clip()], name="test-dataset")
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    mlc.Dataset(jsonld=str(p))


def test_policy_all_of():
    c = consent_record(purposes=["training", "evaluation"])
    assert covers(c, purposes=["training"]) and not covers(c, purposes=["training", "benchmarking"])


CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
CID0 = "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"
HASH = "sha256-" + "ab" * 32


def test_public_url(monkeypatch):
    monkeypatch.setenv("CONSENTVID_IPFS_GATEWAY", "https://gw.example/")
    assert public_url({"kind": "ipfs", "value": f"ipfs://{CID}"}) == f"https://gw.example/ipfs/{CID}"
    assert public_url({"kind": "ipfs", "value": CID0}) == f"https://gw.example/ipfs/{CID0}"
    assert public_url({"kind": "local", "value": "x.mov"}) is None
    assert public_url({"kind": "r2", "value": "https://r2.example/x"}) == "https://r2.example/x"


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "ipfs", "value": "ipfs://bafyX"},
        {"kind": "ipfs", "value": "../ipns/k51x"},
        {"kind": "r2", "value": "/Users/x/v.mp4"},
        {"kind": "url", "value": "file:///etc/passwd"},
        {"kind": "url", "value": "javascript:alert(1)"},
        {"kind": "peertube", "value": "http://pt.example/w/abc"},
        {"kind": "somethingNew", "value": "https://x.example/v"},
        {"kind": "url"},
        None,
    ],
)
def test_public_url_refuses_bad_values(ref):
    assert public_url(ref) is None


def test_public_url_gateway_must_be_https(monkeypatch):
    from consentvid.errors import ConfigError

    monkeypatch.setenv("CONSENTVID_IPFS_GATEWAY", "http://gw.example")
    with pytest.raises(ConfigError):
        public_url({"kind": "ipfs", "value": CID})


# ------------------------------------------------------ R1-09: delete_bytes


def client_error(status, code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject")


class FakeS3:
    def __init__(self, exists=False):
        self.exists, self.deleted, self.uploaded = exists, [], []
        self.head_error = None

    def head_object(self, Bucket, Key):
        if self.head_error is not None:
            raise self.head_error
        if not self.exists:
            raise client_error(404, "404")

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploaded.append(key)

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


@pytest.fixture
def r2(monkeypatch):
    from consentvid import storage

    for k, v in {
        "CONSENTVID_R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
        "CONSENTVID_R2_BUCKET": "b",
        "CONSENTVID_R2_ACCESS_KEY_ID": "k",
        "CONSENTVID_R2_SECRET_ACCESS_KEY": "s",
        "CONSENTVID_R2_PUBLIC_URL": "https://pub-x.r2.dev",
    }.items():
        monkeypatch.setenv(k, v)
    s3 = FakeS3()
    monkeypatch.setattr(storage, "_r2", lambda: s3)
    return s3


@pytest.mark.parametrize(
    "value",
    [
        "https://pub-x.r2.dev/site/index.html",
        f"https://pub-x.r2.dev/videos/{'cd' * 32}",
        f"https://pub-x.r2.dev/videos/{HASH}/../site/index.html",
        f"https://other.r2.dev/videos/{HASH}",
    ],
)
def test_delete_r2_only_this_clips_object(r2, value):
    from consentvid import storage
    from consentvid.errors import ConsentVidError

    with pytest.raises(ConsentVidError, match="not deleting"):
        storage.delete_bytes({"kind": "r2", "value": value}, HASH)
    assert r2.deleted == []


def test_delete_r2_legitimate(r2):
    from consentvid import storage

    storage.delete_bytes({"kind": "r2", "value": f"https://pub-x.r2.dev/videos/{HASH}"}, HASH)
    assert r2.deleted == [f"videos/{HASH}"]


class FakeHttpx:
    def __init__(self):
        self.calls = []

    def _resp(self):
        from types import SimpleNamespace

        return SimpleNamespace(raise_for_status=lambda: None)

    def delete(self, url, **kw):
        self.calls.append(("delete", url))
        return self._resp()

    def post(self, url, **kw):
        self.calls.append(("post", url, kw.get("params")))
        return self._resp()


@pytest.mark.parametrize("value", ["ipfs://../../v3/files/X", "../ipns/k51x", "bafy/../x", "QmShort"])
def test_unpin_ipfs_refuses_non_cids(monkeypatch, value):
    from consentvid import storage
    from consentvid.errors import ConsentVidError

    fake = FakeHttpx()
    monkeypatch.setattr(storage, "_httpx", lambda: fake)
    monkeypatch.setenv("CONSENTVID_PINATA_JWT", "jwt")
    with pytest.raises(ConsentVidError):
        storage.delete_bytes({"kind": "ipfs", "value": value}, HASH)
    assert fake.calls == []


def test_unpin_ipfs_legitimate(monkeypatch):
    from consentvid import storage

    fake = FakeHttpx()
    monkeypatch.setattr(storage, "_httpx", lambda: fake)
    monkeypatch.setenv("CONSENTVID_PINATA_JWT", "jwt")
    storage.delete_bytes({"kind": "ipfs", "value": f"ipfs://{CID}"}, HASH)
    assert fake.calls == [("delete", f"https://api.pinata.cloud/pinning/unpin/{CID}")]


@pytest.mark.parametrize(
    "value",
    [
        "https://pt.example.evil.com/w/abcdefgh12",
        "https://pt.example/api/v1/videos/abcdefgh12",
        "https://pt.example/w/abc/../../api",
        "https://pt.example/w/abcdefgh12?x=1",
        "http://pt.example/w/abcdefgh12",
        "https://user@pt.example/w/abcdefgh12",
    ],
)
def test_delete_peertube_only_watch_urls_on_our_instance(monkeypatch, value):
    from consentvid import storage
    from consentvid.errors import ConsentVidError

    monkeypatch.setenv("CONSENTVID_PEERTUBE_URL", "https://pt.example")
    sessions = []
    monkeypatch.setattr(storage, "_peertube_session", lambda: sessions.append(1))
    with pytest.raises(ConsentVidError):
        storage.delete_bytes({"kind": "peertube", "value": value}, HASH)
    assert sessions == []  # refused before logging in


PT_HASH = "sha256-" + "cd" * 32


def _peertube(monkeypatch, *, video=None, me=None):
    """The REAL _peertube_session/upload/delete code against a MockTransport
    PeerTube (R3-11: no 'with client:' after the client has been used)."""
    import httpx

    from conftest import MockHttpx
    from consentvid import storage

    monkeypatch.setenv("CONSENTVID_PEERTUBE_URL", "https://PT.example/")
    monkeypatch.setenv("CONSENTVID_PEERTUBE_USERNAME", "alice")
    monkeypatch.setenv("CONSENTVID_PEERTUBE_PASSWORD", "pw")
    monkeypatch.delenv("CONSENTVID_PEERTUBE_CHANNEL_ID", raising=False)
    me = me or {"username": "alice", "videoChannels": [{"id": 7}]}
    video = video or {
        "account": {"name": "alice", "host": "pt.example"},
        "channel": {"id": 7},
        "support": f"consentvid:{PT_HASH}",
    }
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        p = (request.method, request.url.path)
        if p == ("GET", "/api/v1/oauth-clients/local"):
            return httpx.Response(200, json={"client_id": "a", "client_secret": "b"})
        if p == ("POST", "/api/v1/users/token"):
            return httpx.Response(200, json={"access_token": "t"})
        assert request.headers.get("authorization") == "Bearer t"
        if p == ("GET", "/api/v1/users/me"):
            return httpx.Response(200, json=me)
        if p == ("GET", "/api/v1/videos/abcdefgh12"):
            return httpx.Response(200, json=video)
        if p == ("DELETE", "/api/v1/videos/abcdefgh12"):
            return httpx.Response(204)
        if p == ("POST", "/api/v1/videos/upload"):
            body = request.read()
            assert f"consentvid:{PT_HASH}".encode() in body  # the ownership marker
            return httpx.Response(200, json={"video": {"shortUUID": "abcdefgh12"}})
        return httpx.Response(404)

    monkeypatch.setattr(storage, "_httpx", lambda: MockHttpx(handler))
    return seen


LOGIN = [("GET", "/api/v1/oauth-clients/local"), ("POST", "/api/v1/users/token")]


def test_delete_peertube_legitimate(monkeypatch):
    from consentvid import storage

    seen = _peertube(monkeypatch)
    storage.delete_bytes({"kind": "peertube", "value": "https://pt.example/w/abcdefgh12"}, PT_HASH)
    assert seen == LOGIN + [
        ("GET", "/api/v1/users/me"),
        ("GET", "/api/v1/videos/abcdefgh12"),
        ("DELETE", "/api/v1/videos/abcdefgh12"),
    ]


def test_upload_to_peertube_real_session(monkeypatch, tmp_path):
    from consentvid import storage

    seen = _peertube(monkeypatch)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    up = storage.upload("peertube", f, content_hash=PT_HASH, title="t")
    assert up.value.lower() == "https://pt.example/w/abcdefgh12" and up.created
    assert seen == LOGIN + [("GET", "/api/v1/users/me"), ("POST", "/api/v1/videos/upload")]


@pytest.mark.parametrize(
    "video,why",
    [
        ({"account": {"name": "bob", "host": "pt.example"}, "channel": {"id": 7}}, "not owned"),
        ({"account": {"name": "alice", "host": "other.example"}, "channel": {"id": 7}}, "not owned"),
        ({"account": {"name": "alice", "host": "pt.example"}, "channel": {"id": 8}}, "not owned"),
        ({"account": {"name": "alice", "host": "pt.example"}, "channel": {"id": 7}}, "marker"),
        ({"account": {"name": "alice", "host": "pt.example"}, "channel": {"id": 7},
          "support": f"consentvid:sha256-{'ee' * 32}"}, "marker"),
    ],
)
def test_delete_peertube_refuses_videos_that_are_not_this_clips_upload(monkeypatch, video, why):
    """R3-10: a moderator/admin account must not be steered into deleting
    another account's (or another clip's) video."""
    from consentvid import storage
    from consentvid.errors import ConsentVidError

    video.setdefault("support", "")
    seen = _peertube(monkeypatch, video=video)
    with pytest.raises(ConsentVidError, match=why):
        storage.delete_bytes({"kind": "peertube", "value": "https://pt.example/w/abcdefgh12"}, PT_HASH)
    assert not any(m == "DELETE" for m, _ in seen)


# --------------------------------------------- R1-10: upload ownership


def test_upload_to_r2_reports_whether_it_created_the_object(r2):
    from consentvid import storage

    up = storage.upload_to_r2(Path(__file__), HASH)
    assert up.created and up.value == f"https://pub-x.r2.dev/videos/{HASH}" and r2.uploaded
    r2.exists, r2.uploaded = True, []
    up = storage.upload_to_r2(Path(__file__), HASH)
    assert not up.created and r2.uploaded == []


@pytest.mark.parametrize(
    "error",
    [
        lambda: client_error(503, "ServiceUnavailable"),
        lambda: client_error(403, "AccessDenied"),
        lambda: TimeoutError("read timed out"),
    ],
)
def test_upload_to_r2_head_failure_is_not_absence(r2, error):
    """R2-15: only a definite 404 means 'absent, ours to create (and delete)'."""
    from consentvid import storage

    r2.head_error = error()
    with pytest.raises(Exception):  # noqa: B017 — whatever head_object raised
        storage.upload_to_r2(Path(__file__), HASH)
    assert r2.uploaded == [] and r2.deleted == []


@pytest.mark.parametrize("status,code", [(404, "404"), (404, "Whatever"), (400, "NoSuchKey"), (400, "NotFound")])
def test_upload_to_r2_not_found_creates(r2, status, code):
    from consentvid import storage

    r2.head_error = client_error(status, code)
    assert storage.upload_to_r2(Path(__file__), HASH).created and r2.uploaded


# ---------------------------- R3-09: IPFS 'created' must be proven, not assumed


def _pinata(monkeypatch, body):
    import httpx

    from conftest import MockHttpx
    from consentvid import storage

    monkeypatch.setenv("CONSENTVID_PINATA_JWT", "jwt")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/pinning/pinFileToIPFS":
            return httpx.Response(200, json={"IpfsHash": CID, **body})
        return httpx.Response(200, json={})

    monkeypatch.setattr(storage, "_httpx", lambda: MockHttpx(handler))
    return seen


@pytest.mark.parametrize("body,created", [({"isDuplicate": True}, False), ({}, False),
                                          ({"isDuplicate": "false"}, False), ({"isDuplicate": False}, True)])
def test_pinata_created_only_when_not_duplicate(monkeypatch, tmp_path, body, created):
    from consentvid import storage

    _pinata(monkeypatch, body)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert storage.upload("ipfs", f, content_hash=HASH, title="t") == storage.Upload(CID, created)


def _kubo(monkeypatch, pin_ls):
    import httpx

    from conftest import MockHttpx
    from consentvid import storage

    monkeypatch.delenv("CONSENTVID_PINATA_JWT", raising=False)
    monkeypatch.setenv("CONSENTVID_IPFS_API", "http://127.0.0.1:5001")
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v0/add":
            return httpx.Response(200, text=json.dumps({"Name": "v.mp4", "Hash": CID}) + "\n")
        if request.url.path == "/api/v0/pin/ls":
            return pin_ls()
        return httpx.Response(200, json={})

    monkeypatch.setattr(storage, "_httpx", lambda: MockHttpx(handler))
    return seen


@pytest.mark.parametrize(
    "pin_ls,created",
    [
        (lambda: __import__("httpx").Response(200, json={"Keys": {CID: {"Type": "recursive"}}}), False),
        (lambda: __import__("httpx").Response(500, json={"Message": f"path '/ipfs/{CID}' is not pinned"}), True),
        (lambda: __import__("httpx").Response(500, json={"Message": "something else broke"}), False),
        (lambda: __import__("httpx").Response(500, text="not json"), False),
    ],
)
def test_kubo_created_only_when_not_already_pinned(monkeypatch, tmp_path, pin_ls, created):
    from consentvid import storage

    seen = _kubo(monkeypatch, pin_ls)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert storage.upload("ipfs", f, content_hash=HASH, title="t") == storage.Upload(CID, created)
    assert [p for p, _ in seen] == ["/api/v0/add", "/api/v0/pin/ls", "/api/v0/add"]
    assert seen[0][1].get("only-hash") == "true" and seen[2][1].get("pin") == "true"


# ------------------------------------------- R1-06: no silent fallbacks


def test_ipfs_without_configuration_refuses(monkeypatch, tmp_path):
    from consentvid import storage
    from consentvid.errors import ConfigError

    monkeypatch.delenv("CONSENTVID_PINATA_JWT", raising=False)
    monkeypatch.delenv("CONSENTVID_IPFS_API", raising=False)
    calls = []
    monkeypatch.setattr(storage, "_httpx", lambda: calls.append(1) or FakeHttpx())
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(ConfigError):
        storage.add_to_ipfs(f)
    with pytest.raises(ConfigError):
        storage.upload("ipfs", f, content_hash=HASH, title="t")


def test_upload_refuses_unconfigured_backend(monkeypatch, tmp_path):
    from consentvid import storage
    from consentvid.errors import ConfigError

    monkeypatch.delenv("CONSENTVID_R2_ENDPOINT", raising=False)
    with pytest.raises(ConfigError):
        storage.upload("r2", tmp_path, content_hash=HASH, title="t")


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://pt.example", True),
        ("http://pt.example", False),
        ("http://localhost:9000", False),
        ("ftp://pt.example", False),
    ],
)
def test_require_https(monkeypatch, url, ok):
    from consentvid import storage
    from consentvid.errors import ConfigError

    monkeypatch.delenv("CONSENTVID_ALLOW_INSECURE", raising=False)
    if ok:
        assert storage.require_https("X", url) == url
    else:
        with pytest.raises(ConfigError):
            storage.require_https("X", url)
    monkeypatch.setenv("CONSENTVID_ALLOW_INSECURE", "1")
    assert storage.require_https("X", "http://localhost:9000") == "http://localhost:9000"
    with pytest.raises(ConfigError):
        storage.require_https("X", "http://evil.example")


def test_manifest_cites_consentvid_not_a_client_app():
    """R3-14: the dataset URL is ConsentVid's, each creator is their DID."""
    from conftest import ALICE, consent_record, video_record
    from consentvid.croissant import Clip, build_manifest

    clip = Clip(f"at://{ALICE}/org.consentvid.consent/3maaaaaaaaab2", "bafyc", consent_record(),
                f"at://{ALICE}/org.consentvid.video/3maaaaaaaaab2", "bafyv", video_record(), ALICE, "alice.test")
    m, _ = build_manifest([clip], name="t")
    assert "bsky.app" not in json.dumps(m)
    assert m["url"] == "https://consentvid.org" and m["citeAs"].endswith("https://consentvid.org")
    assert m["creator"] == [{"@type": "sc:Person", "name": "alice.test", "identifier": ALICE, "sameAs": f"at://{ALICE}"}]
