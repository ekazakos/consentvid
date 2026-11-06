"""Media inspection and privacy sanitisation (ffprobe / ffmpeg).

Phones embed GPS coordinates, device make/model and sometimes serial numbers in
video containers — Apple even writes a timed-metadata track ("mebx") with
location. Once uploaded to public storage those bytes are effectively
permanent, so ConsentVid strips identifying metadata *before* hashing and
uploading, by default. Stripping is a lossless stream copy (no re-encode).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from consentvid.errors import ConfigError, ConsentVidError

# Tag-name patterns that identify a person, place or device.
_SENSITIVE_TAG = re.compile(
    r"(location|gps|xyz|iso6709|latitude|longitude|make|model|serial|device|"
    r"owner|artist|author|camera|lens|user|email|phone)",
    re.IGNORECASE,
)


@dataclass
class Probe:
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    sensitive_tags: dict[str, str] = field(default_factory=dict)
    extra_streams: list[str] = field(default_factory=list)  # data/attachment/subtitle

    @property
    def needs_sanitising(self) -> bool:
        return bool(self.sensitive_tags or self.extra_streams)

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


def probe(path: Path) -> Probe | None:
    """Inspect a video. Returns None if ffprobe is unavailable."""
    if not have_ffprobe():
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise ConsentVidError(f"ffprobe could not read {path.name}: {e.stderr.strip()}") from e
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    p = Probe()
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ConsentVidError(f"{path.name} has no video stream")
    p.width, p.height = video.get("width"), video.get("height")
    dur = video.get("duration") or fmt.get("duration")
    if dur not in (None, "N/A"):
        p.duration_ms = int(round(float(dur) * 1000))

    def scan(tags: dict, where: str):
        for k, v in (tags or {}).items():
            if _SENSITIVE_TAG.search(k):
                p.sensitive_tags[f"{where}:{k}"] = str(v)[:80]

    scan(fmt.get("tags"), "format")
    for s in streams:
        idx = s.get("index")
        scan(s.get("tags"), f"stream{idx}")
        if s.get("codec_type") not in ("video", "audio"):
            p.extra_streams.append(f"stream{idx}:{s.get('codec_type')}:{s.get('codec_tag_string', '')}")
    return p


def sanitise(src: Path, dst: Path) -> Probe:
    """Copy video+audio streams to dst with all metadata, chapters and
    non-A/V streams removed. Returns the probe of the result, which is
    guaranteed clean."""
    if not have_ffmpeg():
        raise ConfigError("ffmpeg is required to strip identifying metadata (brew install ffmpeg)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v",
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
    if dst.suffix.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]  # moov first: streamable over HTTP
    cmd.append(str(dst))
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600)
    except subprocess.CalledProcessError as e:
        raise ConsentVidError(f"ffmpeg failed to sanitise {src.name}: {e.stderr.strip()}") from e
    result = probe(dst)
    if result is None or result.needs_sanitising:
        leftover = result.sensitive_tags if result else "unknown"
        raise ConsentVidError(f"metadata still present after sanitising: {leftover}")
    return result
