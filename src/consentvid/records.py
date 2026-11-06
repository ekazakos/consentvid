"""Small, dependency-light helpers shared by every module."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

VIDEO_NSID = "org.consentvid.video"
CONSENT_NSID = "org.consentvid.consent"

# Syntax per atproto.com/specs (did, nsid, record-key). Deliberately strict:
# these strings end up in public records and manifests.
_DID_RE = re.compile(r"^did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-]$")
_NSID_RE = re.compile(
    r"^[a-zA-Z]([a-zA-Z0-9-]{0,62})?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})?)+"
    r"\.[a-zA-Z]([a-zA-Z0-9]{0,62})?$"
)
_RKEY_RE = re.compile(r"^[a-zA-Z0-9._:~-]{1,512}$")
_HANDLE_RE = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


@dataclass(frozen=True)
class AtUri:
    did: str
    collection: str
    rkey: str

    def __str__(self) -> str:
        return f"at://{self.did}/{self.collection}/{self.rkey}"


def parse_at_uri(uri: str, collection: str | None = None) -> AtUri:
    """Parse a record AT-URI (at://<did>/<nsid>/<rkey>). Raises ValueError.

    Only DID authorities are accepted: a handle-based URI is mutable (handles
    change hands), which is unacceptable for a consent reference.
    """
    if not isinstance(uri, str) or not uri.startswith("at://"):
        raise ValueError(f"not an at:// URI: {uri!r}")
    parts = uri[len("at://") :].split("/")
    if len(parts) != 3:
        raise ValueError(f"expected at://<did>/<collection>/<rkey>: {uri!r}")
    did, coll, rkey = parts
    if not _DID_RE.match(did):
        raise ValueError(f"AT-URI authority must be a DID: {uri!r}")
    if not _NSID_RE.match(coll):
        raise ValueError(f"invalid collection NSID in {uri!r}")
    if not _RKEY_RE.match(rkey) or rkey in (".", ".."):
        raise ValueError(f"invalid record key in {uri!r}")
    if collection and coll != collection:
        raise ValueError(f"expected a {collection} record, got {coll}: {uri}")
    return AtUri(did, coll, rkey)


def is_did(s: str) -> bool:
    return bool(_DID_RE.match(s or ""))


def is_handle(s: str) -> bool:
    return bool(_HANDLE_RE.match(s or ""))


def now_iso() -> str:
    """Current UTC time as an atproto datetime (millisecond precision, 'Z')."""
    return format_dt(datetime.now(UTC))


def format_dt(dt: datetime) -> str:
    """Canonical, fixed-width UTC form. Fixed width makes stored timestamps
    lexicographically comparable (the AppView relies on this)."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_FRACTION_RE = re.compile(r"\.(\d+)(?=[+-]\d\d:\d\d$)")


def parse_dt(s) -> datetime | None:
    """Parse an atproto datetime. Requires an explicit timezone (per spec);
    returns None for anything else."""
    if not isinstance(s, str) or "T" not in s:
        return None
    s = s.replace("Z", "+00:00").replace("z", "+00:00")
    # Python 3.10's fromisoformat only takes 3- or 6-digit fractions; pad/trim.
    s = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def normalize_dt(s) -> str | None:
    dt = parse_dt(s)
    return format_dt(dt) if dt else None


def is_expired(record: dict, now: datetime | None = None) -> bool:
    exp = parse_dt(record.get("expiresAt"))
    return bool(exp and exp <= (now or datetime.now(UTC)))


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256-{h.hexdigest()}"


def to_dict(value):
    """Recursively coerce an atproto SDK record value to plain JSON types.

    The SDK returns Pydantic models for known lexicons and DotDict wrappers for
    unknown ones. DotDict.__getattr__ returns None for missing keys, so a
    hasattr() check for model_dump lies — check callable instead.
    """
    mdump = getattr(value, "model_dump", None)
    if callable(mdump):
        return mdump(by_alias=True, exclude_none=True)
    to_d = getattr(value, "to_dict", None)
    if callable(to_d):
        return to_dict(to_d())
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {k: to_dict(v) for k, v in dict(value).items()}


_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".3gp": "video/3gpp",
}


def mime_for(path_or_name) -> str | None:
    return _MIME_BY_SUFFIX.get(Path(str(path_or_name)).suffix.lower())
