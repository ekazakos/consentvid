"""The public Python API (consentvid.api): Terms, Session writes and reads,
check(), and the bulk runner (resume, pacing, rate limits)."""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from conftest import ALICE, HASH, K1, VIDEO_CID, consent_record, video_record
from consentvid import _core, api, records, signals
from consentvid.api import BulkItem, Session, Terms
from consentvid.errors import AlreadyRegistered, Cancelled, ConsentVidError, RateLimited, UsageError

V2, C2 = records.VIDEO_V2_NSID, records.CONSENT_V2_NSID


def terms(**kw):
    base = dict(
        purposes=["training"],
        modalities=["video", "audio"],
        model_classes=["academic"],
        people="onlyMe",
        adult=True,
        authorship=True,
    )
    base.update(kw)
    return Terms(**base)


@pytest.fixture
def media(monkeypatch):
    """Sanitising is idempotent and keeps files distinct (so different
    inputs get different content hashes); uploads are content-addressed."""
    from consentvid.media import Probe
    from consentvid.storage import Upload

    def sanitise(src, dst):
        data = src.read_bytes()
        dst.write_bytes(data if data.startswith(b"clean:") else b"clean:" + data)
        return Probe(content_hash=records.hash_file(dst))

    uploads = []

    def upload(kind, path, *, content_hash, title=None):
        uploads.append((kind, path.read_bytes()))
        return Upload(f"https://r2.example/videos/{content_hash}", True)

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe())
    monkeypatch.setattr("consentvid.media.sanitise", sanitise)
    monkeypatch.setattr("consentvid.storage.upload", upload)
    return uploads


@pytest.fixture
def session(fake_client):
    return Session(fake_client)


def clip(tmp_path, name="a.mp4", data=b"video a"):
    f = tmp_path / name
    f.write_bytes(data)
    return f


# ------------------------------------------------------------------ terms ---


def test_terms_require_both_attestations():
    with pytest.raises(UsageError, match="authorship=True is required"):
        terms(authorship=False)
    with pytest.raises(UsageError, match="adult=True is required"):
        terms(adult=False)
    # Truthy is not enough: the affirmation must be the literal True.
    with pytest.raises(UsageError, match="authorship"):
        terms(authorship="yes")


def test_terms_have_no_defaults_for_what_is_granted():
    with pytest.raises(TypeError):
        Terms(purposes=["training"], modalities=["video"], model_classes=["academic"], adult=True, authorship=True)
    with pytest.raises(UsageError, match="nothing is granted by default"):
        terms(purposes=[])
    with pytest.raises(TypeError):
        Terms(["training"], ["video"], ["academic"], "onlyMe", True, True)  # keyword-only


def test_terms_validate_and_normalise_values():
    t = terms(model_classes=["open-weights", "academic", "academic"], purposes="training")
    assert t.model_classes == ("openWeights", "academic") and t.purposes == ("training",)
    with pytest.raises(UsageError, match="'mining' is not one of"):
        terms(purposes=["mining"])
    with pytest.raises(UsageError, match="researchOnly"):
        terms(purposes=["researchOnly"])  # deprecated: not offered for new consents
    with pytest.raises(UsageError, match="people"):
        terms(people="everyone")
    with pytest.raises(UsageError, match="contradict"):
        terms(people="noIdentifiablePeople", modalities=["face"])
    with pytest.raises(UsageError, match="reserved"):
        terms(capture_provenance="attested")


def test_terms_expiry_forms():
    assert terms().expires_at is None
    in_90d = terms(expires="90d").expires_at
    assert in_90d.endswith("Z") and in_90d > datetime.now(UTC).isoformat()
    assert terms(expires=timedelta(days=1)).expires_at.endswith("Z")
    assert terms(expires=date(2099, 1, 1)).expires_at.startswith("2099-01-01T00:00:00")
    assert terms(expires=datetime(2099, 1, 1, tzinfo=UTC)).expires_at.startswith("2099-01-01")
    for bad in (datetime(2099, 1, 1), "2000-01-01", timedelta(days=-1), "soon", 5):
        with pytest.raises(UsageError):
            terms(expires=bad)


# ----------------------------------------------------------------- writes ---


def test_contribute_writes_video_and_sidecar_consent_in_one_commit(session, fake_client, tmp_path, media):
    c = session.contribute(clip(tmp_path), terms(), title="Pasta", tags=["cooking"], storage="r2")
    assert isinstance(c, api.Contribution)
    rkey = c.video_uri.rsplit("/", 1)[1]
    assert c.video_uri == f"at://{ALICE}/{V2}/{rkey}" and c.consent_uri == f"at://{ALICE}/{C2}/{rkey}"
    writes = [call for call in fake_client.repo.calls if call[0] in ("apply_writes", "create", "put")]
    assert len(writes) == 1 and writes[0][0] == "apply_writes"
    stored_video = fake_client.repo.records[(V2, rkey)]
    stored_consent = fake_client.repo.records[(C2, rkey)]
    assert stored_video[0] == c.video_cid and stored_consent[1]["subject"]["cid"] == c.video_cid
    assert c.content_hash == "sha256-" + __import__("hashlib").sha256(b"clean:video a").hexdigest()
    assert c.storage["value"].endswith(c.content_hash) and media == [("r2", b"clean:video a")]
    assert c.consent["purposes"] == ["training"] and c.consent["peoplePresent"] == "onlyMe"


def test_contribute_refuses_bad_arguments_before_anything_is_written(session, fake_client, tmp_path, media):
    f = clip(tmp_path)
    with pytest.raises(UsageError, match="consentvid.Terms"):
        session.contribute(f, {"purposes": ["training"]}, title="t")
    with pytest.raises(UsageError, match="title is required"):
        session.contribute(f, terms(), title="")
    with pytest.raises(UsageError, match="storage"):
        session.contribute(f, terms(), title="t", storage="dropbox")
    with pytest.raises(UsageError, match="not a file"):
        session.contribute(tmp_path / "missing.mp4", terms(), title="t")
    with pytest.raises(UsageError, match="self_labels"):
        session.contribute(f, terms(), title="t", self_labels=["spicy"])
    assert not fake_client.repo.calls and not media


def test_identical_bytes_are_already_registered(session, tmp_path, media):
    first = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    with pytest.raises(AlreadyRegistered) as e:
        session.contribute(clip(tmp_path, "copy.mp4"), terms(), title="t", storage="r2")
    assert e.value.uri == first.video_uri


def test_contribute_refuses_a_bot_account(session, fake_client, tmp_path, media, monkeypatch):
    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(bot=True, detail="profile"))
    signals.clear_cache()
    with pytest.raises(ConsentVidError, match="bot"):
        session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    assert not fake_client.repo.records and not media  # refused before the upload


def test_contribute_refuses_training_when_the_ai_preference_denies_it(session, fake_client, tmp_path, media,
                                                                     monkeypatch):
    monkeypatch.setattr(
        signals, "_lookup", lambda did: signals.AccountSignals(training_denied=True, detail="self record")
    )
    signals.clear_cache()
    with pytest.raises(ConsentVidError, match="not writing this consent"):
        session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    assert not fake_client.repo.records


def test_register_then_attach_then_replace(session, fake_client, tmp_path, media):
    video = session.register_video(clip(tmp_path), title="t", storage="r2")
    assert isinstance(video, api.Video) and video.uri.startswith(f"at://{ALICE}/{V2}/")
    rkey = video.uri.rsplit("/", 1)[1]
    value = fake_client.repo.records[(V2, rkey)][1]
    fake_client.repo.records[(V2, rkey)] = (records.compute_cid(value), value)  # the fake createRecord's CID isn't real
    consent = session.attach_consent(video.uri, terms())
    assert consent.uri == f"at://{ALICE}/{C2}/{rkey}" and not consent.replaced
    with pytest.raises(ConsentVidError, match="already has consent"):
        session.attach_consent(video.uri, terms())
    replaced = session.attach_consent(video.uri, terms(purposes=["evaluation"]), replace=True)
    assert replaced.replaced and fake_client.repo.records[(C2, rkey)][1]["purposes"] == ["evaluation"]


def test_contribute_from_post_goes_through_the_core(session, monkeypatch):
    seen = {}

    def register_post(client, post, **kw):
        seen.update(kw, post=post)
        rec = {"contentHash": "sha256-" + "cd" * 32, "storageRef": {"kind": "bsky"}}
        return "at://v", "bafyv", rec, ("at://c", "bafyc", {"purposes": ["training"]})

    monkeypatch.setattr(_core, "_register_post", register_post)
    c = session.contribute_from_post("https://bsky.app/profile/alice.test/post/3abc", terms(), tags=("x",))
    assert c.video_uri == "at://v" and c.consent_uri == "at://c" and c.storage == {"kind": "bsky"}
    assert seen["post"].endswith("/post/3abc") and seen["tags"] == ("x",) and seen["title"] is None
    built = seen["consent_for"]("at://did:plc:x/org.consentvid.videoV2/3k", "bafyvideo")
    assert built["videoCid"] == "bafyvideo" and built["adultAttested"] is True


# ----------------------------------------------------------------- revoke ---


def test_revoke_a_video_asks_confirm_with_the_plan(session, fake_client, tmp_path, media):
    c = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    plans = []
    out = session.revoke(c.video_uri, confirm=lambda plan: plans.append(plan) or True)
    assert [(p.kind, p.consents) for p in plans] == [("video", [c.consent_uri])]
    assert out.withdrawn == (c.consent_uri,) and out.video_deleted == c.video_uri
    assert not fake_client.repo.records
    assert "publicly reachable" in out.bytes_note and not out.bytes_deleted


def test_a_declined_confirmation_deletes_nothing(session, fake_client, tmp_path, media):
    c = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    with pytest.raises(Cancelled):
        session.revoke(c.video_uri, confirm=lambda plan: False)
    with pytest.raises(Cancelled):
        session.revoke(c.consent_uri, confirm=lambda plan: False)
    assert len(fake_client.repo.records) == 2


def test_revoke_only_the_consent(session, fake_client, tmp_path, media):
    c = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    out = session.revoke(c.consent_uri)
    assert out.withdrawn == (c.consent_uri,) and out.video_deleted is None
    assert list(fake_client.repo.records) == [(V2, c.video_uri.rsplit("/", 1)[1])]
    with pytest.raises(UsageError, match="delete-bytes"):
        session.revoke(c.consent_uri, delete_bytes=True)


def test_revoke_with_delete_bytes(session, tmp_path, media, monkeypatch):
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append(h) or "deleted from R2")
    c = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    out = session.revoke(c.video_uri, delete_bytes=True)
    assert out.bytes_deleted and out.bytes_note == "deleted from R2" and deleted == [c.content_hash]


# ------------------------------------------------------------------- list ---


def test_list_reports_consent_status(session, fake_client, tmp_path, media):
    c = session.contribute(clip(tmp_path), terms(), title="with consent", storage="r2")
    bare = session.register_video(clip(tmp_path, "b.mp4", b"video b"), title="bare", storage="r2")
    fake_client.repo.records[("org.consentvid.video", K1)] = (VIDEO_CID, video_record())
    fake_client.repo.records[("org.consentvid.consent", K1)] = (
        "bafyold",
        consent_record(expiresAt="2001-01-01T00:00:00.000Z"),
    )
    by_uri = {e.uri: e for e in session.list()}
    assert by_uri[c.video_uri].status == "consented" and by_uri[c.video_uri].version == 2
    assert by_uri[c.video_uri].consent.uri == c.consent_uri and by_uri[c.video_uri].consent.keyed
    assert by_uri[bare.uri].status == "no-consent" and by_uri[bare.uri].consent is None
    v1 = by_uri[f"at://{ALICE}/org.consentvid.video/{K1}"]
    assert v1.version == 1 and v1.status == "expired" and v1.content_hash == HASH


# ------------------------------------------------------------------ check ---


def _fetcher(consent, video):
    from consentvid.verify import VerifiedRecord

    curi = f"at://{ALICE}/org.consentvid.consent/{K1}"
    vuri = f"at://{ALICE}/org.consentvid.video/{K1}"

    def fetch(at):
        uri = str(at)
        if uri == curi:
            return VerifiedRecord(uri, consent is not None, "bafyconsent" if consent else None, consent, "r", True)
        if uri == vuri:
            return VerifiedRecord(uri, video is not None, VIDEO_CID if video else None, video, "r", True)
        raise AssertionError(uri)

    return fetch


def test_check_gives_resync_verdicts(monkeypatch):
    from consentvid import resync

    vuri = f"at://{ALICE}/org.consentvid.video/{K1}"
    monkeypatch.setattr(resync, "fetch_verified", _fetcher(consent_record(), video_record()))
    r = api.check(vuri)
    assert r.ok and r.status == "valid" and r.verified, r.reason
    assert r.consent_uri == f"at://{ALICE}/org.consentvid.consent/{K1}" and r.video_uri == vuri

    monkeypatch.setattr(resync, "fetch_verified", _fetcher(None, video_record()))
    r = api.check(vuri)
    assert r.status == "revoked" and not r.ok

    expired = consent_record(expiresAt="2001-01-01T00:00:00.000Z")
    monkeypatch.setattr(resync, "fetch_verified", _fetcher(expired, video_record()))
    assert api.check(vuri).status == "expired"

    local = video_record(storageRef={"kind": "local", "value": HASH + ".mp4"})
    monkeypatch.setattr(resync, "fetch_verified", _fetcher(consent_record(), local))
    r = api.check(vuri)
    assert r.status == "invalid" and "publicly reachable" in r.reason


def test_check_refuses_other_collections():
    with pytest.raises(UsageError, match="not a ConsentVid record"):
        api.check(f"at://{ALICE}/app.bsky.feed.post/{K1}")


# ------------------------------------------------------------------- bulk ---


def test_bulk_contributes_logs_and_resumes(session, fake_client, tmp_path, media):
    files = [clip(tmp_path, f"{i}.mp4", f"video {i}".encode()) for i in range(3)]
    log = tmp_path / "run.jsonl"
    seen = []
    results = session.contribute_many(
        files, terms=terms(), title_for=lambda it: "clip", resume=log, storage="r2", progress=seen.append
    )
    assert [r.status for r in results] == ["contributed"] * 3 and seen == results
    assert len([k for k in fake_client.repo.records if k[0] == V2]) == 3
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["status"] for e in entries] == ["contributed"] * 3 and all(e["consent_uri"] for e in entries)

    # Re-run: everything is skipped, nothing is written.
    calls = len(fake_client.repo.calls)
    again = session.contribute_many(files, terms=terms(), title_for=lambda it: "clip", resume=log, storage="r2")
    assert [r.status for r in again] == ["skipped"] * 3 and all(r.ok for r in again)
    assert not [c for c in fake_client.repo.calls[calls:] if c[0] == "apply_writes"]

    # An edited file is contributed again.
    files[1].write_bytes(b"video 1, re-cut")
    third = session.contribute_many(files, terms=terms(), title_for=lambda it: "clip", resume=log, storage="r2")
    assert [r.status for r in third] == ["skipped", "contributed", "skipped"]


def test_bulk_reports_already_registered_bytes_as_skipped(session, tmp_path, media):
    first = session.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    (r,) = session.contribute_many([clip(tmp_path, "dup.mp4")], terms=terms(), title_for=lambda it: "t",
                                   storage="r2")
    assert r.status == "skipped" and r.video_uri == first.video_uri and r.error_type == "AlreadyRegistered"


def test_bulk_per_item_terms_titles_and_errors(session, fake_client, tmp_path, media):
    items = [
        BulkItem(path=clip(tmp_path, "a.mp4", b"a"), title="A", terms=terms(purposes=["evaluation"])),
        {"path": str(clip(tmp_path, "b.mp4", b"b")), "title": "B", "tags": ["x"]},
        BulkItem(path=clip(tmp_path, "c.mp4", b"c")),  # no title
        BulkItem(path=tmp_path / "gone.mp4", title="gone"),
    ]
    results = session.contribute_many(items, terms=terms(), storage="r2")
    assert [r.status for r in results] == ["contributed", "contributed", "failed", "failed"]
    assert results[0].contribution.consent["purposes"] == ["evaluation"]
    assert results[1].contribution.video["tags"] == ["x"]
    assert "no title" in results[2].error and "can't read" in results[3].error

    stopped = session.contribute_many(
        [BulkItem(path=clip(tmp_path, "d.mp4", b"d")), BulkItem(path=clip(tmp_path, "e.mp4", b"e"), title="E")],
        terms=terms(), storage="r2", on_error="stop",
    )
    assert [r.status for r in stopped] == ["failed"]


def test_bulk_item_shapes_are_checked(session):
    with pytest.raises(UsageError, match="exactly one of path or post"):
        session.contribute_many([BulkItem(title="t")], terms=terms())
    with pytest.raises(UsageError, match="unknown BulkItem field"):
        session.contribute_many([{"path": "x", "colour": "red"}], terms=terms())
    with pytest.raises(UsageError, match="on_error"):
        session.contribute_many([], terms=terms(), on_error="ignore")


def test_bulk_posts_use_contribute_from_post(session, monkeypatch):
    posts = []

    def register_post(client, post, **kw):
        posts.append((post, kw["title"]))
        rec = {"contentHash": "sha256-" + "ef" * 32, "storageRef": {"kind": "bsky"}}
        return f"at://v/{len(posts)}", "bafyv", rec, ("at://c", "bafyc", {})

    monkeypatch.setattr(_core, "_register_post", register_post)
    results = session.contribute_many([BulkItem(post="at://p/1"), {"post": "at://p/2", "title": "two"}],
                                      terms=terms())
    assert [r.status for r in results] == ["contributed", "contributed"]
    assert posts == [("at://p/1", None), ("at://p/2", "two")]


def test_bulk_waits_out_a_short_rate_limit_and_stops_on_a_long_one(session, tmp_path, monkeypatch):
    import time

    attempts, sleeps = [], []

    def flaky(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimited("slow down", reset=time.time() + 30)
        return ("at://v", "bafyv", {"contentHash": HASH, "storageRef": {}}, ("at://c", "bafyc", {}))

    monkeypatch.setattr(_core, "_register_impl", flaky)
    (r,) = session.contribute_many([clip(tmp_path)], terms=terms(), title_for=lambda it: "t", _sleep=sleeps.append)
    assert r.status == "contributed" and len(attempts) == 2 and 25 < sleeps[0] <= 31

    def blocked(*a, **k):
        raise RateLimited("come back tomorrow", reset=time.time() + 86400)

    monkeypatch.setattr(_core, "_register_impl", blocked)
    files = [clip(tmp_path, "x.mp4", b"x"), clip(tmp_path, "y.mp4", b"y")]
    results = session.contribute_many(files, terms=terms(), title_for=lambda it: "t", _sleep=sleeps.append)
    assert [r.status for r in results] == ["failed"] and results[0].error_type == "RateLimited"


def test_bulk_retries_transient_errors_only(session, tmp_path, monkeypatch):
    import httpx

    calls, sleeps = [], []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("reset by peer")
        return ("at://v", "bafyv", {"contentHash": HASH, "storageRef": {}}, ("at://c", "bafyc", {}))

    monkeypatch.setattr(_core, "_register_impl", flaky)
    (r,) = session.contribute_many([clip(tmp_path)], terms=terms(), title_for=lambda it: "t", _sleep=sleeps.append)
    assert r.status == "contributed" and len(calls) == 3 and sleeps == [1.0, 2.0]

    def broken(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(_core, "_register_impl", broken)
    (r,) = session.contribute_many([clip(tmp_path, "b.mp4", b"b")], terms=terms(), title_for=lambda it: "t",
                                   _sleep=sleeps.append)
    assert r.status == "failed" and r.error_type == "KeyError" and sleeps == [1.0, 2.0]


def test_write_budget_paces_under_the_hourly_limit():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    budget = api._WriteBudget(per_hour=12, per_day=None, sleep=sleep, clock=lambda: now[0])
    for _ in range(2):
        budget.take(6)
    assert slept == []
    budget.take(6)  # the window is full until the first write is an hour old
    assert slept == [3600.0]
    with pytest.raises(UsageError):
        budget.take(13)
    api._WriteBudget(per_hour=None, per_day=None).take(10**6)  # disabled


def test_a_truncated_last_log_line_is_ignored(tmp_path):
    log = tmp_path / "run.jsonl"
    log.write_text(json.dumps({"key": "k", "status": "contributed", "fingerprint": "f"}) + "\n{\"key\": \"k2\", \"st")
    assert set(api._read_resume_log(log)) == {"k"}
    log.write_text("garbage\n" + json.dumps({"key": "k", "status": "contributed"}) + "\n")
    with pytest.raises(UsageError, match="not a JSONL resume log"):
        api._read_resume_log(log)


# ------------------------------------------------------- session plumbing ---


def test_messages_go_to_on_message(fake_client, tmp_path, media):
    got = []
    s = Session(fake_client, on_message=lambda level, msg: got.append((level, msg)))
    s.contribute(clip(tmp_path), terms(), title="t", storage="r2")
    assert ("info", "Uploading to r2 ...") in got
    assert _core._reporter.get() is None  # restored afterwards


def test_login_uses_the_cached_session_or_explicit_credentials(monkeypatch, fake_client):
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    s = Session.login()
    assert s.did == ALICE and s.handle == "alice.test" and "alice.test" in repr(s)

    ended = []
    monkeypatch.setattr(_core, "_end_cached_session", lambda **k: ended.append(1) or False)
    monkeypatch.setattr(_core, "_session_client", lambda h, p: SimpleNamespace(me=SimpleNamespace(did=h, handle=h)))
    s = Session.login("did:plc:other", "app-pass")
    assert s.did == "did:plc:other" and ended == [1]
    with pytest.raises(UsageError, match="both handle and password"):
        Session.login("alice.test")


def test_package_exports_the_api_lazily():
    import subprocess
    import sys

    code = (
        "import sys, consentvid\n"
        "assert 'consentvid.api' not in sys.modules\n"
        "from consentvid import Session, Terms, check\n"
        "assert 'consentvid.api' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    import consentvid

    with pytest.raises(AttributeError):
        consentvid.nope  # noqa: B018
    assert "Session" in dir(consentvid)
