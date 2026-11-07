"""Media inspection and privacy sanitisation (ffprobe / ffmpeg).

Phones embed GPS coordinates, device make/model and sometimes serial numbers in
video containers — Apple even writes a timed-metadata track ("mebx") with
location, and XMP/vendor boxes carry more. Once uploaded to public storage
those bytes are effectively permanent, so ConsentVid strips identifying
metadata *before* hashing and uploading, by default. Stripping is a lossless
stream copy (no re-encode) that keeps only the main video and audio streams:

* all container and stream metadata, chapters, XMP and vendor boxes are dropped;
* data, subtitle and attachment streams (GPS tracks, GoPro GPMF telemetry) and
  attached pictures (cover art, which can carry EXIF GPS/serials) are dropped;
* in-band H.264/HEVC NAL units and AV1 OBUs are filtered by ALLOWLIST: only
  slices, parameter sets, AUD and end-of-sequence/stream units (H.264 types
  1-5, 7-11; HEVC 0-9, 16-21, 32-37) and AV1 sequence/frame/tile OBUs (1-4,
  6-8) are kept. SEI (dashcams and action cams put GPS and serials in
  user_data_unregistered SEI), filler, reserved and unspecified units
  (H.264 0, 12-31; HEVC 38-63, incl. Dolby Vision RPU/EL), AV1 metadata and
  padding OBUs are dropped. For HEVC/AV1 this also drops HDR
  mastering-display / content-light metadata; colour information in the
  SPS/VUI / sequence header is kept. Dolby Vision input is refused (it would
  silently lose its enhancement data): transcode first;
* SEI stored in the HEVC decoder configuration (hvcC, Matroska CodecPrivate),
  which the bitstream filter does not touch, is overwritten in place with
  filler SEI of the same length; an AV1 configuration carrying metadata OBUs
  is refused;
* only codecs whose configuration and bitstream we know how to clean are
  stream-copied: video H.264, HEVC, VP8, VP9, AV1; audio AAC, Opus, MP3, PCM.
  Anything else (Vorbis/Theora/FLAC headers carry free-text comments, MJPEG
  frames carry EXIF, ProRes, ...) is refused: transcode first. AAC with a
  program config element (channelConfiguration 0, whose comment_field holds
  up to 255 free bytes) is refused too;
* a file whose container doesn't match its name (an MP4 named .ts), an MP4
  track with more than one sample description, or an output with a
  different packet count than the input is refused (each would let a
  stream-copy keep or silently drop data the checks above don't see).

Residual limitation: identifying information inside the coded picture itself
(faces, licence plates, burnt-in timestamps), and private data inside in-band
AAC data stream / fill elements (DSE/FIL extension payloads, an in-band PCE
when the stream's configuration does not announce one) or MP3 ancillary
bits, is not touched.

Every input is untrusted. Paths are passed as ``file:<absolute path>`` with a
``file``-only protocol whitelist (a name starting with '-' is never parsed as
an option), and only real video container formats are accepted (a text
playlist named ``.mp4`` is refused, so ffmpeg cannot be steered into reading
other local files).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from consentvid.errors import ConfigError, ConsentVidError
from consentvid.paths import secure_dir

# Tag-name patterns that identify a person, place or device (for display; the
# decision to sanitise is allowlist-based, see _BENIGN_TAGS).
_SENSITIVE_TAG = re.compile(
    r"(location|gps|xyz|iso6709|latitude|longitude|make|model|serial|device|"
    r"owner|artist|author|camera|lens|user|email|phone)",
    re.IGNORECASE,
)
# Tags that are known not to identify anyone. Everything else (title, comment,
# creation_time, XMP, Apple content identifiers, vendor keys, ...) counts as
# metadata to strip.
_BENIGN_TAGS = frozenset(
    {
        "major_brand",
        "minor_version",
        "compatible_brands",
        "handler_name",
        "vendor_id",
        "language",
        "encoder",
        "duration",
        "timecode",
    }
)
# Demuxers we accept. ffprobe sniffs content, so a file named .mp4 could be an
# ffconcat/HLS playlist or an image sequence; those are refused.
_ALLOWED_FORMATS = frozenset(
    {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi", "mpegts", "mpeg"}
)
# Bitstream filters per codec: ALLOWLISTS of NAL unit / OBU types (R3-12).
# H.264: slices incl. data partitions and IDR (1-5), SPS/PPS/AUD/end of
#   sequence/end of stream (7-11). Drops SEI (6), filler (12), SPS extension
#   and SVC/MVC layers (13-23) and unspecified types (0, 24-31).
# HEVC: VCL (0-9), IRAP (16-21), VPS/SPS/PPS/AUD/EOS/EOB (32-37). Drops
#   filler (38), SEI (39/40), reserved (10-15, 22-31, 41-47) and unspecified
#   types (48-63, which include Dolby Vision RPU/EL as 62/63).
# AV1: sequence header, temporal delimiter, frame header, tile group, frame,
#   redundant frame header, tile list (1-4, 6-8). Drops metadata (5),
#   padding (15) and reserved OBUs.
_SEI_FILTERS = {
    "h264": "filter_units=pass_types=1-5|7-11",
    "hevc": "filter_units=pass_types=0-9|16-21|32-37",
    "av1": "filter_units=pass_types=1-4|6-8",
}
# Container suffixes per demuxer (first ffprobe format name). The output
# muxer is chosen from the name, so they must agree (R3-14).
_FORMAT_SUFFIXES = {
    "mov": {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mj2", ".m4a"},
    "mp4": {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mj2", ".m4a"},
    "matroska": {".mkv", ".webm"},
    "webm": {".mkv", ".webm"},
    "avi": {".avi"},
    "mpegts": {".ts", ".mts", ".m2ts"},
    "mpeg": {".mpg", ".mpeg", ".vob"},
}
# Codecs sanitise() will stream-copy (see module docstring).
_VIDEO_CODECS = frozenset({"h264", "hevc", "vp8", "vp9", "av1"})
_AUDIO_CODECS = frozenset({"aac", "opus", "mp3"})
_HEVC_SEI_TYPES = (39, 40)
_AV1_OBU_SEQUENCE_HEADER = 1


@dataclass
class Probe:
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    sensitive_tags: dict[str, str] = field(default_factory=dict)
    unknown_tags: dict[str, str] = field(default_factory=dict)
    extra_streams: list[str] = field(default_factory=list)  # data/attachment/subtitle/cover art
    format_name: str | None = None  # first allowed demuxer name, e.g. "mov"
    video_codecs: list[str] = field(default_factory=list)  # main (non-cover) video streams, in order
    audio_codecs: list[str] = field(default_factory=list)
    dolby_vision: bool = False  # a DOVI configuration record on a video stream
    content_hash: str | None = None  # set by sanitise(): hash of the bytes it produced

    @property
    def needs_sanitising(self) -> bool:
        return bool(self.sensitive_tags or self.unknown_tags or self.extra_streams)

    def record_fields(self) -> dict:
        out = {}
        if self.width:
            out["width"] = self.width
        if self.height:
            out["height"] = self.height
        if self.duration_ms is not None:
            out["durationMs"] = self.duration_ms
        return out


def have_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _ff_path(path: Path) -> str:
    """ffmpeg's file: protocol: no option parsing, no protocol guessing."""
    return "file:" + str(Path(path).resolve())


def _is_cover(s: dict) -> bool:
    return (s.get("disposition") or {}).get("attached_pic") == 1


# Record integers stay within 53 bits (atproto.com/specs/data-model: JS
# readers can't represent more).
MAX_RECORD_INT = 2**53 - 1


def _record_int(value) -> int | None:
    """A non-negative integer for a record field, or None (field dropped)
    for anything non-finite ('inf', NaN), negative, too large or not a
    number."""
    if isinstance(value, bool):
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 0 <= n <= MAX_RECORD_INT else None


def probe(path: Path) -> Probe | None:
    """Inspect a video. Returns None if ffprobe is unavailable. Raises
    ConsentVidError for anything that isn't a supported video container."""
    if not have_ffprobe():
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-export_xmp",
                "1",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                _ff_path(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise ConsentVidError(f"ffprobe could not read {Path(path).name}: {e.stderr.strip()}") from e
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    names = [n for n in str(fmt.get("format_name") or "").split(",") if n]
    if not names or any(n not in _ALLOWED_FORMATS for n in names):
        raise ConsentVidError(
            f"{Path(path).name} is not a supported video container (detected {fmt.get('format_name')!r})"
        )
    p = Probe(format_name=names[0])
    videos = [s for s in streams if s.get("codec_type") == "video" and not _is_cover(s)]
    if not videos:
        raise ConsentVidError(f"{Path(path).name} has no video stream")
    video = videos[0]
    p.video_codecs = [str(s.get("codec_name") or "") for s in videos]
    p.audio_codecs = [str(s.get("codec_name") or "") for s in streams if s.get("codec_type") == "audio"]
    p.dolby_vision = any(
        "dovi" in str(sd.get("side_data_type", "")).lower()
        for s in videos
        for sd in (s.get("side_data_list") or [])
        if isinstance(sd, dict)
    )
    p.width, p.height = _record_int(video.get("width")), _record_int(video.get("height"))
    dur = video.get("duration") or fmt.get("duration")
    if dur not in (None, "N/A"):
        try:
            p.duration_ms = _record_int(float(dur) * 1000)
        except (TypeError, ValueError):
            p.duration_ms = None

    def scan(tags: dict, where: str):
        for k, v in (tags or {}).items():
            if _SENSITIVE_TAG.search(k):
                p.sensitive_tags[f"{where}:{k}"] = str(v)[:80]
            elif k.lower() not in _BENIGN_TAGS:
                p.unknown_tags[f"{where}:{k}"] = str(v)[:80]

    scan(fmt.get("tags"), "format")
    for s in streams:
        idx = s.get("index")
        scan(s.get("tags"), f"stream{idx}")
        if s.get("codec_type") not in ("video", "audio") or _is_cover(s):
            kind = "attached_pic" if _is_cover(s) else s.get("codec_type")
            p.extra_streams.append(f"stream{idx}:{kind}:{s.get('codec_tag_string', '')}")
    return p


def _config_dir() -> Path:
    return Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid"))


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256-{h.hexdigest()}"


def publish_file(tmp: Path, dst: Path) -> None:
    """Copy a finished private file to dst, which must not exist yet."""
    _publish(Path(tmp), Path(dst))


def _publish(tmp: Path, dst: Path) -> None:
    """Move a finished private file to dst without ever following or
    replacing anything already at dst (including a dangling symlink)."""
    try:
        os.link(tmp, dst, follow_symlinks=False)
        return
    except FileExistsError:
        raise ConsentVidError(f"{dst} already exists; remove it first") from None
    except OSError:
        pass  # e.g. different filesystems: copy into an exclusively created file
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dst, flags, 0o644)
    except FileExistsError:
        raise ConsentVidError(f"{dst} already exists; remove it first") from None
    with os.fdopen(fd, "wb") as out, tmp.open("rb") as src:
        shutil.copyfileobj(src, out)


def sanitise(src: Path, dst: Path) -> Probe:
    """Copy the main video+audio streams to dst with all metadata, chapters,
    cover art, non-A/V streams and SEI removed. Returns the probe of the
    result, which is guaranteed clean, with ``content_hash`` set to the hash
    of exactly the bytes written (computed before dst became visible).

    ffmpeg writes into a private directory under CONSENTVID_HOME; dst is only
    created at the end, exclusively. Raises ConsentVidError if dst exists."""
    if not have_ffmpeg():
        raise ConfigError("ffmpeg is required to strip identifying metadata (brew install ffmpeg)")
    src_probe = probe(src)
    if src_probe is None:
        raise ConfigError("ffprobe is required to strip identifying metadata (brew install ffmpeg)")
    _check_codecs(src, src_probe)
    _check_container(src, src_probe, dst)
    _check_aac_configs(src)
    if os.path.lexists(dst):
        raise ConsentVidError(f"{dst} already exists; remove it first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    home = _config_dir()
    secure_dir(home)  # also holds the session and keys: owner-only
    with tempfile.TemporaryDirectory(dir=home, prefix="sanitise-") as tmpdir:
        tmp = Path(tmpdir) / f"clean{dst.suffix.lower()}"
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-protocol_whitelist",
            "file",
            "-f",
            src_probe.format_name,
            "-i",
            _ff_path(src),
            "-map",
            "0:V",  # capital V: video streams that are NOT attached pictures
            "-map",
            "0:a?",
            "-map_metadata",
            "-1",
            "-map_metadata:s",
            "-1",
            "-map_chapters",
            "-1",
            "-c",
            "copy",
            "-fflags",
            "+bitexact",
        ]
        for i, codec in enumerate(src_probe.video_codecs):
            if codec in _SEI_FILTERS:
                cmd += [f"-bsf:v:{i}", _SEI_FILTERS[codec]]
        if tmp.suffix in (".mp4", ".m4v", ".mov"):
            cmd += ["-movflags", "+faststart"]  # moov first: streamable over HTTP
        cmd.append(_ff_path(tmp))
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600)
        except subprocess.CalledProcessError as e:
            raise ConsentVidError(f"ffmpeg failed to sanitise {src.name}: {e.stderr.strip()}") from e
        _clean_codec_config(tmp)
        _check_aac_configs(tmp)
        _check_packet_counts(src, tmp)
        result = probe(tmp)
        if result is None or result.needs_sanitising:
            leftover = (
                {**result.sensitive_tags, **result.unknown_tags, "streams": result.extra_streams}
                if result
                else "unknown"
            )
            raise ConsentVidError(f"metadata still present after sanitising: {leftover}")
        result.content_hash = _hash(tmp)
        _publish(tmp, dst)
    return result


# ------------------------------------------------- codec configuration ------


def _check_codecs(src: Path, p: Probe) -> None:
    if p.dolby_vision:
        raise ConsentVidError(
            f"{Path(src).name} is Dolby Vision: its RPU/enhancement data would be dropped by "
            "metadata stripping. Transcode it first (e.g. ffmpeg -i in -c:v libx265 -c:a aac out.mp4), "
            "or pass --keep-metadata."
        )
    for c in p.video_codecs:
        if c not in _VIDEO_CODECS:
            raise ConsentVidError(
                f"{Path(src).name}: video codec {c!r} can't be stripped of metadata losslessly "
                f"(supported: {', '.join(sorted(_VIDEO_CODECS))}). Transcode it first "
                "(e.g. ffmpeg -i in -c:v libx264 -c:a aac out.mp4), or pass --keep-metadata."
            )
    for c in p.audio_codecs:
        if c not in _AUDIO_CODECS and not c.startswith("pcm_"):
            raise ConsentVidError(
                f"{Path(src).name}: audio codec {c!r} can carry free-text metadata in its headers "
                f"(supported: {', '.join(sorted(_AUDIO_CODECS))}, PCM). Transcode the audio first "
                "(e.g. -c:a aac or -c:a libopus), or pass --keep-metadata."
            )


def _check_container(src: Path, p: Probe, dst: Path) -> None:
    """The output muxer follows dst's suffix: it must be the source's own
    container (an MP4 written as .ts goes through hevc_mp4toannexb, which
    re-injects configuration SEI). MP4 tracks must have exactly one sample
    description: a second one carries its own configuration and the
    stream copy silently drops its frames."""
    allowed = _FORMAT_SUFFIXES.get(p.format_name or "", set())
    if dst.suffix.lower() not in allowed:
        raise ConsentVidError(
            f"{Path(src).name} is a {p.format_name} file but is named {Path(src).suffix or '(no suffix)'}; "
            f"rename it to one of {', '.join(sorted(allowed)) or '(none)'} and retry"
        )
    if p.format_name in ("mov", "mp4"):
        for n in _mp4_sample_description_counts(src):
            if n != 1:
                raise ConsentVidError(
                    f"{Path(src).name} has a track with {n} sample descriptions; "
                    "transcode it first (e.g. ffmpeg -i in -c:v libx264 -c:a aac out.mp4), or pass --keep-metadata"
                )


_MP4_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl")


def _mp4_sample_description_counts(path: Path) -> list[int]:
    """entry_count of every trak's stsd box (ISO BMFF box walk)."""
    import struct

    counts: list[int] = []

    def walk(f, start: int, end: int, depth: int) -> None:
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                return
            size, kind = struct.unpack(">I4s", head[:8])
            hdr = 8
            if size == 1:
                if len(head) < 16:
                    raise ConsentVidError("truncated MP4 box header")
                size, hdr = struct.unpack(">Q", head[8:16])[0], 16
            elif size == 0:
                size = end - pos
            if size < hdr or pos + size > end:
                raise ConsentVidError("malformed MP4 box structure; refusing")
            if kind == b"stsd" and depth == len(_MP4_CONTAINERS):
                f.seek(pos + hdr)
                body = f.read(8)
                if len(body) < 8:
                    raise ConsentVidError("truncated stsd box")
                counts.append(struct.unpack(">I", body[4:8])[0])
            elif depth < len(_MP4_CONTAINERS) and kind == _MP4_CONTAINERS[depth]:
                walk(f, pos + hdr, pos + size, depth + 1)
            pos += size

    with Path(path).open("rb") as f:
        f.seek(0, os.SEEK_END)
        walk(f, 0, f.tell(), 0)
    return counts


def _kept_packet_counts(path: Path) -> list[int | None]:
    """Packets per stream that sanitise keeps (non-cover video, then audio),
    in output order."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-count_packets",
            "-show_entries", "stream=index,codec_type,nb_read_packets:stream_disposition=attached_pic",
            "-of", "json", _ff_path(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=3600,
    ).stdout
    streams = json.loads(out or "{}").get("streams", [])

    def n(s):
        try:
            return int(s.get("nb_read_packets"))
        except (TypeError, ValueError):
            return None

    video = [n(s) for s in streams if s.get("codec_type") == "video" and not _is_cover(s)]
    audio = [n(s) for s in streams if s.get("codec_type") == "audio"]
    return video + audio


def _check_packet_counts(src: Path, out: Path) -> None:
    """A stream copy must keep every packet; fewer means data was dropped
    that the record would then commit to (R3-14)."""
    a, b = _kept_packet_counts(src), _kept_packet_counts(out)
    if a != b or any(x is None for x in a):
        raise ConsentVidError(
            f"sanitising {Path(src).name} changed the packet counts ({a} -> {b}); refusing "
            "(transcode the file first, or pass --keep-metadata)"
        )


def _aac_channel_config(asc: bytes) -> int:
    """channelConfiguration from an AAC AudioSpecificConfig (ISO 14496-3
    1.6.2.1): audioObjectType (5 bits, 31 = escape + 6 bits),
    samplingFrequencyIndex (4 bits, 0xF = escape + 24-bit rate), then 4 bits."""
    bits = int.from_bytes(asc[:8].ljust(8, b"\0"), "big")
    pos = 64

    def take(n: int) -> int:
        nonlocal pos
        pos -= n
        if pos < 64 - 8 * len(asc):
            raise ConsentVidError("truncated AAC AudioSpecificConfig")
        return (bits >> pos) & ((1 << n) - 1)

    if take(5) == 31:
        take(6)
    if take(4) == 0xF:
        take(24)
    return take(4)


def _adts_channel_config(frame: bytes) -> int:
    if len(frame) < 4 or frame[0] != 0xFF or frame[1] & 0xF0 != 0xF0:
        raise ConsentVidError("AAC stream has neither a configuration nor ADTS headers; refusing")
    return ((frame[2] & 0x01) << 2) | (frame[3] >> 6)


def _check_aac_configs(path: Path) -> None:
    """Refuse AAC whose configuration has channelConfiguration 0: it is then
    described by a program config element, whose comment_field carries up to
    255 free bytes (an encoder name, or a device serial), also in-band."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", "a",
            "-show_streams", "-show_data", "-of", "json", _ff_path(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    for st in json.loads(out or "{}").get("streams", []):
        if st.get("codec_name") != "aac":
            continue
        dump = st.get("extradata")
        if isinstance(dump, str) and dump.strip():
            config = _aac_channel_config(_parse_hexdump(dump))
        else:  # ADTS (MPEG-TS): the configuration is in every frame header
            config = _adts_channel_config(_first_packet(path, st.get("index")))
        if config == 0:
            raise ConsentVidError(
                "AAC with a program config element can carry free text; transcode the audio "
                "(e.g. -c:a aac -ac 2) or use --keep-metadata"
            )


def _first_packet(path: Path, index) -> bytes:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", str(int(index)),
            "-read_intervals", "%+#1", "-show_packets", "-show_data", "-of", "json", _ff_path(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    packets = json.loads(out or "{}").get("packets", [])
    return _parse_hexdump(packets[0].get("data", "")) if packets else b""


def _stream_extradata(path: Path) -> list[tuple[str, bytes]]:
    """(codec_name, extradata) of every stream, as ffprobe reports them."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_streams", "-show_data",
            "-of", "json", _ff_path(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    result = []
    for st in json.loads(out or "{}").get("streams", []):
        dump = st.get("extradata")
        if not isinstance(dump, str) or not dump.strip():
            continue
        result.append((str(st.get("codec_name") or ""), _parse_hexdump(dump)))
    return result


def _parse_hexdump(dump: str) -> bytes:
    """ffprobe -show_data format: 'OFFSET: hhhh hhhh ...  ascii'."""
    data = bytearray()
    for line in dump.splitlines():
        if ": " not in line:
            continue
        hexpart = line.split(": ", 1)[1][:40]
        data += bytes.fromhex(hexpart.replace(" ", ""))
    return bytes(data)


def _hevc_filler_nal(header: bytes, length: int) -> bytes:
    """An HEVC SEI NAL of exactly ``length`` bytes (same NAL header) holding
    only filler_payload SEI messages (payloadType 3, 0xFF bytes)."""
    body = length - len(header) - 1  # minus rbsp_trailing_bits (0x80)

    def message(size: int) -> bytes:
        return b"\x03" + b"\xff" * (size // 255) + bytes([size % 255]) + b"\xff" * size

    for extra in (b"", b"\x03\x00"):  # optional empty second message
        need = body - len(extra)
        for size in range(max(0, need - 2 - need // 255 - 1), max(0, need) + 1):
            m = message(size)
            if len(m) == need:
                return header + m + extra + b"\x80"
    raise ConsentVidError(f"can't rewrite a {length}-byte HEVC SEI unit")


def _scrub_hvcc(cfg: bytes) -> bytes:
    """hvcC with every SEI NAL unit replaced by same-length filler SEI.
    Raises ConsentVidError if it doesn't parse as hvcC."""
    if len(cfg) < 23 or cfg[0] != 1:
        raise ConsentVidError("HEVC configuration is not an hvcC record")
    out, i = bytearray(cfg[:23]), 23
    for _ in range(cfg[22]):
        if i + 3 > len(cfg):
            raise ConsentVidError("truncated hvcC")
        nal_type, count = cfg[i] & 0x3F, int.from_bytes(cfg[i + 1 : i + 3], "big")
        out += cfg[i : i + 3]
        i += 3
        for _ in range(count):
            if i + 2 > len(cfg):
                raise ConsentVidError("truncated hvcC")
            n = int.from_bytes(cfg[i : i + 2], "big")
            nal = cfg[i + 2 : i + 2 + n]
            if len(nal) != n or n < 2:
                raise ConsentVidError("truncated hvcC")
            if nal_type in _HEVC_SEI_TYPES or (nal[0] >> 1) & 0x3F in _HEVC_SEI_TYPES:
                if n < 5:
                    raise ConsentVidError("HEVC SEI unit too short to rewrite")
                nal = _hevc_filler_nal(nal[:2], n)
            out += cfg[i : i + 2] + nal
            i += 2 + n
    if i != len(cfg):
        raise ConsentVidError("trailing bytes after hvcC arrays")
    return bytes(out)


def _leb128(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    for k in range(8):
        if i + k >= len(buf):
            raise ConsentVidError("truncated AV1 configuration")
        b = buf[i + k]
        value |= (b & 0x7F) << (7 * k)
        if not b & 0x80:
            return value, i + k + 1
    raise ConsentVidError("bad leb128 in AV1 configuration")


def _check_av1c(cfg: bytes) -> None:
    """Refuse an av1C whose configOBUs hold anything but sequence headers
    (metadata OBUs there survive the bitstream filter)."""
    if len(cfg) < 4 or not cfg[0] & 0x80:
        raise ConsentVidError("AV1 configuration is not an av1C record")
    i = 4
    while i < len(cfg):
        h = cfg[i]
        obu_type, ext, has_size = (h >> 3) & 0x0F, (h >> 2) & 1, (h >> 1) & 1
        i += 1 + ext
        if has_size:
            size, i = _leb128(cfg, i)
        else:
            size = len(cfg) - i
        if obu_type != _AV1_OBU_SEQUENCE_HEADER:
            raise ConsentVidError(
                f"AV1 configuration carries a non-sequence-header OBU (type {obu_type}); "
                "re-encode the video first, or pass --keep-metadata"
            )
        i += size


def _clean_codec_config(path: Path) -> None:
    """Clean metadata the bitstream filters can't reach: SEI inside hvcC is
    overwritten in place (same length, so no container offsets move); an av1C
    with metadata OBUs is refused. Verifies the result."""
    import mmap

    for codec, cfg in _stream_extradata(path):
        if codec == "av1":
            _check_av1c(cfg)
        elif codec == "hevc" and cfg[:1] == b"\x01":
            clean = _scrub_hvcc(cfg)
            if clean == cfg:
                continue
            with path.open("r+b") as f, mmap.mmap(f.fileno(), 0) as mm:
                at = mm.find(cfg)
                if at < 0 or mm.find(cfg, at + 1) >= 0:
                    raise ConsentVidError("could not locate the HEVC configuration in the output; refusing")
                mm[at : at + len(cfg)] = clean
                mm.flush()
        elif codec == "hevc" and _annexb_has_sei(cfg):
            raise ConsentVidError("HEVC configuration in Annex B form carries SEI; refusing")
    for codec, cfg in _stream_extradata(path):  # verify
        if codec == "hevc" and cfg[:1] == b"\x01" and _scrub_hvcc(cfg) != cfg:
            raise ConsentVidError("HEVC configuration still carries SEI after sanitising")


def _annexb_has_sei(cfg: bytes) -> bool:
    i = cfg.find(b"\x00\x00\x01")
    while i >= 0 and i + 3 < len(cfg):
        if (cfg[i + 3] >> 1) & 0x3F in _HEVC_SEI_TYPES:
            return True
        i = cfg.find(b"\x00\x00\x01", i + 3)
    return False
