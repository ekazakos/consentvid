import json
import shutil
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from conftest import ALICE, BOB, K0, K1, K2, VIDEO_CID, video_record, video_v2_record
from consentvid import _core, cli
from consentvid.errors import ConsentVidError, UsageError

ROOT = Path(__file__).resolve().parents[1]
TERMS = dict(
    modalities=("video", "audio"),
    purposes=("training",),
    model_classes=("academic",),
    people="onlyMe",
    adult=True,
    expires_at=None,
    compensation="none",
    attest=True,
    rights_basis="ownRecording",
    capture_provenance="selfAttested",
)


def run(*args, input=None):  # noqa: A002 — CliRunner's name
    return CliRunner().invoke(cli.main, list(args), input=input)


def test_every_command_has_help():
    for name, _cmd in cli.main.commands.items():
        assert run(name, "--help").exit_code == 0, name
    for name in cli.appview.commands:
        assert run("appview", name, "--help").exit_code == 0, name


def test_consent_without_attestation_refused_before_login():
    for args in (
        ["attach-consent", f"at://{ALICE}/org.consentvid.video/x"],
        ["contribute", __file__, "--title", "t"],
    ):
        r = run(*args)
        assert r.exit_code != 0 and "attest-authorship" in r.output


def test_capture_provenance_attested_is_refused():
    """R3-05: 'attested' can't be verified, so the CLI won't write or filter on it."""
    terms = ["--modality", "video", "--purpose", "training", "--model-class", "academic", "--people", "onlyMe",
             "--attest-authorship", "--attest-adult", "--capture-provenance", "attested"]
    for args in (
        ["attach-consent", f"at://{ALICE}/org.consentvid.video/x", *terms],
        ["contribute", __file__, "--title", "t", *terms],
        ["appview", "query", "--capture-provenance", "attested"],
        ["appview", "export", "/nonexistent/x.json", "--capture-provenance", "attested"],
    ):
        r = run(*args)
        assert r.exit_code == 2 and "cannot verify" in r.output, (args, r.output)


def test_appview_refuses_attested_filter(tmp_path):
    from consentvid import appview as av

    with pytest.raises(ValueError, match="attested"):
        av.query_consents(av.connect(tmp_path / "a.db"), provenance=("attested",))


def test_consent_terms_required_before_login():
    """Nothing is granted by default: missing terms are refused (non-interactive)."""
    attested = ["--attest-authorship", "--attest-adult", "--people", "onlyMe"]
    for args in (
        ["attach-consent", f"at://{ALICE}/org.consentvid.video/x", *attested],
        ["contribute", __file__, "--title", "t", *attested],
    ):
        r = run(*args)
        assert r.exit_code == 2 and "--modality" in r.output and "--purpose" in r.output, r.output
    terms = ["--modality", "video", "--purpose", "training", "--model-class", "academic"]
    r = run("attach-consent", f"at://{ALICE}/org.consentvid.video/x", "--attest-authorship", *terms, "--people", "onlyMe")
    assert r.exit_code == 2 and "attest-adult" in r.output
    r = run("attach-consent", f"at://{ALICE}/org.consentvid.video/x", "--attest-authorship", "--attest-adult", *terms)
    assert r.exit_code == 2 and "--people" in r.output
    r = run(
        "attach-consent", f"at://{ALICE}/org.consentvid.video/x", *attested[:2],
        "--people", "noIdentifiablePeople", *terms, "--modality", "face",
    )
    assert r.exit_code == 2 and "contradicts" in r.output


def test_deprecated_values_not_offered():
    r = run("attach-consent", f"at://{ALICE}/org.consentvid.video/x", "--purpose", "researchOnly")
    assert r.exit_code == 2 and "researchOnly" in r.output and "Invalid value" in r.output


def test_resync_is_strict_by_default():
    opt = next(p for p in cli.resync.params if p.name == "strict")
    assert opt.default is True


def test_not_logged_in_message():
    r = run("whoami")
    assert r.exit_code == 1 and "consentvid login" in r.output and "Traceback" not in r.output


@pytest.mark.parametrize(
    "value,ok",
    [
        ("90d", True),
        ("2y", True),
        ("2999-01-01", True),
        ("2999-01-01T00:00:00Z", True),
        ("2000-01-01", False),
        ("2999-01-01T00:00:00", False),
        ("soon", False),
    ],
)
def test_expiry_parsing(value, ok):
    if ok:
        assert _core._parse_expiry(value).endswith("Z")
    else:
        with pytest.raises(UsageError):
            _core._parse_expiry(value)


def _add_video(fake_client, rkey=K1, **kw):
    """A version-1 video record (readers still accept it)."""
    fake_client.repo.records[("org.consentvid.video", rkey)] = (VIDEO_CID, video_record(**kw))
    return f"at://{ALICE}/org.consentvid.video/{rkey}"


def _add_video_v2(fake_client, rkey=K1, **kw):
    fake_client.repo.records[("org.consentvid.videoV2", rkey)] = (VIDEO_CID, video_v2_record(**kw))
    return f"at://{ALICE}/org.consentvid.videoV2/{rkey}"


def test_attach_pins_video_version_and_is_one_per_video(fake_client):
    vuri = _add_video_v2(fake_client)
    uri, cid, rec, replaced = _core._attach(fake_client, vuri, replace=False, validate=None, **TERMS)
    assert rec["videoCid"] == VIDEO_CID and rec["revocable"] is True and not replaced
    op, params = fake_client.repo.calls[-1]
    assert op == "create" and params["rkey"] == K1  # the video's own key: a sidecar (R1-01)
    assert uri == f"at://{ALICE}/org.consentvid.consentV2/{K1}"
    assert params["collection"] == "org.consentvid.consentV2"
    assert params["record"]["subject"] == {"uri": vuri, "cid": VIDEO_CID}  # a strongRef
    assert "revocable" not in params["record"] and params["record"]["rightsBasis"] == "own-recording"
    assert "validate" not in params  # PDS default policy
    with pytest.raises(ConsentVidError, match="already has consent"):
        _core._attach(fake_client, vuri, replace=False, validate=None, **TERMS)
    uri2, _, _, replaced = _core._attach(
        fake_client, vuri, replace=True, validate=None, **{**TERMS, "purposes": ("evaluation",)}
    )
    assert replaced and uri2 == uri  # same record, new version
    op, params = fake_client.repo.calls[-1]
    assert op == "put" and params["swapRecord"] == cid and params["rkey"] == K1


def test_attach_replace_creates_when_absent_and_reports_concurrent_change(fake_client, monkeypatch):
    vuri = _add_video_v2(fake_client)
    _, cid, _, replaced = _core._attach(fake_client, vuri, replace=True, validate=None, **TERMS)
    assert not replaced and fake_client.repo.calls[-1][0] == "create"
    real_get = fake_client.repo.get_record

    def stale_get(p):  # someone rewrites the consent between our read and our write
        r = real_get(p)
        if p["collection"] == "org.consentvid.consentV2":
            fake_client.repo.records[(p["collection"], p["rkey"])] = ("bafyother", r.value)
        return r

    monkeypatch.setattr(fake_client.repo, "get_record", stale_get)
    with pytest.raises(ConsentVidError, match="changed concurrently"):
        _core._attach(fake_client, vuri, replace=True, validate=None, **TERMS)


def test_attach_refuses_other_repo(fake_client):
    with pytest.raises(ConsentVidError, match="not in your repo"):
        _core._attach(fake_client, f"at://{BOB}/org.consentvid.videoV2/{K1}", replace=False, validate=None, **TERMS)


def test_attach_refuses_a_version_1_video(fake_client):
    """V2 consents bind only V2 videos; V1 videos can't get new consents."""
    fake_client.repo.records[("org.consentvid.video", K1)] = (VIDEO_CID, video_record())
    with pytest.raises(UsageError, match="version-1 video record"):
        _core._attach(fake_client, f"at://{ALICE}/org.consentvid.video/{K1}", replace=False, validate=None, **TERMS)
    assert not [c for c in fake_client.repo.calls if c[0] in ("create", "put")]


VOPTS = dict(
    title="t",
    description="",
    language="",
    tags=(),
    storage_value=None,
    keep_metadata=False,
    allow_duplicate=False,
    validate=None,
)


def test_register_local_never_publishes_paths(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    f = tmp_path / "secret-folder" / "clip.mp4"
    f.parent.mkdir()
    f.write_bytes(b"video bytes")
    opts = {**VOPTS, "keep_metadata": True}  # no ffmpeg here
    uri, _, rec = _core._register(fake_client, f, storage_kind="local", **opts)
    assert rec["storageRef"]["value"] == rec["contentHash"] + ".mp4" and rec["mimeType"] == "video/mp4"
    with pytest.raises(ConsentVidError, match="already registered"):
        _core._register(fake_client, f, storage_kind="local", **opts)


@pytest.mark.parametrize("kind,value", [("local", None), ("url", "https://example.org/v.mp4")])
def test_every_kind_needs_ffmpeg_unless_keep_metadata(fake_client, tmp_path, monkeypatch, kind, value):
    """R2-06: pointer and local kinds are sanitised too, so no ffmpeg = refuse."""
    from consentvid.errors import ConfigError

    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video bytes")
    with pytest.raises(ConfigError, match="ffmpeg is required"):
        _core._register(fake_client, f, storage_kind=kind, **{**VOPTS, "storage_value": value})
    assert not fake_client.repo.records


def _fake_media(monkeypatch, probe=None, calls=None):
    """media.probe returns ``probe`` (default: no tags); media.sanitise writes
    b'clean' and records the call."""
    from consentvid.media import Probe

    def sanitise(src, dst):
        if calls is not None:
            calls.append((src, dst))
        dst.write_bytes(b"clean")
        return Probe(content_hash=cli.hash_file(dst))

    monkeypatch.setattr("consentvid.media.probe", lambda p: probe if probe is not None else Probe())
    monkeypatch.setattr("consentvid.media.sanitise", sanitise)


R2_URL = "https://r2.example/videos/x"


def _upload(value=R2_URL, created=True):
    from consentvid.storage import Upload

    return lambda *a, **k: Upload(value, created)


def test_local_storage_ref_does_not_publish_the_camera_file_name(fake_client, tmp_path, monkeypatch):
    """R3-15: PXL_20261106_101530123 = capture timestamp + device family."""
    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    f = tmp_path / "PXL_20261106_101530123.MP4"
    f.write_bytes(b"video bytes")
    _, _, rec = _core._register(fake_client, f, storage_kind="local", **{**VOPTS, "keep_metadata": True})
    value = rec["storageRef"]["value"]
    assert "PXL" not in value and "20261106" not in value
    assert value == rec["contentHash"] + ".mp4"
    from consentvid.validate import storage_ref_errors

    assert storage_ref_errors(rec["storageRef"]) == []


def test_register_validates_before_upload(fake_client, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    uploads = []
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: uploads.append(a) or _upload()())
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(ConsentVidError, match="maxGraphemes"):
        _core._register(fake_client, f, storage_kind="r2", **{**VOPTS, "title": "x" * 400})
    assert uploads == []


def test_register_cleans_up_bytes_if_record_write_fails(fake_client, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append((ref, h)) or "ok")

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted and deleted[0][0]["value"] == R2_URL
    assert deleted[0][1] == "sha256-" + __import__("hashlib").sha256(b"clean").hexdigest()


def test_failed_register_never_deletes_an_object_it_did_not_create(fake_client, tmp_path, monkeypatch):
    """R1-10: head_object found the object already there: someone else's."""
    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload(created=False))
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append(ref) or "ok")

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_metadata_is_stripped(fake_client, tmp_path):
    f = tmp_path / "gps.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=d=1:s=64x48",
            "-metadata",
            "location=+50.0755+014.4378/",
            "-metadata",
            "make=Apple",
            "-movflags",
            "use_metadata_tags",
            str(f),
        ],
        check=True,
    )
    uri, _, rec = _core._register(fake_client, f, storage_kind="local", **VOPTS)
    clean = tmp_path / "gps.clean.mp4"
    assert clean.exists() and rec["contentHash"] == cli.hash_file(clean)
    assert rec["width"] == 64 and rec["durationMs"] == 1000
    from consentvid.media import probe

    assert probe(f).sensitive_tags and not probe(clean).sensitive_tags


def test_lexicon_mirror_in_sync():
    pkg = ROOT / "src/consentvid/schemas"
    for f in pkg.glob("*.json"):
        assert json.loads(f.read_text()) == json.loads((ROOT / "web/lexicons" / f.name).read_text()), f.name


def test_pointer_plus_metadata_refused(fake_client, tmp_path, monkeypatch):
    """The hash must describe the bytes the pointer serves (review finding 4)."""
    from consentvid.media import Probe

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe(sensitive_tags={"format:location": "x"}))
    monkeypatch.setattr("consentvid.media.sanitise", lambda src, dst: (dst.write_bytes(b"clean"), Probe())[1])
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig")
    cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    with pytest.raises(ConsentVidError, match="clean copy was written"):
        _core._register(fake_client, f, storage_kind="ipfs", **{**VOPTS, "storage_value": cid})
    assert (tmp_path / "clip.clean.mp4").exists() and not fake_client.repo.records


def test_local_value_names_the_hashed_file(fake_client, tmp_path, monkeypatch):
    from consentvid.media import Probe

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe(sensitive_tags={"format:make": "x"}))
    monkeypatch.setattr("consentvid.media.sanitise", lambda src, dst: (dst.write_bytes(b"clean"), Probe())[1])
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig")
    _, _, rec = _core._register(fake_client, f, storage_kind="local", **VOPTS)
    assert rec["storageRef"]["value"] == rec["contentHash"] + ".mp4"  # R3-15: not the file name
    assert rec["contentHash"] == cli.hash_file(tmp_path / "clip.clean.mp4")


def test_shared_bytes_not_deleted_on_failed_write(fake_client, tmp_path, monkeypatch):
    """Duplicates share a content-addressed object (review finding 5)."""
    _fake_media(monkeypatch)
    url = "https://r2.example/videos/x"
    monkeypatch.setattr("consentvid.storage.upload", _upload(url))
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append(ref) or "ok")
    _add_video(fake_client, storageRef={"kind": "r2", "value": url})

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        _core._register(fake_client, f, storage_kind="r2", **{**VOPTS, "allow_duplicate": True})
    assert deleted == []


# ----------------------------------------- R1-06 / R1-24: settings files


@pytest.fixture
def env_sandbox(monkeypatch, tmp_path):
    """Run the real env loader against tmp files only; restore os.environ."""
    import os

    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in list(os.environ):
        if k.startswith("CONSENTVID_") and k != "CONSENTVID_HOME":
            del os.environ[k]
    for k in ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE", "HTTP_PROXY"):
        os.environ.pop(k, None)
    monkeypatch.setattr(_core, "_checkout_env", lambda: None)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def test_env_file_in_cwd_is_ignored(env_sandbox, tmp_path, monkeypatch):
    import os

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / ".env").write_text("CONSENTVID_SERVICE=http://evil\nHTTPS_PROXY=http://evil\n")
    cfg = env_sandbox / "config.env"
    cfg.write_text("CONSENTVID_SERVICE=https://bsky.social\n")
    cfg.chmod(0o600)
    monkeypatch.chdir(dataset)
    _core._apply_env_files()
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["CONSENTVID_SERVICE"] == "https://bsky.social"


def test_env_files_only_export_consentvid_keys(env_sandbox):
    import os

    cfg = env_sandbox / "config.env"
    cfg.write_text(
        "SSL_CERT_FILE=/tmp/evil.pem\nHTTPS_PROXY=http://evil\nCONSENTVID_HOME=/tmp/x\n"
        "CONSENTVID_ALLOW_INSECURE=1\nCONSENTVID_STORAGE=r2\n"
    )
    cfg.chmod(0o600)
    os.environ["CONSENTVID_STORAGE"] = "local"  # real env wins
    _core._apply_env_files()
    assert "SSL_CERT_FILE" not in os.environ and "HTTPS_PROXY" not in os.environ
    assert "CONSENTVID_ALLOW_INSECURE" not in os.environ
    assert os.environ["CONSENTVID_HOME"] != "/tmp/x"
    assert os.environ["CONSENTVID_STORAGE"] == "local"


def test_world_readable_env_file_warns(env_sandbox, capsys):
    cfg = env_sandbox / "config.env"
    cfg.write_text("CONSENTVID_HANDLE=me.test\n")
    cfg.chmod(0o644)
    _core.set_reporter(cli._click_reporter)  # as when the CLI runs
    _core._apply_env_files()
    assert "chmod 600" in capsys.readouterr().err
    cfg.chmod(0o600)
    _core._apply_env_files()
    assert "chmod 600" not in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://evil.example", "ftp://x", "https://u:p@evil.example"])
def test_service_must_be_https(monkeypatch, url):
    from consentvid.errors import ConfigError

    monkeypatch.setenv("CONSENTVID_SERVICE", url)
    with pytest.raises(ConfigError):
        _core._new_client()


# ------------------------------------- R1-12: uploads are always sanitised


def test_upload_is_sanitised_even_when_probe_sees_nothing(fake_client, tmp_path, monkeypatch):
    calls = []
    _fake_media(monkeypatch, calls=calls)
    uploaded = []
    monkeypatch.setattr(
        "consentvid.storage.upload", lambda kind, path, **k: uploaded.append(path.read_bytes()) or _upload()()
    )
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig with xmp")
    _, _, rec = _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert calls and calls[0][0] == f and uploaded == [b"clean"]
    assert rec["contentHash"] == "sha256-" + __import__("hashlib").sha256(b"clean").hexdigest()


def test_upload_without_ffmpeg_refused_unless_keep_metadata(fake_client, tmp_path, monkeypatch):
    from consentvid.errors import ConfigError

    monkeypatch.setattr("consentvid.media.probe", lambda p: None)  # ffprobe missing
    uploads = []
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: uploads.append(a) or _upload()())
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(ConfigError, match="ffmpeg is required"):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert uploads == []
    _core._register(fake_client, f, storage_kind="r2", **{**VOPTS, "keep_metadata": True})
    assert len(uploads) == 1


def test_storage_value_is_validated_per_kind(fake_client, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    for kind, value in (("r2", "/Users/me/clip.mp4"), ("ipfs", "../ipns/x"), ("peertube", "http://pt/w/x")):
        with pytest.raises(UsageError):
            _core._register(fake_client, f, storage_kind=kind, **{**VOPTS, "storage_value": value})
    assert not fake_client.repo.records


# ------------------------------------------------ R1-10: shared bytes


@pytest.mark.parametrize(
    "mine,theirs",
    [
        ({"kind": "ipfs", "value": "ipfs://bafyX"}, {"kind": "ipfs", "value": "bafyX"}),
        (
            {"kind": "r2", "value": f"https://new.r2.dev/videos/sha256-{'ab' * 32}"},
            {"kind": "r2", "value": f"https://old.r2.dev/videos/sha256-{'ab' * 32}"},
        ),
        ({"kind": "peertube", "value": "https://pt.example/w/abcdefgh12"}, {"kind": "peertube", "value": "https://pt.example/w/abcdefgh12/"}),
    ],
)
def test_bytes_shared_normalises_pointers(fake_client, mine, theirs):
    other = _add_video(fake_client, K2, storageRef=theirs, contentHash="sha256-" + "cd" * 32)
    assert _core._bytes_shared(fake_client, ALICE, mine, "sha256-" + "ef" * 32) == other


def test_bytes_shared_by_content_hash(fake_client):
    h = "sha256-" + "cd" * 32
    other = _add_video(fake_client, K2, contentHash=h, storageRef={"kind": "url", "value": "https://a/b"})
    assert _core._bytes_shared(fake_client, ALICE, {"kind": "r2", "value": "https://x/y"}, h) == other
    assert _core._bytes_shared(fake_client, ALICE, {"kind": "r2", "value": "https://x/y"}, h, exclude_uri=other) is None


def test_revoke_delete_bytes_passes_the_content_hash(fake_client, monkeypatch):
    h = "sha256-" + "ab" * 32
    vuri = _add_video(fake_client, contentHash=h)
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    got = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, ch: got.append((ref, ch)) or "deleted")
    r = run("revoke", vuri, "--delete-bytes", "-y")
    assert r.exit_code == 0, r.output
    assert got == [(video_record(contentHash=h)["storageRef"], h)]


def test_revoke_delete_bytes_failure_says_bytes_may_be_public(fake_client, monkeypatch):
    """R3-11: the records are gone; a failed byte delete must be loud."""
    import httpx

    vuri = _add_video(fake_client, contentHash="sha256-" + "ab" * 32)
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)

    def fail(ref, ch):
        raise httpx.ConnectError("instance down")

    monkeypatch.setattr("consentvid.storage.delete_bytes", fail)
    r = run("revoke", vuri, "--delete-bytes", "-y")
    assert r.exit_code != 0
    assert "may still be public" in r.output and "Deleted video record" in r.output, r.output


# ---------------------- R3-09: a failed register never unpins a pin it didn't create


@pytest.mark.parametrize("body,unpinned", [({"isDuplicate": True}, False), ({}, False), ({"isDuplicate": False}, True)])
def test_failed_register_never_unpins_an_existing_pinata_pin(fake_client, tmp_path, monkeypatch, body, unpinned):
    import httpx

    from conftest import MockHttpx
    from consentvid import storage

    cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    _fake_media(monkeypatch)
    monkeypatch.setenv("CONSENTVID_PINATA_JWT", "jwt")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/pinning/pinFileToIPFS":
            return httpx.Response(200, json={"IpfsHash": cid, **body})
        return httpx.Response(200, json={})

    monkeypatch.setattr(storage, "_httpx", lambda: MockHttpx(handler))

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        _core._register(fake_client, f, storage_kind="ipfs", **VOPTS)
    deletes = [x for x in seen if x[0] == "DELETE"]
    assert deletes == ([("DELETE", f"/pinning/unpin/{cid}")] if unpinned else [])


def test_failed_register_never_unpins_an_existing_kubo_pin(fake_client, tmp_path, monkeypatch):
    import httpx

    from conftest import MockHttpx
    from consentvid import storage

    cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    _fake_media(monkeypatch)
    monkeypatch.delenv("CONSENTVID_PINATA_JWT", raising=False)
    monkeypatch.setenv("CONSENTVID_IPFS_API", "http://127.0.0.1:5001")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v0/add":
            return httpx.Response(200, text=json.dumps({"Hash": cid}))
        if request.url.path == "/api/v0/pin/ls":
            return httpx.Response(200, json={"Keys": {cid: {"Type": "recursive"}}})  # a lab-mate's pin
        return httpx.Response(200, json={})

    monkeypatch.setattr(storage, "_httpx", lambda: MockHttpx(handler))

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        _core._register(fake_client, f, storage_kind="ipfs", **VOPTS)
    assert "/api/v0/pin/rm" not in seen and seen.count("/api/v0/add") == 2


# ------------------------------ R1-01 / R1-02: one consent; atomic revoke


def test_revoke_consent_is_one_delete(fake_client, monkeypatch):
    from conftest import consent_record

    _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", consent_record())
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    r = run("revoke", f"at://{ALICE}/org.consentvid.consent/{K1}", "-y", "--all")  # --all: deprecated no-op
    assert r.exit_code == 0, r.output
    assert [c[0] for c in fake_client.repo.calls] == ["delete"]
    assert "deprecated" in r.output


def test_revoke_video_is_one_apply_writes_commit(fake_client, monkeypatch):
    from conftest import consent_record

    vuri = _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", consent_record())
    fake_client.repo.records[("org.consentvid.consent", K2)] = ("bafyb", consent_record())  # legacy, mis-keyed
    fake_client.repo.records[("org.consentvid.consent", K0)] = ("bafyc", consent_record(rkey=K0))  # another video
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    r = run("revoke", vuri, "-y")
    assert r.exit_code == 0, r.output
    writes = [c for c in fake_client.repo.calls if c[0] not in ("get", "get_latest_commit")]
    assert [c[0] for c in writes] == ["apply_writes"]
    ops = [(w["$type"].rsplit("#", 1)[1], w["collection"], w["rkey"]) for w in writes[0][1]["writes"]]
    assert ops[-1] == ("delete", "org.consentvid.video", K1)  # the video last
    assert sorted(ops[:-1]) == [("delete", "org.consentvid.consent", K1), ("delete", "org.consentvid.consent", K2)]
    assert set(fake_client.repo.records) == {("org.consentvid.consent", K0)}


def test_revoke_chunks_beyond_the_apply_writes_limit(fake_client, monkeypatch):
    monkeypatch.setattr(_core, "_APPLY_WRITES_MAX", 2)
    targets = [("c", "a"), ("c", "b"), ("c", "d"), ("v", "x")]
    batches = []
    fake_client.com.atproto.repo.apply_writes = lambda p: batches.append(([w["rkey"] for w in p["writes"]],
                                                                          p.get("swapCommit")))
    _core._delete_in_one_commit(fake_client, ALICE, targets, swap_commit="bafyhead")
    # R2-16: swapCommit on the first commit only (later ones follow it).
    assert batches == [(["a", "b"], "bafyhead"), (["d", "x"], None)]


def test_revoke_deletes_a_sidecar_whatever_its_video_uri(fake_client, monkeypatch):
    """R2-16: a sidecar with a mismatched videoUri must not stay behind."""
    from conftest import consent_record

    vuri = _add_video(fake_client)
    stray = consent_record(videoUri=f"at://{ALICE}/org.consentvid.video/{K0}")
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", stray)
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    r = run("revoke", vuri, "-y")
    assert r.exit_code == 0, r.output
    assert fake_client.repo.records == {}
    (apply,) = [p for op, p in fake_client.repo.calls if op == "apply_writes"]
    assert apply["swapCommit"] == "bafycommit1"


def test_revoke_survives_unrelated_writes_with_one_prompt(fake_client, monkeypatch):
    """R3-05: the head moves while the prompt waits (a like) and before the
    first two commits; the revoke still succeeds, asking only once."""
    from conftest import consent_record

    vuri = _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", consent_record())
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    sleeps = []
    monkeypatch.setattr(_core, "_revoke_sleep", sleeps.append)
    real_confirm, prompts = click.confirm, []

    def confirm(*a, **k):
        prompts.append(a)
        fake_client.repo.head += 1  # a Bluesky like while the prompt waits
        return real_confirm(*a, **k)

    monkeypatch.setattr(click, "confirm", confirm)
    real_apply, conflicts = fake_client.repo.apply_writes, {"n": 2}

    def apply_writes(p):
        if conflicts["n"]:
            conflicts["n"] -= 1
            fake_client.repo.head += 1  # another write lands just before ours
        return real_apply(p)

    fake_client.repo.apply_writes = apply_writes
    r = run("revoke", vuri, input="y\n")
    assert r.exit_code == 0, r.output
    assert len(prompts) == 1 and fake_client.repo.records == {}
    applies = [p for op, p in fake_client.repo.calls if op == "apply_writes"]
    assert len(applies) == 3 and len(sleeps) == 2 and all(0.2 <= x <= 1.0 for x in sleeps)
    assert "Deleted video record" in r.output


def test_revoke_asks_again_when_what_it_deletes_changed(fake_client, monkeypatch):
    from conftest import consent_record

    vuri = _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", consent_record())
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    real_confirm, prompts = click.confirm, []

    def confirm(*a, **k):
        prompts.append(a)
        if len(prompts) == 1:  # a legacy consent for this video appears meanwhile
            fake_client.repo.records[("org.consentvid.consent", K2)] = ("bafyb", consent_record())
        return real_confirm(*a, **k)

    monkeypatch.setattr(click, "confirm", confirm)
    r = run("revoke", vuri, input="y\ny\n")
    assert r.exit_code == 0, r.output
    assert len(prompts) == 2 and "changed since you confirmed it" in r.output
    assert "2 consent receipt(s) will be deleted" in r.output and fake_client.repo.records == {}


def test_revoke_declined_on_the_second_prompt_deletes_nothing(fake_client, monkeypatch):
    from conftest import consent_record

    vuri = _add_video(fake_client)
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    real_confirm = click.confirm

    def confirm(*a, **k):
        fake_client.repo.records[("org.consentvid.consent", K2)] = ("bafyb", consent_record())
        return real_confirm(*a, **k)

    monkeypatch.setattr(click, "confirm", confirm)
    r = run("revoke", vuri, input="y\nn\n")
    assert r.exit_code != 0 and not [c for c in fake_client.repo.calls if c[0] == "apply_writes"]


def test_revoke_gives_up_after_five_conflicts(fake_client, monkeypatch):
    from conftest import consent_record

    vuri = _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", K1)] = ("bafya", consent_record())
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    sleeps = []
    monkeypatch.setattr(_core, "_revoke_sleep", sleeps.append)
    real = fake_client.repo.get_latest_commit

    def always_racing(p):
        out = real(p)
        fake_client.repo.head += 1
        return out

    fake_client.repo.get_latest_commit = always_racing
    r = run("revoke", vuri, "-y")
    assert r.exit_code != 0 and "your repo kept changing during revoke; re-run" in r.output
    assert len([c for c in fake_client.repo.calls if c[0] == "apply_writes"]) == 5 and len(sleeps) == 4
    assert len(fake_client.repo.records) == 2  # nothing deleted


# ------------------------------------------------------- R1-21: logout


class _SessionClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
        self.com = type("C", (), {})()
        self.com.atproto = type("A", (), {})()
        self.com.atproto.server = self

    def _import_session_string(self, s):
        self.calls.append(("import", s))

    def _get_refresh_auth_headers(self):
        return {"Authorization": "Bearer refresh"}

    def delete_session(self, **kw):
        self.calls.append(("delete_session", kw))
        if self.fail:
            raise RuntimeError("PDS down")
        return True


@pytest.mark.parametrize("fail", [False, True])
def test_logout_ends_server_session_and_removes_file(monkeypatch, fail):
    fake = _SessionClient(fail)
    monkeypatch.setattr(_core, "_throwaway_client", lambda: fake)
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("handle:::did:::access:::refresh:::https://pds")
    r = run("logout")
    assert r.exit_code == 0, r.output
    assert not _core.SESSION_PATH.exists()
    assert fake.calls[0][0] == "import" and fake.calls[1][0] == "delete_session"
    assert fake.calls[1][1]["headers"] == {"Authorization": "Bearer refresh"}
    assert "app-passwords" in r.output
    assert ("could not end the session" in r.output) is fail



# ---------------------------- R2-06: pointer/local kinds always sanitised


def _fixed_point_media(monkeypatch, clean=b"clean"):
    """sanitise() maps any input to ``clean`` (so ``clean`` is its fixed point)."""
    from consentvid.media import Probe

    calls = []

    def sanitise(src, dst):
        calls.append(src)
        dst.write_bytes(clean)
        return Probe(content_hash=cli.hash_file(dst))

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe())  # sees nothing
    monkeypatch.setattr("consentvid.media.sanitise", sanitise)
    monkeypatch.setattr("consentvid.media.have_ffmpeg", lambda: True)
    return calls


def test_pointer_is_refused_even_when_probe_sees_nothing(fake_client, tmp_path, monkeypatch):
    _fixed_point_media(monkeypatch)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig with a vendor box")
    opts = {**VOPTS, "storage_value": "https://example.org/clip.mp4"}
    with pytest.raises(ConsentVidError, match="clean copy was written"):
        _core._register(fake_client, f, storage_kind="url", **opts)
    clean = tmp_path / "clip.clean.mp4"
    assert clean.read_bytes() == b"clean" and not fake_client.repo.records
    # Re-running on the clean copy is accepted: it is the fixed point.
    _, _, rec = _core._register(fake_client, clean, storage_kind="url", **opts)
    assert rec["contentHash"] == cli.hash_file(clean)


def test_local_commits_to_the_clean_copy_even_when_probe_sees_nothing(fake_client, tmp_path, monkeypatch):
    _fixed_point_media(monkeypatch)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig with a vendor box")
    _, _, rec = _core._register(fake_client, f, storage_kind="local", **VOPTS)
    assert rec["storageRef"]["value"] == rec["contentHash"] + ".mp4"  # R3-15: not the file name
    assert rec["contentHash"] == cli.hash_file(tmp_path / "clip.clean.mp4")


def test_sanitising_that_never_converges_fails_closed(fake_client, tmp_path, monkeypatch):
    from consentvid.media import Probe

    n = []

    def sanitise(src, dst):
        n.append(1)
        dst.write_bytes(b"pass %d" % len(n))
        return Probe()

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe())
    monkeypatch.setattr("consentvid.media.sanitise", sanitise)
    monkeypatch.setattr("consentvid.media.have_ffmpeg", lambda: True)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig")
    with pytest.raises(ConsentVidError, match="did not converge"):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert len(n) == _core._SANITISE_PASSES and not fake_client.repo.records


# ------------------------------------------ R1-01: export, sidecar only


def test_export_uses_only_the_sidecar_consent(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from conftest import consent_record
    from consentvid import identity

    V, C = "org.consentvid.video", "org.consentvid.consent"
    vuri = f"at://{ALICE}/{V}/{K1}"
    sidecar = consent_record(authorshipAttested=False)
    misskeyed = consent_record(modelClasses=["academic", "commercial"])
    listing = {
        V: [SimpleNamespace(uri=vuri, cid=VIDEO_CID, value=video_record())],
        C: [
            SimpleNamespace(uri=f"at://{ALICE}/{C}/{K1}", cid="bafyside", value=sidecar),
            SimpleNamespace(uri=f"at://{ALICE}/{C}/{K2}", cid="bafyother", value=misskeyed),
        ],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(identity, "display_name", lambda did: "alice.test")
    monkeypatch.setattr(_core, "_list_all", lambda client, did, coll: listing.get(coll, []))
    out = tmp_path / "m.json"
    r = run("export", str(out), "--repo", ALICE)
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["distribution"] == []  # the mis-keyed one never stands in
    assert "is invalid" in r.output and "record key doesn't match its video" in r.output
    listing[C][0].value = consent_record()
    r = run("export", str(out), "--repo", ALICE)
    (fo,) = json.loads(out.read_text())["distribution"]
    assert fo["cv:consentReceipt"]["atUri"].endswith(f"/{K1}")


# ------------------------------------ R2-16: private files from the start


def test_session_file_is_written_0600(tmp_path, monkeypatch):
    import os
    import stat

    old_umask = os.umask(0o022)
    try:
        client = _core._new_client()
        monkeypatch.setattr(client, "export_session_string", lambda: "secret-session")
        (save,) = client._session_dispatcher._on_session_change_callbacks
        save(None, None)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(_core.SESSION_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(_core.CONFIG_DIR.stat().st_mode) == 0o700
    assert _core.SESSION_PATH.read_text() == "secret-session"


def test_write_private_never_creates_readable_files(tmp_path, monkeypatch):
    import os

    from consentvid import paths

    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        modes.append((str(path), flags, mode))
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(paths.os, "open", spy)
    target = tmp_path / "secret"
    target.symlink_to(tmp_path / "elsewhere")  # replaced, never written through
    paths.write_private(target, "x")
    assert modes and all(m == 0o600 and f & os.O_EXCL for _, f, m in modes)
    assert not (tmp_path / "elsewhere").exists() and target.read_text() == "x"
    assert not target.is_symlink()


# ----------------------------------------- R2-10: appview output escaped


def test_appview_query_escapes_untrusted_strings(monkeypatch):
    from conftest import consent_record
    from consentvid import appview as av

    conn = av.connect()
    evil_title = "t\r\x1b[2K\x1b]52;c;cHduZWQ=\x07"
    av.upsert_video(conn, f"at://{ALICE}/org.consentvid.video/{K1}", ALICE, K1, VIDEO_CID, video_record(title=evil_title))
    # Stored as an older index would have it (validation now rejects these terms).
    av.upsert_consent(
        conn, f"at://{ALICE}/org.consentvid.consent/{K1}", ALICE, K1, "bafyc",
        consent_record(modelClasses=["commercial\x1b[2K\r"], captureProvenance="x\u202e"),
    )
    conn.commit()
    r = CliRunner().invoke(cli.main, ["appview", "query"], color=True)
    assert r.exit_code == 0, r.output
    for ch in ("\r", "\x07", "\u202e", "\x1b]", "\x1b[2K"):
        assert ch not in r.output, (ch, r.output)
    assert "\\x1b" in r.output


# ----------------------------- R2-16: C2PA signer key private from birth


def test_c2pa_signer_key_is_created_private(tmp_path, monkeypatch):
    import os
    import stat

    pytest.importorskip("cryptography")
    from consentvid import c2pa_embed as ce
    from consentvid import paths

    home = tmp_path / "fresh-home"  # embed-c2pa before any login: nothing exists yet
    monkeypatch.setattr(ce, "_SIGNER_DIR", home / "c2pa")
    monkeypatch.setattr(ce, "_CERT_PATH", home / "c2pa" / "signer.cert.pem")
    monkeypatch.setattr(ce, "_KEY_PATH", home / "c2pa" / "signer.key.pem")
    opened = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        opened.append((os.path.basename(str(path)), mode))
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(paths.os, "open", spy)
    old = os.umask(0o022)
    try:
        cert, key = ce.ensure_signer_cert()
    finally:
        os.umask(old)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "c2pa").stat().st_mode) == 0o700
    assert [m for name, m in opened if "signer.key.pem" in name] == [0o600]
    assert b"PRIVATE KEY" in key.read_bytes()


@pytest.mark.parametrize("base", ["https://pub-x.r2.dev:9", "https://127.1", "https://r2.internal"])
def test_r2_public_base_is_checked_before_upload(fake_client, tmp_path, monkeypatch, base):
    """R2-11: the storageRef host/port rules apply to the configured base too,
    and are checked before any bytes leave the machine."""
    _fake_media(monkeypatch)
    uploads = []
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: uploads.append(a) or _upload()())
    monkeypatch.setenv("CONSENTVID_R2_PUBLIC_URL", base)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(ConsentVidError, match="storage for 'r2'"):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert uploads == [] and not fake_client.repo.records


# ------------------------------------- R1-02: contribute is one atomic commit


def _contribute(fake_client, tmp_path, monkeypatch, deleted=None):
    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    monkeypatch.setattr(
        "consentvid.storage.delete_bytes", lambda ref, h: (deleted.append(ref) if deleted is not None else None) or "ok"
    )
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    return run(
        "contribute", str(f), "--title", "t", "--storage-kind", "r2", "--attest-authorship", "--attest-adult",
        "--people", "onlyMe", "--modality", "video", "--purpose", "training", "--model-class", "academic",
    )


def test_contribute_writes_video_and_consent_in_one_apply_writes(fake_client, tmp_path, monkeypatch):
    from consentvid.records import compute_cid, is_tid

    r = _contribute(fake_client, tmp_path, monkeypatch)
    assert r.exit_code == 0, r.output
    writes = [c for c in fake_client.repo.calls if c[0] in ("create", "put", "delete", "apply_writes")]
    assert [c[0] for c in writes] == ["apply_writes"]
    video_w, consent_w = writes[0][1]["writes"]
    assert video_w["$type"] == consent_w["$type"] == "com.atproto.repo.applyWrites#create"
    assert video_w["collection"] == "org.consentvid.videoV2" and consent_w["collection"] == "org.consentvid.consentV2"
    assert video_w["rkey"] == consent_w["rkey"] and is_tid(video_w["rkey"])
    assert consent_w["value"]["subject"] == {
        "uri": f"at://{ALICE}/org.consentvid.videoV2/{video_w['rkey']}", "cid": compute_cid(video_w["value"])
    }
    video = video_w["value"]
    assert video["contentHash"].startswith("bafkrei") and video["storage"]["$type"].endswith("#r2Ref")
    assert video["size"] == len(b"bytes") and "storageRef" not in video
    assert consent_w["value"]["peoplePresent"] == "only-me"
    assert len(fake_client.repo.records) == 2


def test_failed_contribute_cleans_up_bytes_and_writes_nothing(fake_client, tmp_path, monkeypatch):
    fake_client.repo.fail_apply = RuntimeError("PDS down")
    deleted = []
    r = _contribute(fake_client, tmp_path, monkeypatch, deleted)
    assert r.exit_code != 0
    assert deleted and deleted[0]["value"] == R2_URL
    assert not fake_client.repo.records


def test_contribute_repins_consent_if_the_pds_assigns_another_video_cid(fake_client, tmp_path, monkeypatch):
    from consentvid.records import compute_cid

    real, actual = fake_client.repo.apply_writes, compute_cid({"re-encoded": True})

    def apply_writes(p):
        resp = real(p)
        key = (p["writes"][0]["collection"], p["writes"][0]["rkey"])
        fake_client.repo.records[key] = (actual, fake_client.repo.records[key][1])
        resp.results[0].cid = actual
        return resp

    fake_client.repo.apply_writes = apply_writes
    r = _contribute(fake_client, tmp_path, monkeypatch)
    assert r.exit_code == 0, r.output
    op, params = fake_client.repo.calls[-1]
    assert op == "put" and params["record"]["subject"]["cid"] == actual and "swapRecord" in params


# --------------------------------- R1-15: a 429 keeps the uploaded bytes


def _rate_limited_error(reset_in=60):
    import time

    from atproto_client.exceptions import RequestException
    from atproto_client.request import Response

    return RequestException(
        Response(success=False, status_code=429, content=None, headers={"ratelimit-reset": str(int(time.time()) + reset_in)})
    )


def test_rate_limited_contribute_keeps_the_uploaded_bytes(fake_client, tmp_path, monkeypatch):
    fake_client.repo.fail_apply = _rate_limited_error()
    deleted = []
    r = _contribute(fake_client, tmp_path, monkeypatch, deleted)
    assert r.exit_code != 0 and deleted == []
    assert "rate limited by your PDS until" in r.output and "bytes are kept" in r.output


def test_rate_limited_register_keeps_the_uploaded_bytes(fake_client, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append(ref) or "ok")

    def limited(p):
        raise _rate_limited_error()

    fake_client.repo.create_record = limited
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(ConsentVidError, match="rate limited"):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted == []


# ------------------------------ R1-21: byte limits also report characters


def test_byte_limit_error_reports_characters(fake_client):
    rec = {**video_record(), "title": "字" * 101}
    with pytest.raises(ConsentVidError, match=r"303 bytes exceeds maxLength 300 \(101 characters"):
        _core._check_before_write("org.consentvid.video", rec, ALICE)


# ----------------------- R1-03: log in at the account's own PDS, no AppView


class _LoginClient:
    instances = []
    session_info = {}  # extra getSession / createSession fields (active, status)
    session_pds = "https://pds.alice.example"  # where an imported session points
    scope = "com.atproto.appPass"
    create_error = None

    def __init__(self, base_url=None, *a, **k):
        from types import SimpleNamespace

        self.base_url, self.sessions, self.imported = base_url, [], None
        self._base_url = f"{base_url}/xrpc" if base_url else None
        self._session = None
        self.server_error = None
        self.deleted = []
        server = SimpleNamespace(get_session=self._get_session, delete_session=lambda **kw: self.deleted.append(kw))
        self._server = server
        _LoginClient.instances.append(self)
        self.com = type("C", (), {"atproto": type("A", (), {"server": server})()})()
        actor = type("Act", (), {"get_profile": self._profile})()
        self.app = type("App", (), {"bsky": type("B", (), {"actor": actor})()})()

    def on_session_change(self, cb):
        pass

    def _profile(self, *a, **k):
        raise AssertionError("getProfile (AppView) must not be called")

    def login(self, *a, **k):
        raise AssertionError("Client.login must not be called")

    def _session_for(self, pds):
        from types import SimpleNamespace

        self._session = SimpleNamespace(pds_endpoint=pds, access_jwt_payload=SimpleNamespace(scope=_LoginClient.scope))

    def _get_refresh_auth_headers(self):
        return {"Authorization": "Bearer refresh"}

    def _get_and_set_session(self, login, password):
        from types import SimpleNamespace

        self.sessions.append((login, password))
        if _LoginClient.create_error:
            raise _LoginClient.create_error
        self._session_for(self.base_url)
        return SimpleNamespace(did=ALICE, handle="alice.example.com", **_LoginClient.session_info)

    def _import_session_string(self, s):
        self.imported = s
        self._session_for(_LoginClient.session_pds)

    def _get_session(self):
        from types import SimpleNamespace

        if _LoginClient.server_error:
            raise _LoginClient.server_error
        return SimpleNamespace(did=ALICE, handle="handle.invalid", **_LoginClient.session_info)


@pytest.fixture
def login_env(monkeypatch):
    from types import SimpleNamespace

    from consentvid import identity

    _LoginClient.instances = []
    _LoginClient.server_error = None
    _LoginClient.create_error = None
    _LoginClient.session_info = {}
    _LoginClient.session_pds = "https://pds.alice.example"
    _LoginClient.scope = "com.atproto.appPass"
    state = SimpleNamespace(handle="alice.example.com", resolved=[])
    monkeypatch.setattr(_core, "Client", _LoginClient)
    monkeypatch.delenv("CONSENTVID_SERVICE", raising=False)

    def resolve_did(h):
        state.resolved.append(h)
        return ALICE

    monkeypatch.setattr(identity, "resolve_did", resolve_did)
    monkeypatch.setattr(
        identity,
        "resolve_identity",
        lambda did: SimpleNamespace(did=did, pds="https://pds.alice.example", signing_key="k",
                                    claimed_handle=state.handle),
    )
    # R3-08: login proves handle -> DID once and checks the DID document's
    # claim; it never repeats the handle lookup.
    monkeypatch.setattr(identity, "verified_handle", lambda did, **kw: pytest.fail("second handle lookup"))
    return state


def test_login_signs_in_at_the_resolved_pds(login_env):
    client = _core._session_client("@Alice.Example.com", "pw")
    (c,) = _LoginClient.instances
    assert c.base_url == "https://pds.alice.example" and c.sessions == [(ALICE, "pw")]
    assert client.me.did == ALICE and client.me.handle == "alice.example.com"


def test_login_refuses_an_unverified_handle_before_sending_the_password(login_env):
    from consentvid.errors import ConfigError

    login_env.handle = None  # the DID document doesn't claim this handle back
    with pytest.raises(ConfigError, match="doesn't verify"):
        _core._session_client("alice.example.com", "pw")
    assert all(not c.sessions for c in _LoginClient.instances)


def test_cached_session_network_error_does_not_create_a_session(login_env, monkeypatch):
    from atproto_client.exceptions import NetworkError

    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("session")
    _LoginClient.server_error = NetworkError()
    with pytest.raises(ConsentVidError, match="could not check the cached session"):
        _core.get_client()
    assert all(not c.sessions for c in _LoginClient.instances)


def test_cached_session_shows_handle_invalid_as_the_did(login_env):
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("session")
    client = _core.get_client()
    assert client.me.handle == ALICE and _LoginClient.instances[0].imported == "session"


def test_expired_cached_session_falls_back_to_credentials(login_env, monkeypatch):
    from atproto_client.exceptions import BadRequestError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("session")
    _LoginClient.server_error = BadRequestError(
        Response(success=False, status_code=400, content=XrpcError(error="ExpiredToken", message="x"), headers={})
    )
    client = _core.get_client()
    assert client.me.did == ALICE
    assert _LoginClient.instances[-1].sessions == [(ALICE, "pw")]


# --------------- R2-09 / R2-10: session state, migration and credential type


def _cached_session():
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("session")


def test_cached_session_of_a_deactivated_account_is_refused(login_env, fake_client, tmp_path, monkeypatch):
    from consentvid.errors import ConfigError

    _cached_session()
    _LoginClient.session_info = {"active": False, "status": "deactivated"}
    with pytest.raises(ConfigError, match="is deactivated; if you migrated PDS, run `consentvid login` again"):
        _core.get_client()
    # contribute stops there, before anything is uploaded
    uploads = []
    from consentvid import storage

    monkeypatch.setattr(storage, "upload", lambda *a, **k: uploads.append(a))
    _cached_session()
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    r = run("contribute", str(f), "--title", "t", "--attest-authorship", "--attest-adult", "--people", "onlyMe",
            "--modality", "video", "--purpose", "training", "--model-class", "academic", "--keep-metadata",
            "--storage-kind", "r2")
    assert r.exit_code != 0 and "deactivated" in r.output and uploads == []


def test_cached_session_on_the_old_pds_signs_in_again(login_env, monkeypatch):
    _cached_session()
    _LoginClient.session_pds = "https://old-pds.example"  # the DID document now names pds.alice.example
    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    r = CliRunner().invoke(cli.main, ["whoami"])
    assert r.exit_code == 0, r.output
    assert "your account moved to https://pds.alice.example; signing in again" in r.output
    assert _LoginClient.instances[-1].base_url == "https://pds.alice.example"
    assert _LoginClient.instances[-1].sessions == [(ALICE, "pw")]


def test_cached_session_on_the_old_pds_without_credentials_asks_for_login(login_env):
    from consentvid.errors import ConfigError

    _cached_session()
    _LoginClient.session_pds = "https://old-pds.example"
    with pytest.raises(ConfigError, match="not logged in"):
        _core.get_client()
    assert not _core.SESSION_PATH.exists()


def test_matching_active_session_is_used_unchanged(login_env):
    _cached_session()
    _LoginClient.session_info = {"active": True}
    client = _core.get_client()
    assert client.me.did == ALICE and _core.SESSION_PATH.exists() and not client.sessions


def test_identity_failure_is_not_a_migration(login_env, monkeypatch):
    from consentvid import identity

    _cached_session()

    def down(did):
        raise OSError("plc.directory unreachable")

    monkeypatch.setattr(identity, "resolve_identity", down)
    with pytest.raises(ConsentVidError, match="could not resolve"):
        _core.get_client()
    assert _core.SESSION_PATH.exists()


@pytest.mark.parametrize(
    "scope, warning",
    [
        ("com.atproto.access", "MAIN account password"),
        ("com.atproto.appPassPrivileged", "non-privileged"),
        ("com.atproto.appPass", None),
        ("some.other.pds.scope", None),
    ],
)
def test_login_flags_the_credential_type(login_env, scope, warning):
    _LoginClient.scope = scope
    r = CliRunner().invoke(cli.main, ["login"], input="alice.example.com\npw\n")
    assert r.exit_code == 0, r.output
    if warning:
        assert warning in r.output
    else:
        assert "warning" not in r.output


def test_login_refuses_a_taken_down_account(login_env):
    from consentvid.errors import ConfigError

    _LoginClient.scope = "com.atproto.takendown"
    with pytest.raises(ConfigError, match="taken down"):
        _core._session_client("alice.example.com", "pw")
    assert _LoginClient.instances[-1].deleted and not _core.SESSION_PATH.exists()


def test_two_factor_error_is_explained(login_env):
    from atproto_client.exceptions import UnauthorizedError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    from consentvid.errors import ConfigError

    _LoginClient.create_error = UnauthorizedError(
        Response(success=False, status_code=401, content=XrpcError(error="AuthFactorTokenRequired", message="x"),
                 headers={})
    )
    with pytest.raises(ConfigError, match="email code .* use an app password"):
        _core._session_client("alice.example.com", "pw")


# ------------------------- R1-16: only the lexicon authority publishes, compatibly


@pytest.fixture
def lexicon_env(fake_client, monkeypatch):
    """The authority's published schemas live in fake_client's repo (so the
    compare-and-swap is enforced by FakeRepo); DNS names ``authority``."""
    from types import SimpleNamespace

    from consentvid import identity
    from consentvid import lexicons as lx

    state = SimpleNamespace(authority=ALICE)

    def publish(nsid, doc, cid="bafyold"):
        fake_client.repo.records[(lx.SCHEMA_COLLECTION, nsid)] = (cid, {**doc, "$type": lx.SCHEMA_COLLECTION})

    state.publish = publish
    pub = SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(repo=fake_client.repo)))
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    monkeypatch.setattr(lx, "resolve_did_via_dns", lambda nsid, strict=False: state.authority)
    monkeypatch.setattr(identity, "public_client", lambda did: pub)
    return state


def _writes(fake_client):
    return [(op, p) for op, p in fake_client.repo.calls if op in ("put", "create")]


def test_publish_refuses_a_non_authority(fake_client, lexicon_env):
    lexicon_env.authority = BOB
    r = run("publish-lexicons")
    assert r.exit_code != 0 and "not the lexicon authority" in r.output and not _writes(fake_client)


def test_publish_without_dns_needs_bootstrap(fake_client, lexicon_env):
    lexicon_env.authority = None
    r = run("publish-lexicons")
    assert r.exit_code != 0 and "--bootstrap" in r.output and not _writes(fake_client)
    r = run("publish-lexicons", "--bootstrap")
    assert r.exit_code == 0, r.output
    writes = _writes(fake_client)
    # R2-02: a first publication is a createRecord under rkey=nsid.
    assert [op for op, _ in writes] == ["create"] * 4
    assert sorted(p["rkey"] for _, p in writes) == [
        "org.consentvid.consent", "org.consentvid.consentV2", "org.consentvid.video", "org.consentvid.videoV2"
    ]
    assert all("validate" not in p for _, p in writes)


def test_publish_refuses_breaking_changes(fake_client, lexicon_env):
    from consentvid import lexicons as lx

    old = lx.load_lexicon("org.consentvid.consent")
    old["defs"]["main"]["record"]["properties"]["compensation"]["knownValues"].append("barter")
    lexicon_env.publish("org.consentvid.consent", old)
    r = run("publish-lexicons", "--nsid", "org.consentvid.consent")
    assert r.exit_code != 0 and "'barter' removed" in r.output and not _writes(fake_client)
    r = run("publish-lexicons", "--nsid", "org.consentvid.consent", "--allow-breaking", "--skip-pds-validation")
    assert r.exit_code == 0, r.output
    ((op, p),) = _writes(fake_client)
    assert op == "put" and p["validate"] is False and p["rkey"] == "org.consentvid.consent"
    assert p["swapRecord"] == "bafyold"  # R2-02: compare-and-swap on the existing CID


def test_bootstrap_still_checks_an_existing_schema(fake_client, lexicon_env):
    """R2-02: --bootstrap (no DNS yet) must not skip the evolution check."""
    from consentvid import lexicons as lx

    lexicon_env.authority = None
    old = lx.load_lexicon("org.consentvid.consent")
    old["defs"]["main"]["record"]["required"].remove("compensation")  # we'd add it: breaking
    lexicon_env.publish("org.consentvid.consent", old)
    r = run("publish-lexicons", "--nsid", "org.consentvid.consent", "--bootstrap")
    assert r.exit_code != 0 and "'compensation' added to required" in r.output and not _writes(fake_client)
    r = run("publish-lexicons", "--nsid", "org.consentvid.consent", "--bootstrap", "--allow-breaking")
    assert r.exit_code == 0, r.output
    ((op, p),) = _writes(fake_client)
    assert op == "put" and p["swapRecord"] == "bafyold"


def test_publish_reports_a_concurrent_change(fake_client, lexicon_env):
    import json as _json

    nsid = "org.consentvid.video"
    lexicon_env.publish(nsid, _json.loads((ROOT / "tests/fixtures/published_lexicons" / f"{nsid}.json").read_text()))
    real_put = fake_client.repo.put_record

    def put_after_someone_else(p):
        lexicon_env.publish(nsid, {**fake_client.repo.records[(p["collection"], p["rkey"])][1]}, cid="bafynewer")
        return real_put(p)

    fake_client.repo.put_record = put_after_someone_else
    r = run("publish-lexicons", "--nsid", nsid)
    assert r.exit_code != 0 and "changed while you were publishing; re-run" in r.output
    fake_client.repo.put_record = real_put
    fake_client.repo.records.pop(("com.atproto.lexicon.schema", nsid))
    real_create = fake_client.repo.create_record

    def create_after_someone_else(p):
        lexicon_env.publish(nsid, {"id": nsid})
        return real_create(p)

    fake_client.repo.create_record = create_after_someone_else
    r = run("publish-lexicons", "--nsid", nsid)
    assert r.exit_code != 0 and "changed while you were publishing; re-run" in r.output


def test_publish_compatible_change_is_allowed(fake_client, lexicon_env):
    import json as _json

    for nsid in ("org.consentvid.video", "org.consentvid.consent"):
        lexicon_env.publish(nsid, _json.loads((ROOT / "tests/fixtures/published_lexicons" / f"{nsid}.json").read_text()))
    r = run("publish-lexicons")
    assert r.exit_code == 0, r.output
    # The published V1 schemas are updated; the new V2 NSIDs have nothing to
    # be compatible with and are created.
    ops = {p["rkey"]: op for op, p in _writes(fake_client)}
    assert ops == {
        "org.consentvid.video": "put", "org.consentvid.consent": "put",
        "org.consentvid.videoV2": "create", "org.consentvid.consentV2": "create",
    }


def test_dns_failure_is_not_treated_as_no_record(fake_client, monkeypatch):
    """R2-02: a timeout must not lead to --bootstrap (and a clobber)."""
    import dns.exception
    import dns.resolver

    from consentvid import lexicons as lx
    from consentvid.errors import LexiconDnsError

    def timeout(*a, **k):
        raise dns.exception.Timeout()

    monkeypatch.setattr(dns.resolver, "resolve", timeout)
    with pytest.raises(LexiconDnsError, match="Timeout"):
        lx.resolve_did_via_dns("org.consentvid.video")
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    r = run("publish-lexicons")
    assert r.exit_code != 0 and "DNS lookup" in r.output and "--bootstrap" not in r.output
    assert not fake_client.repo.calls

    def nxdomain(*a, **k):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", nxdomain)
    assert lx.resolve_did_via_dns("org.consentvid.video") is None


def test_doctor_shows_dns_errors(monkeypatch):
    import dns.exception
    import dns.resolver

    def timeout(*a, **k):
        raise dns.exception.Timeout()

    monkeypatch.setattr(dns.resolver, "resolve", timeout)
    monkeypatch.setattr(_core, "get_client", lambda: (_ for _ in ()).throw(ConsentVidError("not logged in")))
    r = run("doctor")
    assert "lexicon org.consentvid.video: DNS error:" in r.output
    assert "no _lexicon DNS record" not in r.output


# ------------------------- R2-28: resolve-lexicon verifies the schema record


@pytest.mark.parametrize(
    "value, ok",
    [
        ("good", True),
        ({"$type": "com.atproto.lexicon.schema", "id": "org.consentvid.other"}, False),
        ({"$type": "app.bsky.feed.post", "id": "org.consentvid.video"}, False),
        (None, False),
    ],
)
def test_resolve_lexicon_verifies_type_and_id(monkeypatch, value, ok):
    from consentvid import lexicons as lx
    from consentvid import verify
    from consentvid.verify import VerifiedRecord

    nsid = "org.consentvid.video"
    if value == "good":
        value = lx.build_schema_record(lx.load_lexicon(nsid))
    uri = f"at://{ALICE}/{lx.SCHEMA_COLLECTION}/{nsid}"
    fetched = []

    def fetch(u):
        fetched.append(u)
        return VerifiedRecord(u, value is not None, "bafy" if value else None, value, "3maaaaaaaaaa2", True)

    monkeypatch.setattr(verify, "fetch_verified", fetch)
    r = run("resolve-lexicon", nsid, "--did", ALICE)
    assert fetched == [uri]
    if ok:
        assert r.exit_code == 0, r.output
        assert "verified (commit signature + MST proof)" in r.output and "matches the local lexicon" in r.output
    else:
        assert r.exit_code != 0 and ("not com.atproto.lexicon.schema" in r.output or "not org.consentvid.video" in r.output
                                     or "does not exist" in r.output)


# ------------------------- R2-17: --validate is flagged before any upload


@pytest.mark.parametrize("how", ["flag", "env"])
def test_strict_validation_is_refused_before_uploading(fake_client, tmp_path, monkeypatch, how):
    from consentvid import storage

    uploads = []
    monkeypatch.setattr(storage, "upload", lambda *a, **k: uploads.append(a))
    monkeypatch.setattr(_core, "get_client", lambda: fake_client)
    monkeypatch.setattr(_core, "_prepare_bytes", lambda *a, **k: pytest.fail("prepared bytes"))
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    args = ["contribute", str(f), "--title", "t", "--attest-authorship", "--attest-adult", "--people", "onlyMe",
            "--modality", "video", "--purpose", "training", "--model-class", "academic", "--storage-kind", "r2"]
    if how == "flag":
        args.append("--validate")
    else:
        monkeypatch.setenv("CONSENTVID_VALIDATE", "1")
    r = run(*args)
    assert r.exit_code != 0 and "likely be rejected by your PDS after the upload" in r.output
    assert "not uploading" in r.output and uploads == [] and not fake_client.repo.calls


def test_without_a_reporter_messages_go_to_the_logger(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="consentvid"):
        _core._info("hello")
        _core._warn("careful")
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("INFO", "hello"), ("WARNING", "careful")]


def test_validation_status_is_printed(fake_client, capsys):
    from types import SimpleNamespace

    _core.set_reporter(cli._click_reporter)  # as when the CLI runs
    _core._note_validation(SimpleNamespace(validation_status="unknown"), SimpleNamespace())
    assert capsys.readouterr().out == "  PDS validation: unknown\n"


def test_login_needs_no_second_handle_lookup(login_env):
    """R3-08: handle -> DID is proven by resolve_did, the back-link by the
    DID document's claim; verified_handle (patched to fail) is never called,
    so a transient failure of a repeated lookup can't refuse the login."""
    client = _core._session_client("alice.example.com", "pw")
    assert client.me.did == ALICE and login_env.resolved == ["alice.example.com"]


# ------------------- R3-06: a write that commits but whose response is lost


def _timeout():
    from atproto_client.exceptions import InvokeTimeoutError

    return InvokeTimeoutError()


def test_contribute_that_commits_then_times_out_succeeds(fake_client, tmp_path, monkeypatch):
    real = fake_client.repo.apply_writes

    def commit_then_timeout(p):
        real(p)
        raise _timeout()

    fake_client.repo.apply_writes = commit_then_timeout
    deleted = []
    r = _contribute(fake_client, tmp_path, monkeypatch, deleted)
    assert r.exit_code == 0, r.output
    assert "committed the write although the response was lost" in r.output
    assert deleted == [] and len(fake_client.repo.records) == 2
    assert "Created consent receipt" in r.output


def _register_upload(fake_client, tmp_path, monkeypatch, deleted):
    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, h: deleted.append(ref) or "ok")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    return f


def test_register_uses_a_client_chosen_record_key(fake_client, tmp_path, monkeypatch):
    from consentvid.records import is_tid

    f = _register_upload(fake_client, tmp_path, monkeypatch, [])
    uri, _, _ = _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    ((op, params),) = [c for c in fake_client.repo.calls if c[0] == "create"]
    assert is_tid(params["rkey"]) and uri.endswith("/" + params["rkey"])


def test_register_that_commits_then_times_out_succeeds(fake_client, tmp_path, monkeypatch):
    from consentvid.records import compute_cid

    deleted = []
    f = _register_upload(fake_client, tmp_path, monkeypatch, deleted)
    real = fake_client.repo.create_record

    def commit_then_timeout(p):
        real(p)
        key = (p["collection"], p["rkey"])
        fake_client.repo.records[key] = (compute_cid(p["record"]), p["record"])  # the PDS's real CID
        raise _timeout()

    fake_client.repo.create_record = commit_then_timeout
    uri, cid, rec = _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    from consentvid.versions import video_record_v2

    assert cid == compute_cid(video_record_v2(rec)) and deleted == []
    assert uri.startswith(f"at://{ALICE}/org.consentvid.videoV2/")


def test_timeout_with_nothing_written_cleans_up_as_before(fake_client, tmp_path, monkeypatch):
    from atproto_client.exceptions import InvokeTimeoutError

    deleted = []
    f = _register_upload(fake_client, tmp_path, monkeypatch, deleted)

    def timeout(p):
        raise _timeout()

    fake_client.repo.create_record = timeout
    with pytest.raises(InvokeTimeoutError):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted and deleted[0]["value"] == R2_URL


@pytest.mark.parametrize("check", ["fails", "other-cid"])
def test_unknown_outcome_keeps_the_bytes(fake_client, tmp_path, monkeypatch, check):
    deleted = []
    f = _register_upload(fake_client, tmp_path, monkeypatch, deleted)
    real = fake_client.repo.create_record

    def commit_then_timeout(p):
        if check == "other-cid":
            real(p)  # stored under a fake CID: not what we wrote
        raise _timeout()

    fake_client.repo.create_record = commit_then_timeout
    if check == "fails":
        def broken(p):
            raise OSError("PDS unreachable")

        fake_client.repo.get_record = broken
    with pytest.raises(ConsentVidError, match="the outcome of the write is unknown; run `consentvid list`"):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted == []


def test_a_definite_refusal_is_not_looked_up(fake_client, tmp_path, monkeypatch):
    from atproto_client.exceptions import BadRequestError
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    deleted = []
    f = _register_upload(fake_client, tmp_path, monkeypatch, deleted)
    err = BadRequestError(Response(success=False, status_code=400, content=XrpcError(error="InvalidRequest", message="x"), headers={}))

    def refuse(p):
        raise err

    fake_client.repo.create_record = refuse
    fake_client.repo.get_record = lambda p: pytest.fail("looked up a refused write")
    with pytest.raises(BadRequestError):
        _core._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted


def test_new_client_waits_long_enough_for_a_commit():
    client = _core._new_client("https://pds.example.com")
    timeout = client.request._client.timeout
    assert timeout.read == 60 and timeout.write == 60 and timeout.connect == 10


# ------------------- R3-13: a new login ends the session it replaces


def test_login_ends_the_previous_session_before_creating_one(login_env, monkeypatch):
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("old-session")
    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    r = CliRunner().invoke(cli.main, ["login"])
    assert r.exit_code == 0, r.output
    enders = [i for i, c in enumerate(_LoginClient.instances) if c.imported == "old-session" and c.deleted]
    creators = [i for i, c in enumerate(_LoginClient.instances) if c.sessions]
    assert enders and creators and enders[0] < creators[0]
    ender = _LoginClient.instances[enders[0]]
    # deleteSession with the old session's REFRESH token, sent to its own PDS.
    assert ender.deleted == [{"headers": {"Authorization": "Bearer refresh"}, "ignore_session_check": True}]
    assert ender._session.pds_endpoint == _LoginClient.session_pds
    assert "Ended the previous session" in r.output


def test_a_failing_delete_session_does_not_block_login(login_env, monkeypatch):
    _core.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _core.SESSION_PATH.write_text("old-session")
    failing = _SessionClient(fail=True)
    monkeypatch.setattr(_core, "_throwaway_client", lambda: failing)
    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    r = CliRunner().invoke(cli.main, ["login"])
    assert r.exit_code == 0, r.output
    assert [c[0] for c in failing.calls] == ["import", "delete_session"]
    assert "Logged in as" in r.output


def test_moved_pds_relogin_ends_the_old_session(login_env, monkeypatch):
    enders = []
    monkeypatch.setattr(_core, "_end_cached_session", lambda **kw: enders.append(_core.SESSION_PATH.read_text()) or True)
    _cached_session()
    _LoginClient.session_pds = "https://old-pds.example"
    monkeypatch.setenv("CONSENTVID_HANDLE", "alice.example.com")
    monkeypatch.setenv("CONSENTVID_APP_PASSWORD", "pw")
    r = CliRunner().invoke(cli.main, ["whoami"])
    assert r.exit_code == 0, r.output
    assert enders == ["session"]  # the cached (old-PDS) session, before the file is removed


def test_deactivated_session_is_ended_before_it_is_dropped(login_env, monkeypatch):
    from consentvid.errors import ConfigError

    enders = []
    monkeypatch.setattr(_core, "_end_cached_session", lambda **kw: enders.append(_core.SESSION_PATH.exists()) or True)
    _cached_session()
    _LoginClient.session_info = {"active": False, "status": "deactivated"}
    with pytest.raises(ConfigError):
        _core.get_client()
    assert enders == [True] and not _core.SESSION_PATH.exists()


@pytest.mark.parametrize("answer, code, text", [
    ((False, "throttled"), 0, "note: relay reports throttled"),
    ((False, "takendown"), 1, "UNAVAILABLE: the relay reports the account takendown"),
])
def test_verify_command_separates_relay_processing_from_account_status(monkeypatch, answer, code, text):
    """R3-01 for `consentvid verify`."""
    from conftest import consent_record
    from consentvid import resync
    from consentvid import verify as vf
    from consentvid.verify import VerifiedRecord

    monkeypatch.setattr(vf, "fetch_verified", lambda at: VerifiedRecord(str(at), True, "bafyc", consent_record(), "3maaaaaaaaaa2", True))
    monkeypatch.setattr(resync, "_relay_lookup", lambda did: answer)
    resync.clear_relay_cache()
    r = run("verify", f"at://{ALICE}/org.consentvid.consent/{K1}")
    assert r.exit_code == code, r.output
    assert text in r.output
