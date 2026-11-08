"""Videos stored as a blob in the contributor's own PDS (videoV2 #pdsBlobRef)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ALICE, BOB, K1, consent_v2_record, video_v2_record
from consentvid import _core, bsky, storage, versions
from consentvid import resync as rs
from consentvid.croissant import Clip, _file_object, build_manifest
from consentvid.records import (
    CONSENT_V2_NSID,
    VIDEO_V2_NSID,
    compute_cid,
    raw_cid_from_sha256,
    same_container,
)
from consentvid.validate import validate_in_repo, validate_record
from consentvid.verify import VerifiedRecord

V2, C2 = VIDEO_V2_NSID, CONSENT_V2_NSID
CLEAN = b"clean"  # what the faked sanitiser writes (test_cli._fake_media)
BLOB = raw_cid_from_sha256("sha256-" + hashlib.sha256(CLEAN).hexdigest())
OTHER_BLOB = raw_cid_from_sha256("sha256-" + "cd" * 32)
PDS = "https://pds.example"
ROOT = Path(__file__).resolve().parent.parent


def blob(cid=BLOB, mime="video/mp4", size=len(CLEAN)):
    return {"$type": "blob", "ref": {"$link": cid}, "mimeType": mime, "size": size}


def pds_storage(**kw):
    return {"$type": f"{V2}#pdsBlobRef", "blob": blob(**kw)}


def pds_video(storage_kw=None, **kw):
    rec = video_v2_record(contentHash=BLOB, storage=pds_storage(**(storage_kw or {})), mimeType="video/mp4",
                          size=len(CLEAN))
    rec.update(kw)
    return rec


@pytest.fixture(autouse=True)
def _pds(monkeypatch):
    monkeypatch.setattr(bsky, "pds_of", lambda did: PDS)


# ------------------------------------------------------------ validation ---


def test_valid_pds_video_record():
    assert validate_in_repo(V2, pds_video(), ALICE, rkey=K1) == []


@pytest.mark.parametrize(
    "storage_kw,kw,needle",
    [
        ({"cid": OTHER_BLOB}, {}, "must equal contentHash"),
        ({"size": 6}, {}, "differs from size"),
        ({"mime": "video/webm"}, {}, "same container"),
        ({"mime": "image/png"}, {}, "is not one of ['video/*']"),
        ({"size": 300_000_001}, {"size": 300_000_001}, "exceeds maxSize"),
        ({"cid": "bafyreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy"}, {}, "raw video bytes"),
        ({"cid": "not-a-cid"}, {}, "not a CID"),
    ],
)
def test_invalid_pds_storage(storage_kw, kw, needle):
    errors = validate_record(V2, pds_video(storage_kw, **kw))
    assert any(needle in e for e in errors), errors


def test_blob_ref_shape_is_checked():
    rec = pds_video()
    rec["storage"]["blob"] = {"cid": BLOB, "mimeType": "video/mp4"}  # legacy blob form
    assert any("$type 'blob'" in e for e in validate_record(V2, rec))
    rec["storage"] = {"$type": f"{V2}#pdsBlobRef"}
    assert any("missing required field" in e for e in validate_record(V2, rec))


def test_container_aliases():
    assert same_container("video/x-matroska", "video/matroska")
    assert same_container("video/x-msvideo", "video/vnd.avi")
    assert same_container("video/mp4", "video/x-m4v")
    assert not same_container("video/mp4", "video/quicktime")
    rec = pds_video({"mime": "video/matroska"}, mimeType="video/x-matroska")
    assert validate_record(V2, rec) == []


def test_a_verified_read_validates():
    """A CAR read decodes the blob ref's link as binary CID bytes."""
    import libipld

    rec = pds_video()
    rec["storage"]["blob"]["ref"] = _cid_bytes(BLOB)
    assert libipld.encode_cid(rec["storage"]["blob"]["ref"]) == BLOB
    assert validate_record(V2, rec) == []
    assert versions.video_view(V2, rec, ALICE)["storageRef"]["value"] == BLOB


def _cid_bytes(cid):
    import libipld

    info = libipld.decode_cid(cid)
    return bytes([1, info["codec"], 0x12, 32]) + info["hash"]["digest"]


def test_view_round_trip_and_did():
    rec = pds_video()
    view = versions.video_view(V2, rec, ALICE)
    assert view["storageRef"] == {"kind": "pds", "value": BLOB, "mimeType": "video/mp4", "size": len(CLEAN),
                                  "did": ALICE}
    assert "did" not in versions.video_view(V2, rec)["storageRef"]
    assert versions.video_record_v2(view) == rec


def test_v1_records_cant_claim_pds_storage():
    from conftest import video_record

    v = versions.video_view("org.consentvid.video", video_record(storageRef={"kind": "pds", "value": BLOB}))
    assert v["storageRef"] == {"kind": "v1:pds", "value": ""}
    assert storage.public_url(v["storageRef"]) is None


def test_lexicon_change_is_compatible_and_mirrored():
    from consentvid.lexicons import compat_errors, load_lexicon

    published = json.loads((ROOT / "tests/fixtures/published_lexicons" / f"{V2}.json").read_text())
    local = load_lexicon(V2)
    assert compat_errors(published, local) == []
    assert "#pdsBlobRef" in local["defs"]["main"]["record"]["properties"]["storage"]["refs"]
    assert local["defs"]["pdsBlobRef"]["properties"]["blob"]["maxSize"] == 300_000_000
    assert (ROOT / "web/lexicons" / f"{V2}.json").read_bytes() == (
        ROOT / "src/consentvid/schemas" / f"{V2}.json"
    ).read_bytes()


# ---------------------------------------------------- urls, manifests ------


def test_public_url_needs_the_repo():
    ref = versions.video_view(V2, pds_video(), ALICE)["storageRef"]
    assert storage.public_url(ref) == f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert storage.public_url({k: v for k, v in ref.items() if k != "did"}) is None


CURI2 = f"at://{ALICE}/{C2}/{K1}"
VURI2 = f"at://{ALICE}/{V2}/{K1}"


def _item_and_fetch(video=None, view_did=None):
    video = video or pds_video()
    vcid = compute_cid(video)
    consent = consent_v2_record(video_cid=vcid)
    # The view as an AppView row would hold it (no DID): croissant adds the clip's.
    clip = Clip(CURI2, "bafyc2", versions.consent_view(C2, consent), VURI2, vcid,
                versions.video_view(V2, video, view_did), ALICE)
    m, skipped = build_manifest([clip], name="t")
    assert not skipped
    (item,) = rs.items_from_manifest(m)

    def fetch(at):
        uri = str(at)
        if uri == CURI2:
            return VerifiedRecord(uri, True, "bafyc2", consent, "r", True)
        if uri == VURI2:
            return VerifiedRecord(uri, True, vcid, video, "r", True)
        raise AssertionError(uri)

    return item, fetch


def test_croissant_entry():
    video = pds_video()
    clip = Clip(CURI2, "c", versions.consent_view(C2, consent_v2_record(video_cid=compute_cid(video))), VURI2,
                compute_cid(video), versions.video_view(V2, video), ALICE)
    fo = _file_object(clip)
    assert fo["cv:storageKind"] == "pds" and fo["encodingFormat"] == "video/mp4"
    assert fo["contentUrl"] == f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert fo["sha256"] == hashlib.sha256(CLEAN).hexdigest()


@pytest.mark.parametrize("view_did", [None, ALICE])
def test_resync_valid(view_did):
    item, fetch = _item_and_fetch(view_did=view_did)
    res = rs.check(item, fetch=fetch, relay_status=False)
    assert res.status == rs.VALID, res.reason


def test_resync_url_must_be_this_repos_blob():
    item, fetch = _item_and_fetch()
    item.content_url = f"https://moved-pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.VALID  # account migration
    for url in (
        f"{PDS}/xrpc/com.atproto.sync.getBlob?did={BOB}&cid={BLOB}",
        f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={OTHER_BLOB}",
        f"http://pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}",
    ):
        item.content_url = url
        assert rs.check(item, fetch=fetch, relay_status=False).status == rs.MODIFIED, url


def test_resync_deactivated_account_is_unavailable():
    from consentvid.errors import RepoUnavailable

    item, fetch = _item_and_fetch()

    def gone(at):
        raise RepoUnavailable(ALICE, "deactivated")

    assert rs.check(item, fetch=gone, relay_status=False).status == rs.UNAVAILABLE


# ------------------------------------------------------------------- cli ---


TERMS = ["--attest-authorship", "--attest-adult", "--people", "onlyMe", "--modality", "video",
         "--purpose", "training", "--model-class", "academic"]


def _uploader(fake_client, cid=BLOB, mime="video/mp4", size=len(CLEAN), error=None):
    sent = []

    def invoke_procedure(nsid, data=None, input_encoding=None, output_encoding=None, **kw):
        assert nsid == "com.atproto.repo.uploadBlob"
        sent.append((data, input_encoding, kw.get("timeout")))
        if error:
            raise error
        return SimpleNamespace(content={"blob": blob(cid, mime, size)})

    fake_client.invoke_procedure = invoke_procedure
    return sent


def _run_pds(fake_client, tmp_path, monkeypatch, name="clip.mp4", *extra):
    from test_cli import _fake_media, run

    _fake_media(monkeypatch)
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    f = tmp_path / name
    f.write_bytes(b"original bytes with GPS")
    return run("contribute", str(f), "--title", "t", "--storage-kind", "pds", *TERMS, *extra)


def test_contribute_to_pds_uploads_then_one_commit(fake_client, tmp_path, monkeypatch):
    sent = _uploader(fake_client)
    r = _run_pds(fake_client, tmp_path, monkeypatch)
    assert r.exit_code == 0, r.output
    assert sent == [(CLEAN, "video/mp4", _core._BLOB_UPLOAD_TIMEOUT)]  # the sanitised copy
    (call,) = [c for c in fake_client.repo.calls if c[0] in ("create", "put", "apply_writes")]
    video_w, consent_w = call[1]["writes"]
    video = video_w["value"]
    assert video["storage"] == pds_storage()
    assert video["contentHash"] == BLOB and video["size"] == len(CLEAN)
    assert validate_in_repo(V2, video, ALICE, rkey=video_w["rkey"]) == []
    assert validate_in_repo(C2, consent_w["value"], ALICE, rkey=consent_w["rkey"]) == []


def test_contribute_to_pds_keeps_the_pds_mime_spelling(fake_client, tmp_path, monkeypatch):
    _uploader(fake_client, mime="video/matroska")
    r = _run_pds(fake_client, tmp_path, monkeypatch, "clip.mkv")
    assert r.exit_code == 0, r.output
    (call,) = [c for c in fake_client.repo.calls if c[0] == "apply_writes"]
    video = call[1]["writes"][0]["value"]
    assert video["mimeType"] == "video/x-matroska" and video["storage"]["blob"]["mimeType"] == "video/matroska"


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"cid": OTHER_BLOB}, "different bytes"),
        ({"size": 99}, "different bytes"),
        ({"mime": "video/webm"}, "identified the file as"),
        ({"error": RuntimeError("BlobTooLarge: too large")}, "too large"),
    ],
)
def test_contribute_to_pds_refusals_write_nothing(fake_client, tmp_path, monkeypatch, kw, needle):
    _uploader(fake_client, **kw)
    r = _run_pds(fake_client, tmp_path, monkeypatch)
    assert r.exit_code != 0 and needle in r.output, r.output
    assert not fake_client.repo.records


def test_pds_storage_rules(fake_client, tmp_path, monkeypatch):
    sent = _uploader(fake_client)
    r = _run_pds(fake_client, tmp_path, monkeypatch, "clip.mp4", "--storage-value", "x")
    assert r.exit_code != 0 and "drop --storage-value" in r.output
    r = _run_pds(fake_client, tmp_path, monkeypatch, "clip.xyz")
    assert r.exit_code != 0 and "unknown video container" in r.output
    monkeypatch.setattr(_core, "PDS_BLOB_MAX_SIZE", 3)
    r = _run_pds(fake_client, tmp_path, monkeypatch)
    assert r.exit_code != 0 and "at most" in r.output
    assert sent == [] and not fake_client.repo.records


def test_revoke_pds_video_frees_the_blob(fake_client, tmp_path, monkeypatch):
    from test_cli import run

    _uploader(fake_client)
    assert _run_pds(fake_client, tmp_path, monkeypatch).exit_code == 0
    (rkey,) = {k for (c, k) in fake_client.repo.records}
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda *a: pytest.fail("no backend delete for pds"))
    r = run("revoke", f"at://{ALICE}/{V2}/{rkey}", "-y")
    assert r.exit_code == 0, r.output
    assert "PDS deletes the video blob" in r.output
    assert not fake_client.repo.records


def test_revoke_warns_when_another_record_keeps_the_blob(fake_client, tmp_path, monkeypatch):
    from test_cli import run

    _uploader(fake_client)
    assert _run_pds(fake_client, tmp_path, monkeypatch).exit_code == 0
    assert _run_pds(fake_client, tmp_path, monkeypatch, "clip.mp4", "--allow-duplicate").exit_code == 0
    rkeys = sorted({k for (c, k) in fake_client.repo.records})
    r = run("revoke", f"at://{ALICE}/{V2}/{rkeys[0]}", "-y")
    assert r.exit_code == 0, r.output
    assert "keeps the video blob" in r.output


def test_api_contribute_to_pds(fake_client, tmp_path, monkeypatch):
    from consentvid import Session, Terms
    from test_cli import _fake_media

    _fake_media(monkeypatch)
    f = tmp_path / "clip.mov"
    f.write_bytes(b"x")
    sent = _uploader(fake_client, mime="video/quicktime")
    terms = Terms(purposes=["training"], modalities=["video"], model_classes=["academic"], people="onlyMe",
                  adult=True, authorship=True)
    c = Session(fake_client).contribute(f, terms, title="t", storage="pds")
    assert sent and sent[0][1] == "video/quicktime"
    assert c.storage["kind"] == "pds" and c.storage["value"] == BLOB
