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
    modalities=(),
    purposes=("training",),
    model_classes=(),
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
    uri, _, rec = cli._register(fake_client, f, storage_kind="local", **VOPTS)
    assert rec["storageRef"]["value"] == "clip.mp4" and rec["mimeType"] == "video/mp4"
    with pytest.raises(ConsentVidError, match="already registered"):
        cli._register(fake_client, f, storage_kind="local", **VOPTS)


def test_register_validates_before_upload(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    uploads = []
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: uploads.append(a) or "https://x")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(ConsentVidError, match="maxLength"):
        cli._register(fake_client, f, storage_kind="r2", **{**VOPTS, "title": "x" * 400})
    assert uploads == []


def test_register_cleans_up_bytes_if_record_write_fails(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: "https://r2.example/videos/x")
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref: deleted.append(ref) or "ok")

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        cli._register(fake_client, f, storage_kind="r2", **VOPTS)
    assert deleted and deleted[0]["value"] == "https://r2.example/videos/x"


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
    with pytest.raises(ConsentVidError, match="clean copy was written"):
        cli._register(fake_client, f, storage_kind="ipfs", **{**VOPTS, "storage_value": "bafyX"})
    assert (tmp_path / "clip.clean.mp4").exists() and not fake_client.repo.records


def test_local_value_names_the_hashed_file(fake_client, tmp_path, monkeypatch):
    from consentvid.media import Probe

    monkeypatch.setattr("consentvid.media.probe", lambda p: Probe(sensitive_tags={"format:make": "x"}))
    monkeypatch.setattr("consentvid.media.sanitise", lambda src, dst: (dst.write_bytes(b"clean"), Probe())[1])
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"orig")
    _, _, rec = cli._register(fake_client, f, storage_kind="local", **VOPTS)
    assert rec["storageRef"]["value"] == "clip.clean.mp4"
    assert rec["contentHash"] == cli.hash_file(tmp_path / "clip.clean.mp4")


def test_shared_bytes_not_deleted_on_failed_write(fake_client, tmp_path, monkeypatch):
    """Duplicates share a content-addressed object (review finding 5)."""
    monkeypatch.setattr("consentvid.media.probe", lambda p: None)
    url = "https://r2.example/videos/x"
    monkeypatch.setattr("consentvid.storage.upload", lambda *a, **k: url)
    deleted = []
    monkeypatch.setattr("consentvid.storage.delete_bytes", lambda ref: deleted.append(ref) or "ok")
    _add_video(fake_client, storageRef={"kind": "r2", "value": url})

    def boom(p):
        raise RuntimeError("PDS down")

    fake_client.repo.create_record = boom
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    with pytest.raises(RuntimeError):
        cli._register(fake_client, f, storage_kind="r2", **{**VOPTS, "allow_duplicate": True})
    assert deleted == []
