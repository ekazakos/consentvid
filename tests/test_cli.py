import json
import shutil
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from conftest import ALICE, BOB, VIDEO_CID, video_record
from consentvid import cli
from consentvid.errors import ConsentVidError

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


def run(*args):
    return CliRunner().invoke(cli.main, list(args))


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
        assert cli._parse_expiry(value).endswith("Z")
    else:
        with pytest.raises(click.BadParameter):
            cli._parse_expiry(value)


def _add_video(fake_client, rkey="v1", **kw):
    fake_client.repo.records[("org.consentvid.video", rkey)] = (VIDEO_CID, video_record(**kw))
    return f"at://{ALICE}/org.consentvid.video/{rkey}"


def test_attach_pins_video_version_and_is_one_per_video(fake_client):
    vuri = _add_video(fake_client)
    uri, cid, rec, replaced = cli._attach(fake_client, vuri, replace=False, validate=None, **TERMS)
    assert rec["videoCid"] == VIDEO_CID and rec["revocable"] is True and not replaced
    assert "validate" not in fake_client.repo.calls[-1][1]  # PDS default policy
    with pytest.raises(ConsentVidError, match="already has consent"):
        cli._attach(fake_client, vuri, replace=False, validate=None, **TERMS)
    uri2, _, _, replaced = cli._attach(
        fake_client, vuri, replace=True, validate=None, **{**TERMS, "purposes": ("evaluation",)}
    )
    assert replaced and uri2 == uri  # same record, new version


def test_attach_refuses_other_repo(fake_client):
    with pytest.raises(ConsentVidError, match="not in your repo"):
        cli._attach(fake_client, f"at://{BOB}/org.consentvid.video/v1", replace=False, validate=None, **TERMS)


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
    uri, _, rec = cli._register(fake_client, f, storage_kind="local", **opts)
    assert rec["storageRef"]["value"] == rec["contentHash"] + ".mp4" and rec["mimeType"] == "video/mp4"
    with pytest.raises(ConsentVidError, match="already registered"):
        cli._register(fake_client, f, storage_kind="local", **opts)


@pytest.mark.parametrize("kind,value", [("local", None), ("url", "https://example.org/v.mp4")])
def test_every_kind_needs_ffmpeg_unless_keep_metadata(fake_client, tmp_path, monkeypatch, kind, value):
    """R2-06: pointer and local kinds are sanitised too, so no ffmpeg = refuse."""
    from consentvid.errors import ConfigError

    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video bytes")
    with pytest.raises(ConfigError, match="ffmpeg is required"):
        cli._register(fake_client, f, storage_kind=kind, **{**VOPTS, "storage_value": value})
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
    _, _, rec = cli._register(fake_client, f, storage_kind="local", **{**VOPTS, "keep_metadata": True})
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
    with pytest.raises(ConsentVidError, match="maxLength"):
        cli._register(fake_client, f, storage_kind="r2", **{**VOPTS, "title": "x" * 400})
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
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
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
    uri, _, rec = cli._register(fake_client, f, storage_kind="local", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="ipfs", **{**VOPTS, "storage_value": cid})
    assert (tmp_path / "clip.clean.mp4").exists() and not fake_client.repo.records


def test_local_value_names_the_hashed_file(fake_client, tmp_path, monkeypatch):
    from consentvid.media import Probe

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe(sensitive_tags={"format:make": "x"}))
    monkeypatch.setattr("consentvid.media.sanitise", lambda src, dst: (dst.write_bytes(b"clean"), Probe())[1])
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig")
    _, _, rec = cli._register(fake_client, f, storage_kind="local", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="r2", **{**VOPTS, "allow_duplicate": True})
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
    monkeypatch.setattr(cli, "_checkout_env", lambda: None)
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
    cli._apply_env_files()
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
    cli._apply_env_files()
    assert "SSL_CERT_FILE" not in os.environ and "HTTPS_PROXY" not in os.environ
    assert "CONSENTVID_ALLOW_INSECURE" not in os.environ
    assert os.environ["CONSENTVID_HOME"] != "/tmp/x"
    assert os.environ["CONSENTVID_STORAGE"] == "local"


def test_world_readable_env_file_warns(env_sandbox, capsys):
    cfg = env_sandbox / "config.env"
    cfg.write_text("CONSENTVID_HANDLE=me.test\n")
    cfg.chmod(0o644)
    cli._apply_env_files()
    assert "chmod 600" in capsys.readouterr().err
    cfg.chmod(0o600)
    cli._apply_env_files()
    assert "chmod 600" not in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://evil.example", "ftp://x", "https://u:p@evil.example"])
def test_service_must_be_https(monkeypatch, url):
    from consentvid.errors import ConfigError

    monkeypatch.setenv("CONSENTVID_SERVICE", url)
    with pytest.raises(ConfigError):
        cli._new_client()


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
    _, _, rec = cli._register(fake_client, f, storage_kind="r2", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert uploads == []
    cli._register(fake_client, f, storage_kind="r2", **{**VOPTS, "keep_metadata": True})
    assert len(uploads) == 1


def test_storage_value_is_validated_per_kind(fake_client, tmp_path, monkeypatch):
    _fake_media(monkeypatch)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    for kind, value in (("r2", "/Users/me/clip.mp4"), ("ipfs", "../ipns/x"), ("peertube", "http://pt/w/x")):
        with pytest.raises(click.BadParameter):
            cli._register(fake_client, f, storage_kind=kind, **{**VOPTS, "storage_value": value})
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
    other = _add_video(fake_client, "v2", storageRef=theirs, contentHash="sha256-" + "cd" * 32)
    assert cli._bytes_shared(fake_client, ALICE, mine, "sha256-" + "ef" * 32) == other


def test_bytes_shared_by_content_hash(fake_client):
    h = "sha256-" + "cd" * 32
    other = _add_video(fake_client, "v2", contentHash=h, storageRef={"kind": "url", "value": "https://a/b"})
    assert cli._bytes_shared(fake_client, ALICE, {"kind": "r2", "value": "https://x/y"}, h) == other
    assert cli._bytes_shared(fake_client, ALICE, {"kind": "r2", "value": "https://x/y"}, h, exclude_uri=other) is None


def test_revoke_delete_bytes_passes_the_content_hash(fake_client, monkeypatch):
    h = "sha256-" + "ab" * 32
    vuri = _add_video(fake_client, contentHash=h)
    monkeypatch.setattr(cli, "get_client", lambda: fake_client)
    got = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref, ch: got.append((ref, ch)) or "deleted")
    r = run("revoke", vuri, "--delete-bytes", "-y")
    assert r.exit_code == 0, r.output
    assert got == [(video_record(contentHash=h)["storageRef"], h)]


def test_revoke_delete_bytes_failure_says_bytes_may_be_public(fake_client, monkeypatch):
    """R3-11: the records are gone; a failed byte delete must be loud."""
    import httpx

    vuri = _add_video(fake_client, contentHash="sha256-" + "ab" * 32)
    monkeypatch.setattr(cli, "get_client", lambda: fake_client)

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
        cli._register(fake_client, f, storage_kind="ipfs", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="ipfs", **VOPTS)
    assert "/api/v0/pin/rm" not in seen and seen.count("/api/v0/add") == 2


# ------------------------------------ R1-20: revoking one of several consents


def test_revoke_warns_when_another_consent_still_governs(fake_client, monkeypatch):
    from conftest import consent_record

    _add_video(fake_client)
    fake_client.repo.records[("org.consentvid.consent", "a")] = ("bafya", consent_record())
    fake_client.repo.records[("org.consentvid.consent", "b")] = ("bafyb", consent_record())
    monkeypatch.setattr(cli, "get_client", lambda: fake_client)
    b = f"at://{ALICE}/org.consentvid.consent/b"
    r = run("revoke", b, "-y")
    assert r.exit_code == 0 and "now governs" in r.output and "org.consentvid.consent/a" in r.output, r.output
    assert ("org.consentvid.consent", "a") in fake_client.repo.records
    fake_client.repo.records[("org.consentvid.consent", "b")] = ("bafyb", consent_record())
    r = run("revoke", b, "-y", "--all")
    assert r.exit_code == 0, r.output
    assert not [k for k in fake_client.repo.records if k[0] == "org.consentvid.consent"]


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
    monkeypatch.setattr(cli, "_new_client", lambda: fake)
    cli.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    cli.SESSION_PATH.write_text("handle:::did:::access:::refresh:::https://pds")
    r = run("logout")
    assert r.exit_code == 0, r.output
    assert not cli.SESSION_PATH.exists()
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
        cli._register(fake_client, f, storage_kind="url", **opts)
    clean = tmp_path / "clip.clean.mp4"
    assert clean.read_bytes() == b"clean" and not fake_client.repo.records
    # Re-running on the clean copy is accepted: it is the fixed point.
    _, _, rec = cli._register(fake_client, clean, storage_kind="url", **opts)
    assert rec["contentHash"] == cli.hash_file(clean)


def test_local_commits_to_the_clean_copy_even_when_probe_sees_nothing(fake_client, tmp_path, monkeypatch):
    _fixed_point_media(monkeypatch)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig with a vendor box")
    _, _, rec = cli._register(fake_client, f, storage_kind="local", **VOPTS)
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
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert len(n) == cli._SANITISE_PASSES and not fake_client.repo.records


# --------------------------------------- R2-02: export, invalid newest


def test_export_omits_a_video_whose_newest_consent_is_invalid(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from conftest import consent_record
    from consentvid import identity

    V, C = "org.consentvid.video", "org.consentvid.consent"
    vuri = f"at://{ALICE}/{V}/v1"
    older = consent_record(modelClasses=["academic", "commercial"])
    newer = consent_record(createdAt="2026-06-01T00:00:00Z", authorshipAttested=False)
    listing = {
        V: [SimpleNamespace(uri=vuri, cid=VIDEO_CID, value=video_record())],
        C: [
            SimpleNamespace(uri=f"at://{ALICE}/{C}/old", cid="bafyold", value=older),
            SimpleNamespace(uri=f"at://{ALICE}/{C}/new", cid="bafynew", value=newer),
        ],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(identity, "display_name", lambda did: "alice.test")
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: listing[coll])
    out = tmp_path / "m.json"
    r = run("export", str(out), "--repo", ALICE)
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["distribution"] == []
    assert "is invalid" in r.output and "org.consentvid.consent/new" in r.output
    # Valid newest: exported, and the older one is ignored.
    listing[C][1].value = consent_record(createdAt="2026-06-01T00:00:00Z")
    r = run("export", str(out), "--repo", ALICE)
    (fo,) = json.loads(out.read_text())["distribution"]
    assert fo["cv:consentReceipt"]["atUri"].endswith("/new")


# ------------------------------------ R2-16: private files from the start


def test_session_file_is_written_0600(tmp_path, monkeypatch):
    import os
    import stat

    old_umask = os.umask(0o022)
    try:
        client = cli._new_client()
        monkeypatch.setattr(client, "export_session_string", lambda: "secret-session")
        (save,) = client._session_dispatcher._on_session_change_callbacks
        save(None, None)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(cli.SESSION_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(cli.CONFIG_DIR.stat().st_mode) == 0o700
    assert cli.SESSION_PATH.read_text() == "secret-session"


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
    av.upsert_video(conn, f"at://{ALICE}/org.consentvid.video/v1", ALICE, "v1", VIDEO_CID, video_record(title=evil_title))
    # Stored as an older index would have it (validation now rejects these terms).
    av.upsert_consent(
        conn, f"at://{ALICE}/org.consentvid.consent/c1", ALICE, "c1", "bafyc",
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
    with pytest.raises(ConsentVidError, match="storageRef"):
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert uploads == [] and not fake_client.repo.records
