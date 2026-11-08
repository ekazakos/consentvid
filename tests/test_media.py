"""Metadata stripping against real ffmpeg output (skipped without ffmpeg)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from consentvid import _core, cli, media
from consentvid.errors import ConsentVidError

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")

SEI_UUID = "086f3693-b7b3-4f2c-9653-21492feee5b8"


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def has_encoder(name):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in out


def ffprobe_streams(path):
    import json

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", f"file:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


@pytest.fixture
def plain(tmp_path):
    f = tmp_path / "plain.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=64x48", f)
    return f


def test_comment_and_title_count_as_metadata_and_are_stripped_on_upload(tmp_path):
    """R1-12: allowlist, not a GPS-name denylist; uploads always sanitised."""
    f = tmp_path / "c.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-metadata", "comment=50.07,14.43", "-metadata", "title=home", f)
    p = media.probe(f)
    assert p.needs_sanitising and not p.sensitive_tags
    out, _, h = _core._prepare_bytes(f, "r2", False, tmp_path / "work", False)
    tags = ffprobe_streams(out)["format"].get("tags", {})
    assert "comment" not in tags and "title" not in tags
    assert h == cli.hash_file(out)
    assert b"50.07,14.43" not in out.read_bytes()


def test_cover_art_with_exif_is_dropped(tmp_path):
    """R1-13: '-map 0:v' kept attached pictures; '0:V' drops them."""
    jpg = tmp_path / "cover.jpg"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=16x16", "-frames:v", "1", jpg)
    data = jpg.read_bytes()
    marker = b"GPS 50.0755N 14.4378E SERIAL C02XYZ"
    com = b"\xff\xfe" + (len(marker) + 2).to_bytes(2, "big") + marker
    jpg.write_bytes(data[:2] + com + data[2:])  # COM segment right after SOI
    src = tmp_path / "cover.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-i", jpg, "-map", "0", "-map", "1",
        "-c:v:0", "libx264" if has_encoder("libx264") else "mpeg4", "-c:v:1", "copy",
        "-disposition:v:1", "attached_pic", src,
    )
    assert marker in src.read_bytes()
    p = media.probe(src)
    assert p.needs_sanitising and any("attached_pic" in s for s in p.extra_streams)
    out = tmp_path / "out.mp4"
    result = media.sanitise(src, out)
    assert marker not in out.read_bytes()
    assert [s["codec_type"] for s in ffprobe_streams(out)["streams"]] == ["video"]
    assert result.content_hash == cli.hash_file(out)


@pytest.mark.skipif(not has_encoder("libx264"), reason="needs libx264")
def test_h264_sei_user_data_is_removed(tmp_path):
    """R1-14: dashcam-style GPS in SEI user_data_unregistered."""
    src = tmp_path / "sei.mp4"
    try:
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-c:v", "libx264",
            "-bsf:v", f"h264_metadata=sei_user_data={SEI_UUID}+MARKER4242",
            "-metadata", "location=+50.0755+014.4378/", src,
        )
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg lacks h264_metadata")
    assert b"MARKER4242" in src.read_bytes()
    out = tmp_path / "out.mp4"
    media.sanitise(src, out)
    assert b"MARKER4242" not in out.read_bytes()
    assert ffprobe_streams(out)["streams"][0]["codec_name"] == "h264"


def test_sanitise_never_follows_or_replaces_an_existing_output(tmp_path, monkeypatch):
    """R1-15: a planted <stem>.clean<ext> symlink must not be written through."""
    monkeypatch.setenv("CONSENTVID_HOME", str(tmp_path / "home"))
    src = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-metadata", "location=+50.0755+014.4378/", src)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (tmp_path / "clip.clean.mp4").symlink_to(victim)
    with pytest.raises(ConsentVidError, match="already exists"):
        _core._prepare_bytes(src, "local", False, tmp_path / "work", False)
    assert victim.read_text() == "precious"
    # dangling symlink: also refused, target never created
    (tmp_path / "clip.clean.mp4").unlink()
    (tmp_path / "clip.clean.mp4").symlink_to(tmp_path / "created-by-attacker.mp4")
    with pytest.raises(ConsentVidError, match="already exists"):
        _core._prepare_bytes(src, "local", False, tmp_path / "work", False)
    assert not (tmp_path / "created-by-attacker.mp4").exists()


def test_clean_copy_hash_is_of_the_bytes_ffmpeg_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENTVID_HOME", str(tmp_path / "home"))
    src = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-metadata", "title=home", src)
    out, probe, h = _core._prepare_bytes(src, "local", False, tmp_path / "work", False)
    assert out == tmp_path / "clip.clean.mp4" and h == cli.hash_file(out) == probe.content_hash


def test_dash_prefixed_names_are_files_not_options(plain, tmp_path, monkeypatch):
    """R1-16: '-version.mp4' must reach ffprobe as a file."""
    shutil.copy(plain, tmp_path / "-version.mp4")
    monkeypatch.chdir(tmp_path)
    p = media.probe(Path("-version.mp4"))
    assert p.width == 64 and p.format_name == "mov"
    out = tmp_path / "-out.mp4"
    media.sanitise(Path("-version.mp4"), Path("-out.mp4"))
    assert out.exists()


def test_playlists_disguised_as_video_are_refused(plain, tmp_path):
    """R1-16: a text ffconcat file named .mp4 must not make ffmpeg read
    (and publish) another local file."""
    secret = tmp_path / "other.mp4"
    shutil.copy(plain, secret)
    x = tmp_path / "x.mp4"
    x.write_text("ffconcat version 1.0\nfile other.mp4\n")
    with pytest.raises(ConsentVidError):
        media.probe(x)
    with pytest.raises(ConsentVidError):
        media.sanitise(x, tmp_path / "x.clean.mp4")
    assert not (tmp_path / "x.clean.mp4").exists()


def test_clean_output_passes_the_allowlist(plain, tmp_path):
    out = tmp_path / "out.mp4"
    result = media.sanitise(plain, out)
    assert not result.needs_sanitising and result.content_hash == cli.hash_file(out)


# ------------------- R2-06: what ffprobe can't see is still stripped


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _with_udta_marker(src: Path, dst: Path, marker: bytes) -> None:
    """Append a GoPro-style moov/udta/CAME box (invisible to ffprobe tags).
    ffmpeg writes moov last by default, so growing it moves no offsets."""
    data = src.read_bytes()
    i = 0
    while i < len(data):
        size, kind = int.from_bytes(data[i : i + 4], "big"), data[i + 4 : i + 8]
        if kind == b"moov":
            assert i + size == len(data), "moov must be the last box"
            extra = _box(b"udta", _box(b"CAME", marker))
            moov = (size + len(extra)).to_bytes(4, "big") + data[i + 4 : i + size] + extra
            dst.write_bytes(data[:i] + moov)
            return
        i += size
    raise AssertionError("no moov box")


@pytest.fixture
def sei_only(tmp_path):
    if not has_encoder("libx264"):
        pytest.skip("needs libx264")
    src = tmp_path / "sei.mp4"
    try:
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-c:v", "libx264",
            "-bsf:v", f"h264_metadata=sei_user_data={SEI_UUID}+GPS5007N1443E", src,
        )
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg lacks h264_metadata")
    return src, b"GPS5007N1443E"


@pytest.fixture
def udta_box(plain, tmp_path):
    marker = b"CAMERA-SERIAL-C3141592653"
    out = tmp_path / "gopro.mp4"
    _with_udta_marker(plain, out, marker)
    return out, marker


@pytest.mark.parametrize("case", ["sei_only", "udta_box"])
def test_pointer_kind_strips_what_ffprobe_cannot_see(case, request, fake_client, tmp_path):
    src, marker = request.getfixturevalue(case)
    assert marker in src.read_bytes()
    assert not media.probe(src).needs_sanitising  # the old gate would have published it as is
    opts = dict(
        title="t", description="", language="", tags=(), keep_metadata=False, allow_duplicate=False,
        validate=None, storage_value="https://example.org/clip.mp4",
    )
    with pytest.raises(ConsentVidError, match="clean copy was written"):
        _core._register(fake_client, src, storage_kind="url", **opts)
    clean = src.with_name(f"{src.stem}.clean{src.suffix}")
    assert clean.exists() and marker not in clean.read_bytes()
    assert not fake_client.repo.records
    _, _, rec = _core._register(fake_client, clean, storage_kind="url", **opts)  # accepted as is
    assert rec["contentHash"] == cli.hash_file(clean)


@pytest.mark.parametrize("case", ["sei_only", "udta_box"])
def test_local_kind_commits_to_a_clean_copy(case, request, fake_client, tmp_path):
    src, marker = request.getfixturevalue(case)
    opts = dict(
        title="t", description="", language="", tags=(), keep_metadata=False, allow_duplicate=False,
        validate=None, storage_value=None,
    )
    _, _, rec = _core._register(fake_client, src, storage_kind="local", **opts)
    # R3-15: the published name is content-derived; the clean copy sits next to the source
    assert rec["storageRef"]["value"] == rec["contentHash"] + src.suffix.lower()
    committed = src.with_name(f"{src.stem}.clean{src.suffix}")
    assert committed != src and marker not in committed.read_bytes()
    assert rec["contentHash"] == cli.hash_file(committed)


# ----------------------- R2-07: codec-private metadata is never kept


def _try_ffmpeg(*args):
    try:
        ffmpeg(*args)
    except subprocess.CalledProcessError:
        pytest.skip(f"ffmpeg can't build this fixture: {args}")


@pytest.mark.parametrize(
    "name,args,what",
    [
        (
            "vorbis.webm",
            ["-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libvpx-vp9",
             "-c:a", "vorbis", "-ac", "2", "-strict", "-2", "-metadata:s:a:0", "VENDOR_MARKER=GPS5007", "-shortest"],
            "audio codec",
        ),
        (
            "flac.mkv",
            ["-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264",
             "-c:a", "flac", "-shortest"],
            "audio codec",
        ),
        ("mjpeg.mov", ["-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-c:v", "mjpeg"], "video codec"),
        ("mpeg4.mp4", ["-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-c:v", "mpeg4"], "video codec"),
    ],
)
def test_codecs_with_private_metadata_are_refused(tmp_path, name, args, what):
    src = tmp_path / name
    _try_ffmpeg(*args, src)
    with pytest.raises(ConsentVidError, match=what):
        media.sanitise(src, tmp_path / f"out{src.suffix}")
    assert not (tmp_path / f"out{src.suffix}").exists()


@pytest.mark.skipif(not has_encoder("libx265"), reason="needs libx265")
@pytest.mark.parametrize("ext", [".mp4", ".mkv"])
def test_hevc_sei_in_decoder_configuration_is_removed(tmp_path, ext):
    """x265 stores an SEI with its build and full option string in hvcC; the
    filter_units bsf never touches that."""
    src = tmp_path / f"hevc{ext}"
    _try_ffmpeg("-f", "lavfi", "-i", "testsrc=d=1:s=64x48", "-c:v", "libx265", "-x265-params", "log-level=error", src)
    assert b"x265 (build" in src.read_bytes()
    assert not media.probe(src).needs_sanitising
    out = tmp_path / f"out{ext}"
    media.sanitise(src, out)
    assert b"x265 (build" not in out.read_bytes()
    assert ffprobe_streams(out)["streams"][0]["codec_name"] == "hevc"
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", f"file:{out}", "-f", "null", "-"], capture_output=True, text=True)
    assert dec.returncode == 0 and not dec.stderr.strip()
    # ... and the register path reaches a fixed point (Matroska needs a pass more).
    fixed, _, h = _core._sanitise_to_fixed_point(src, tmp_path / "work")
    assert b"x265 (build" not in fixed.read_bytes() and h == cli.hash_file(fixed)


def test_hevc_filler_sei_has_the_exact_length():
    for n in range(5, 1200):
        nal = media._hevc_filler_nal(b"\x4e\x01", n)
        assert len(nal) == n and nal[:2] == b"\x4e\x01" and nal.endswith(b"\x80")
        assert b"\x00\x00" not in nal  # no emulation-prevention hazards


# ------------------ R3-12: non-SEI NAL units (allowlist, not SEI denylist)


def _frames(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", f"file:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip())


def _decode_errors(path):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", f"file:{path}", "-f", "null", "-"], capture_output=True, text=True
    ).stderr.strip()


@pytest.mark.parametrize(
    "codec,types",
    [("h264", [24, 31, 12]), ("hevc", [38, 41, 48, 62])],
)
def test_unspecified_and_filler_nal_units_are_removed(tmp_path, codec, types):
    from media_inject import MARK, inject_nal_units

    enc = {"h264": ["-c:v", "libx264", "-bf", "0", "-g", "5"],
           "hevc": ["-c:v", "libx265", "-x265-params", "log-level=none:bframes=0:keyint=5"]}[codec]
    if not has_encoder(enc[1]):
        pytest.skip(f"needs {enc[1]}")
    raw = tmp_path / f"raw.{codec}"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "2", *enc, "-f", codec, raw)
    inj = tmp_path / f"inj.{codec}"
    inj.write_bytes(inject_nal_units(raw.read_bytes(), types, hevc=codec == "hevc"))
    src = tmp_path / "inj.mp4"
    ffmpeg("-f", codec, "-r", "10", "-i", inj, "-c", "copy", *(["-tag:v", "hvc1"] if codec == "hevc" else []), src)
    assert MARK in src.read_bytes()
    out = tmp_path / "out.mp4"
    media.sanitise(src, out)
    assert MARK not in out.read_bytes()
    assert _decode_errors(out) == ""
    assert _frames(out) == _frames(src) == 20


def test_dolby_vision_is_refused():
    with pytest.raises(ConsentVidError, match="Dolby Vision"):
        media._check_codecs(Path("dv.mp4"), media.Probe(video_codecs=["hevc"], dolby_vision=True))


# ----------------------- R3-13: AAC program config element (free text)


def _with_audio(tmp_path, name, layout):
    f = tmp_path / name
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
        "-filter_complex", f"[1:a]aformat=channel_layouts={layout}[a]", "-map", "0:v", "-map", "[a]",
        "-t", "1", "-c:v", "libx264", "-c:a", "aac", f,
    )
    return f


def test_aac_with_program_config_element_is_refused(tmp_path):
    src = _with_audio(tmp_path, "hex.mp4", "hexagonal")
    with pytest.raises(ConsentVidError, match="program config element"):
        media.sanitise(src, tmp_path / "out.mp4")
    ts = tmp_path / "hex.ts"  # ADTS: no extradata, the config is in each frame header
    ffmpeg("-i", src, "-c", "copy", ts)
    with pytest.raises(ConsentVidError, match="program config element"):
        media.sanitise(ts, tmp_path / "out.ts")


def test_stereo_aac_still_sanitises_and_converges(tmp_path):
    src = _with_audio(tmp_path, "st.mp4", "stereo")
    clean, _probe, h = _core._sanitise_to_fixed_point(src, tmp_path)
    assert h == cli.hash_file(clean)


def test_aac_channel_config_parser():
    assert media._aac_channel_config(bytes.fromhex("1190")) == 2
    assert media._aac_channel_config(bytes.fromhex("1180")) == 0
    # escaped object type (31 -> 32+6 bits) and explicit 24-bit sample rate
    asc = int("11111" + "000001" + "1111" + "0" * 24 + "0110" + "0" * 5, 2).to_bytes(6, "big")
    assert media._aac_channel_config(asc) == 6
    with pytest.raises(ConsentVidError):
        media._aac_channel_config(b"\x11")


# ------------- R3-14: container/suffix mismatch, multi-stsd, packet counts


def test_multiple_sample_descriptions_are_refused(tmp_path):
    from media_inject import add_second_sample_description

    base = tmp_path / "base.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "2", "-c:v", "libx264", "-bf", "0", base)
    ms = tmp_path / "ms.mp4"
    ms.write_bytes(add_second_sample_description(base.read_bytes()))
    assert media._mp4_sample_description_counts(ms) == [2]
    assert media._mp4_sample_description_counts(base) == [1]
    with pytest.raises(ConsentVidError, match="sample descriptions"):
        media.sanitise(ms, tmp_path / "out.mp4")


def test_mp4_named_ts_is_refused(tmp_path):
    if not has_encoder("libx265"):
        pytest.skip("needs libx265")
    src = tmp_path / "h.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "1", "-c:v", "libx265",
           "-x265-params", "log-level=none", src)
    renamed = tmp_path / "h.ts"
    src.rename(renamed)
    with pytest.raises(ConsentVidError, match="is a mov file but is named .ts"):
        media.sanitise(renamed, tmp_path / "out.ts")


def test_packet_count_change_is_refused(plain, tmp_path, monkeypatch):
    real = media._kept_packet_counts
    monkeypatch.setattr(media, "_kept_packet_counts", lambda p: real(p) if "plain" in str(p) else [1])
    with pytest.raises(ConsentVidError, match="packet counts"):
        media.sanitise(plain, tmp_path / "out.mp4")
