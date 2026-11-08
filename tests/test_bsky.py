"""Videos of the contributor's own Bluesky posts (videoV2 #bskyPostRef)."""

import hashlib
import json

import httpx
import pytest

from conftest import ALICE, BOB, K1, consent_v2_record, video_v2_record
from consentvid import bsky, cli, versions
from consentvid import resync as rs
from consentvid.croissant import Clip, _file_object, build_manifest
from consentvid.errors import ConsentVidError, RepoUnavailable, VerificationError
from consentvid.records import CONSENT_V2_NSID, VIDEO_V2_NSID, compute_cid, raw_cid_from_sha256
from consentvid.validate import validate_in_repo, validate_record
from consentvid.verify import VerifiedRecord

V2, C2 = VIDEO_V2_NSID, CONSENT_V2_NSID
BYTES = b"\x00\x00\x00\x18ftypmp42 a bluesky video"
BLOB = raw_cid_from_sha256("sha256-" + hashlib.sha256(BYTES).hexdigest())
OTHER_BLOB = raw_cid_from_sha256("sha256-" + "cd" * 32)
PDS = "https://pds.example"
POST_RKEY = "3mzrrcdrdgs2f"
POST_URI = f"at://{ALICE}/app.bsky.feed.post/{POST_RKEY}"


def post_record(blob=BLOB, media_wrapper=False, text="Making pasta\nwith friends", langs=("en",)):
    video = {
        "$type": "app.bsky.embed.video",
        "video": {"$type": "blob", "ref": {"$link": blob}, "mimeType": "video/mp4", "size": len(BYTES)},
        "aspectRatio": {"width": 1920, "height": 1080},
    }
    embed = (
        {"$type": "app.bsky.embed.recordWithMedia", "media": video,
         "record": {"$type": "app.bsky.embed.record", "record": {"uri": POST_URI, "cid": "bafyx"}}}
        if media_wrapper
        else video
    )
    return {"$type": "app.bsky.feed.post", "text": text, "langs": list(langs), "embed": embed,
            "createdAt": "2026-11-08T06:18:33.580Z"}


POST = post_record()
POST_CID = compute_cid(POST)


def storage(post_uri=POST_URI, post_cid=POST_CID, blob=BLOB):
    return {"$type": f"{V2}#bskyPostRef", "post": {"uri": post_uri, "cid": post_cid}, "blob": blob}


def bsky_video(**kw):
    return video_v2_record(contentHash=BLOB, storage=storage(), mimeType="video/mp4", **kw)


@pytest.fixture(autouse=True)
def _pds(monkeypatch):
    monkeypatch.setattr(bsky, "pds_of", lambda did: PDS)


# ------------------------------------------------------------ validation ---


def test_valid_bsky_video_record():
    assert validate_in_repo(V2, bsky_video(), ALICE, rkey=K1) == []


@pytest.mark.parametrize(
    "edit,needle",
    [
        ({"storage": storage(blob=OTHER_BLOB)}, "must equal contentHash"),
        ({"storage": storage(blob=POST_CID)}, "raw video bytes"),
        ({"storage": storage(post_uri=f"at://{ALICE}/app.bsky.feed.like/{POST_RKEY}")}, "app.bsky.feed.post"),
        ({"storage": storage(post_cid=BLOB)}, "post.cid must be the CID of a record"),
        ({"storage": {"$type": f"{V2}#bskyPostRef", "blob": BLOB}}, "missing required field: storage.post"),
        ({"storage": {"$type": f"{V2}#bskyPostRef", "post": {"uri": POST_URI}, "blob": BLOB}}, "storage.post.cid"),
    ],
)
def test_invalid_bsky_storage(edit, needle):
    errs = validate_record(V2, {**bsky_video(), **edit})
    assert any(needle in e for e in errs), errs


def test_only_your_own_posts():
    rec = {**bsky_video(), "storage": storage(post_uri=f"at://{BOB}/app.bsky.feed.post/{POST_RKEY}")}
    assert validate_record(V2, rec) == []  # record-local rules can't know the repo ...
    errs = validate_in_repo(V2, rec, ALICE, rkey=K1)  # ... the repo-aware ones can
    assert any("only your own Bluesky posts" in e for e in errs), errs


def test_view_round_trip():
    rec = bsky_video()
    view = versions.video_view(V2, rec)
    assert view["storageRef"] == {"kind": "bsky", "value": BLOB, "post": {"uri": POST_URI, "cid": POST_CID}}
    assert view["contentHash"] == "sha256-" + hashlib.sha256(BYTES).hexdigest()
    assert versions.video_record_v2(view) == rec


def test_v1_records_cant_claim_bluesky_storage():
    from consentvid.storage import public_url

    v1 = {"$type": "org.consentvid.video", "title": "t", "contentHash": "sha256-" + "ab" * 32,
          "storageRef": {"kind": "bsky", "value": BLOB, "post": {"uri": POST_URI, "cid": POST_CID}},
          "createdAt": "2026-01-01T00:00:00Z"}
    ref = versions.video_view("org.consentvid.video", v1)["storageRef"]
    assert ref["kind"] != "bsky" and public_url(ref) is None


def test_lexicon_change_is_compatible_and_mirrored():
    from pathlib import Path

    from consentvid.lexicons import compat_errors, load_lexicon

    root = Path(__file__).parent.parent
    published = json.loads((root / "tests/fixtures/published_lexicons/org.consentvid.videoV2.json").read_text())
    assert compat_errors(published, load_lexicon(V2)) == []
    assert "#bskyPostRef" in load_lexicon(V2)["defs"]["main"]["record"]["properties"]["storage"]["refs"]


# --------------------------------------------------------------- helpers ---


def test_video_blob_of():
    assert bsky.video_blob_of(POST) == bsky.VideoBlob(BLOB, "video/mp4", len(BYTES))
    assert bsky.video_blob_of(post_record(media_wrapper=True)).cid == BLOB
    image = {**POST, "embed": {"$type": "app.bsky.embed.images", "images": []}}
    assert bsky.video_blob_of(image) is None
    assert bsky.video_blob_of({"text": "no embed"}) is None
    assert bsky.video_blob_of(None) is None


def test_video_blob_of_a_verified_read():
    """fetch_verified decodes the CAR: the blob ref is a binary CID link."""
    import libipld

    decoded = libipld.decode_dag_cbor(libipld.encode_dag_cbor({
        "embed": {"$type": "app.bsky.embed.video",
                  "video": {"$type": "blob", "ref": _cid_bytes(BLOB),
                            "mimeType": "video/mp4", "size": 3}},
    }))
    assert isinstance(decoded["embed"]["video"]["ref"], bytes)
    assert bsky.video_blob_of(decoded).cid == BLOB


def _cid_bytes(cid):
    from consentvid.records import _cid_link_bytes

    return _cid_link_bytes(cid)


@pytest.mark.parametrize(
    "ref,expected",
    [
        (POST_URI, (ALICE, POST_RKEY)),
        (f"https://bsky.app/profile/{ALICE}/post/{POST_RKEY}", (ALICE, POST_RKEY)),
        (f"https://bsky.app/profile/Alice.Example.com/post/{POST_RKEY}", ("alice.example.com", POST_RKEY)),
    ],
)
def test_parse_post_ref(ref, expected):
    assert bsky.parse_post_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        f"at://{ALICE}/app.bsky.feed.like/{POST_RKEY}",
        f"http://bsky.app/profile/{ALICE}/post/{POST_RKEY}",
        f"https://evil.example/profile/{ALICE}/post/{POST_RKEY}",
        f"https://bsky.app/profile/{ALICE}/lists/{POST_RKEY}",
        "https://bsky.app/profile/not a handle/post/x",
    ],
)
def test_parse_post_ref_rejects(ref):
    with pytest.raises(ValueError):
        bsky.parse_post_ref(ref)


def test_public_url_and_url_matching():
    from consentvid.storage import delete_bytes, public_url

    ref = versions.video_view(V2, bsky_video())["storageRef"]
    url = public_url(ref)
    assert url == f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert bsky.url_matches(url, ref)
    # The PDS may change (migration) without the bytes changing.
    assert bsky.url_matches(f"https://other-pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}", ref)
    for bad in (
        f"http://pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}",
        f"https://pds.example/xrpc/com.atproto.sync.getBlob?did={BOB}&cid={BLOB}",
        f"https://pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={OTHER_BLOB}",
        f"https://pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}&x=1",
        f"https://127.0.0.1/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}",
        f"https://github.com/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}",
        f"https://pds.example:8443/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}",
    ):
        assert not bsky.url_matches(bad, ref), bad
    assert "delete the post" in delete_bytes(ref, "sha256-x")


def test_public_url_is_none_when_resolution_fails(monkeypatch):
    from consentvid.storage import public_url

    def boom(did):
        raise OSError("down")

    monkeypatch.setattr(bsky, "pds_of", boom)
    assert public_url(versions.video_view(V2, bsky_video())["storageRef"]) is None


def test_default_title():
    assert bsky.default_title(POST) == "Making pasta"
    assert bsky.default_title({"text": "\n  ‮  \n"}) == "Bluesky video"
    assert bsky.default_title({"text": "a  \t b​c"}) == "a bc"
    long = bsky.default_title({"text": "x" * 400})
    assert len(long) == 300
    emoji = bsky.default_title({"text": "👩‍👩‍👧‍👦" * 300})  # 25 bytes a grapheme: the byte cap binds
    assert len(emoji.encode()) <= 3000


# -------------------------------------------------------------- download ---


def _serve(monkeypatch, body=BYTES, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, content=body)

    monkeypatch.setattr(bsky, "_transport", httpx.MockTransport(handler))


def test_download_blob_checks_the_hash(tmp_path, monkeypatch):
    seen = []
    _serve(monkeypatch, seen=seen)
    path = bsky.download_blob(ALICE, BLOB, tmp_path)
    assert path.read_bytes() == BYTES
    assert seen == [f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"]
    _serve(monkeypatch, body=b"other bytes")
    with pytest.raises(ConsentVidError, match="don't match"):
        bsky.download_blob(ALICE, BLOB, tmp_path)
    _serve(monkeypatch, status=404)
    with pytest.raises(ConsentVidError, match="404"):
        bsky.download_blob(ALICE, BLOB, tmp_path)
    _serve(monkeypatch)
    with pytest.raises(ConsentVidError, match="larger than"):
        bsky.download_blob(ALICE, BLOB, tmp_path, max_bytes=4)
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # failures leave nothing behind


# ---------------------------------------------------------------- resync ---


CURI2 = f"at://{ALICE}/{C2}/{K1}"
VURI2 = f"at://{ALICE}/{V2}/{K1}"


def _item_and_fetch(post=POST, post_cid=None, raise_for_post=None, video=None):
    video = video or bsky_video()
    vcid = compute_cid(video)
    consent = consent_v2_record(video_cid=vcid)
    clip = Clip(CURI2, "bafyc2", versions.consent_view(C2, consent), VURI2, vcid, versions.video_view(V2, video), ALICE)
    m, skipped = build_manifest([clip], name="t")
    assert not skipped
    (item,) = rs.items_from_manifest(m)

    def fetch(at):
        uri = str(at)
        if uri == CURI2:
            return VerifiedRecord(uri, True, "bafyc2", consent, "r", True)
        if uri == VURI2:
            return VerifiedRecord(uri, True, vcid, video, "r", True)
        if uri == POST_URI:
            if raise_for_post:
                raise raise_for_post
            return VerifiedRecord(uri, post is not None, post_cid or POST_CID if post else None, post, "r", True)
        raise AssertionError(uri)

    return item, fetch


def test_manifest_points_at_the_pds():
    item, _ = _item_and_fetch()
    assert item.content_url == f"{PDS}/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert item.sha256 == hashlib.sha256(BYTES).hexdigest()


@pytest.mark.parametrize(
    "kw,status,needle",
    [
        ({}, rs.VALID, ""),
        ({"post": None}, rs.UNAVAILABLE, "deleted"),
        ({"post_cid": "bafyreiaaaa"}, rs.MODIFIED, "rewritten"),
        ({"post": post_record(blob=OTHER_BLOB)}, rs.INVALID, "doesn't embed"),
        ({"post": {"text": "video removed"}}, rs.INVALID, "doesn't embed"),
        ({"raise_for_post": OSError("down")}, rs.UNKNOWN, ""),
        ({"raise_for_post": VerificationError("bad sig")}, rs.INVALID, "post proof"),
        ({"raise_for_post": RepoUnavailable(ALICE, "takendown")}, rs.UNAVAILABLE, "takendown"),
    ],
)
def test_resync_checks_the_post(kw, status, needle):
    item, fetch = _item_and_fetch(**kw)
    res = rs.check(item, fetch=fetch, relay_status=False)
    assert res.status == status and needle in res.reason, res.reason


def test_resync_post_inside_record_with_media():
    item, fetch = _item_and_fetch(post=post_record(media_wrapper=True), post_cid=POST_CID)
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.VALID


def test_resync_unresolvable_pds_is_unknown(monkeypatch):
    item, fetch = _item_and_fetch()

    def boom(did):
        raise OSError("plc down")

    monkeypatch.setattr(bsky, "pds_of", boom)
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.UNKNOWN


def test_resync_accepts_a_migrated_pds_and_rejects_another_blob():
    item, fetch = _item_and_fetch()
    item.content_url = f"https://new-pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={BLOB}"
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.VALID
    item.content_url = f"https://pds.example/xrpc/com.atproto.sync.getBlob?did={ALICE}&cid={OTHER_BLOB}"
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.MODIFIED


def test_croissant_entry():
    video = bsky_video()
    clip = Clip(CURI2, "c", versions.consent_view(C2, consent_v2_record(video_cid=compute_cid(video))), VURI2,
                compute_cid(video), versions.video_view(V2, video), ALICE)
    fo = _file_object(clip)
    assert fo["cv:storageKind"] == "bsky" and fo["encodingFormat"] == "video/mp4"
    assert fo["contentUrl"].startswith(f"{PDS}/xrpc/com.atproto.sync.getBlob?")


# ------------------------------------------------------------------- cli ---


TERMS = ["--attest-authorship", "--attest-adult", "--people", "onlyMe", "--modality", "video",
         "--purpose", "training", "--model-class", "academic"]


def _setup_cli(fake_client, monkeypatch, post=POST, probe=None, post_did=ALICE):
    from consentvid import verify
    from consentvid.media import Probe
    from test_cli import run

    monkeypatch.setattr(cli, "get_client", lambda: fake_client)

    def fetch(at):
        assert str(at) == f"at://{post_did}/app.bsky.feed.post/{POST_RKEY}"
        return VerifiedRecord(str(at), post is not None, POST_CID if post else None, post, "r", True)

    monkeypatch.setattr(verify, "fetch_verified", fetch)
    _serve(monkeypatch)
    monkeypatch.setattr(
        "consentvid.media.probe", lambda p: probe if probe is not None else Probe(width=1920, height=1080, duration_ms=5000)
    )
    return run


def test_contribute_from_post_writes_one_commit(fake_client, monkeypatch):
    run = _setup_cli(fake_client, monkeypatch)
    r = run("contribute", "--from-post", f"https://bsky.app/profile/{ALICE}/post/{POST_RKEY}", *TERMS)
    assert r.exit_code == 0, r.output
    (call,) = [c for c in fake_client.repo.calls if c[0] in ("create", "put", "apply_writes")]
    video_w, consent_w = call[1]["writes"]
    video = video_w["value"]
    assert video["storage"] == storage()
    assert video["contentHash"] == BLOB and video["title"] == "Making pasta" and video["langs"] == ["en"]
    assert video["size"] == len(BYTES) and video["width"] == 1920 and video["mimeType"] == "video/mp4"
    assert consent_w["rkey"] == video_w["rkey"]
    assert consent_w["value"]["subject"]["cid"] == compute_cid(video)
    assert validate_in_repo(V2, video, ALICE, rkey=video_w["rkey"]) == []
    assert validate_in_repo(C2, consent_w["value"], ALICE, rkey=consent_w["rkey"]) == []


def test_contribute_from_post_title_override(fake_client, monkeypatch):
    run = _setup_cli(fake_client, monkeypatch)
    r = run("contribute", "--from-post", POST_URI, "--title", "Pasta", "--language", "cs", *TERMS)
    assert r.exit_code == 0, r.output
    ((_, video),) = [v for k, v in fake_client.repo.records.items() if k[0] == V2]
    assert video["title"] == "Pasta" and video["langs"] == ["cs"]


def test_contribute_from_someone_elses_post_is_refused(fake_client, monkeypatch):
    run = _setup_cli(fake_client, monkeypatch, post_did=BOB)
    r = run("contribute", "--from-post", f"at://{BOB}/app.bsky.feed.post/{POST_RKEY}", *TERMS)
    assert r.exit_code != 0 and "only contribute your own" in r.output and not fake_client.repo.records


@pytest.mark.parametrize("post,needle", [(None, "doesn't exist"), ({"text": "hi"}, "has no video")])
def test_contribute_from_post_without_video(fake_client, monkeypatch, post, needle):
    run = _setup_cli(fake_client, monkeypatch, post=post)
    r = run("contribute", "--from-post", POST_URI, *TERMS)
    assert r.exit_code != 0 and needle in r.output and not fake_client.repo.records


def test_contribute_from_post_refuses_identifying_metadata(fake_client, monkeypatch):
    from consentvid.media import Probe

    run = _setup_cli(fake_client, monkeypatch, probe=Probe(sensitive_tags={"location": "+50.08+014.42/"}))
    r = run("contribute", "--from-post", POST_URI, *TERMS)
    assert r.exit_code != 0 and "identifying metadata" in r.output and not fake_client.repo.records
    r = run("contribute", "--from-post", POST_URI, "--keep-metadata", *TERMS)
    assert r.exit_code == 0, r.output


def test_contribute_from_post_refuses_bad_bytes(fake_client, monkeypatch):
    run = _setup_cli(fake_client, monkeypatch)
    _serve(monkeypatch, body=b"tampered")
    r = run("contribute", "--from-post", POST_URI, *TERMS)
    assert r.exit_code != 0 and "don't match" in r.output and not fake_client.repo.records


def test_contribute_from_post_duplicate_and_gates(fake_client, monkeypatch):
    from consentvid import signals

    run = _setup_cli(fake_client, monkeypatch)
    assert run("contribute", "--from-post", POST_URI, *TERMS).exit_code == 0
    r = run("contribute", "--from-post", POST_URI, *TERMS)
    assert r.exit_code != 0 and "already registered" in r.output
    fake_client.repo.records.clear()
    signals.clear_cache()
    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(bot=True))
    r = run("contribute", "--from-post", POST_URI, *TERMS)
    assert r.exit_code != 0 and "bots cannot give consent" in r.output and not fake_client.repo.records


@pytest.mark.parametrize(
    "args,needle",
    [
        ([], "either a VIDEO_PATH or --from-post"),
        (["--from-post", POST_URI, __file__], "either a VIDEO_PATH or --from-post"),
        ([__file__], "--title is required"),
        (["--from-post", POST_URI, "--storage-kind", "r2"], "don't apply to --from-post"),
    ],
)
def test_contribute_argument_rules(args, needle):
    from test_cli import run

    r = run("contribute", *args, *TERMS)
    assert r.exit_code != 0 and needle in r.output, r.output


def test_export_leaves_out_deleted_posts(fake_client, tmp_path, monkeypatch):
    from consentvid import identity, verify
    from test_cli import run

    video = bsky_video()
    fake_client.repo.records[(V2, K1)] = (compute_cid(video), video)
    fake_client.repo.records[(C2, K1)] = ("bafyc", consent_v2_record(video_cid=compute_cid(video)))
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: fake_client)
    monkeypatch.setattr(identity, "display_name", lambda did: "alice.test")
    state = {"post": POST}
    monkeypatch.setattr(
        verify, "fetch_verified",
        lambda at: VerifiedRecord(str(at), state["post"] is not None, POST_CID, state["post"], "r", True),
    )
    out = tmp_path / "m.json"
    r = run("export", str(out), "--repo", "alice.test")
    assert r.exit_code == 0, r.output
    (fo,) = json.loads(out.read_text())["distribution"]
    assert fo["cv:storageKind"] == "bsky"
    state["post"] = None
    r = run("export", str(out), "--repo", "alice.test")
    assert json.loads(out.read_text())["distribution"] == [] and "deleted" in r.output


def test_verify_reports_a_deleted_post(monkeypatch):
    from consentvid import verify
    from test_cli import run

    video = bsky_video()

    def fetch(at):
        if str(at) == VURI2:
            return VerifiedRecord(VURI2, True, compute_cid(video), video, "r", True)
        return VerifiedRecord(str(at), False, None, None, "r", True)

    monkeypatch.setattr(verify, "fetch_verified", fetch)
    r = run("verify", VURI2, "--no-relay-status")
    assert r.exit_code == 1 and "BYTES" in r.output and "deleted" in r.output
