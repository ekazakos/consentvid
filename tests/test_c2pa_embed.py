"""C2PA embedding: output path handling (R3-04) and SDK floor (R3-16). Offline."""

import os
import shutil
import subprocess

import pytest

from conftest import ALICE, consent_record, video_record
from consentvid.errors import ConfigError, ConsentVidError

c2pa = pytest.importorskip("c2pa")
pytest.importorskip("cryptography")

from consentvid import c2pa_embed as ce  # noqa: E402


@pytest.fixture
def signer_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(ce, "_SIGNER_DIR", home / "c2pa")
    monkeypatch.setattr(ce, "_CERT_PATH", home / "c2pa" / "signer.cert.pem")
    monkeypatch.setattr(ce, "_KEY_PATH", home / "c2pa" / "signer.key.pem")
    return home


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    p = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )
    return p


def _embed(video, out):
    return ce.embed_consent(
        video_path=video,
        out_path=out,
        consent_uri=f"at://{ALICE}/org.consentvid.consent/c1",
        consent_cid="bafyconsent",
        consent=consent_record(),
        video_uri=f"at://{ALICE}/org.consentvid.video/v1",
        video_cid="bafyvideo",
        video=video_record(),
        contributor_handle="alice.test",
        contributor_did=ALICE,
    )


def test_dangling_symlink_at_output_is_refused(signer_home, video, tmp_path):
    target = tmp_path / "elsewhere" / "planted.mp4"
    target.parent.mkdir()
    out = tmp_path / "clip.c2pa.mp4"
    out.symlink_to(target)
    with pytest.raises(ConsentVidError, match="already exists"):
        _embed(video, out)
    assert not target.exists()
    assert out.is_symlink() and os.readlink(out) == str(target)


def test_existing_output_file_is_refused_and_kept(signer_home, video, tmp_path):
    out = tmp_path / "clip.c2pa.mp4"
    out.write_bytes(b"precious")
    with pytest.raises(ConsentVidError, match="already exists"):
        _embed(video, out)
    assert out.read_bytes() == b"precious"


def test_normal_embed_still_reads_back(signer_home, video, tmp_path):
    out = _embed(video, tmp_path / "clip.c2pa.mp4")
    assert not out.is_symlink()
    receipt, _state = ce.extract_consent_receipt(out)
    assert receipt["consentUri"] == f"at://{ALICE}/org.consentvid.consent/c1"
    assert ce.read_manifest(out)["active_manifest"]


@pytest.mark.parametrize("v", ["0.79.0", "0.85.0", "garbage"])
def test_old_c2pa_sdk_is_refused(monkeypatch, v):
    """R3-16: c2pa-rs <= 0.80.1 (and anything before 0.85.1) is affected by
    CVE-2026-34712; we parse untrusted files with it."""
    monkeypatch.setattr(c2pa, "sdk_version", lambda: v)
    with pytest.raises(ConfigError, match="CVE-2026-34712"):
        ce._require_c2pa()
    with pytest.raises(ConfigError):
        ce.read_manifest("/nonexistent.mp4")


def test_current_c2pa_sdk_is_accepted(monkeypatch):
    monkeypatch.setattr(c2pa, "sdk_version", lambda: "0.85.1")
    assert ce._require_c2pa() is c2pa
