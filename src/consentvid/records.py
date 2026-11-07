"""Small, dependency-light helpers shared by every module."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

VIDEO_NSID = "org.consentvid.video"
CONSENT_NSID = "org.consentvid.consent"

# Syntax per atproto.com/specs (did, nsid, record-key). Deliberately strict:
# these strings end up in public records and manifests.
# Always used with fullmatch: '$' also matches before a trailing newline.
_DID_RE = re.compile(r"did:[a-z]+:[a-zA-Z0-9._:%-]*[a-zA-Z0-9._-]")
_NSID_RE = re.compile(
    r"[a-zA-Z]([a-zA-Z0-9-]{0,62})?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})?)+"
    r"\.[a-zA-Z]([a-zA-Z0-9]{0,62})?"
)
_RKEY_RE = re.compile(r"[a-zA-Z0-9._:~-]{1,512}")
_HANDLE_RE = re.compile(
    r"([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
)


# Strict IPFS CID syntax: CIDv1 in lowercase base32 ('b...') or CIDv0 ('Qm...').
# Anything else (paths, '../', query strings) must never be interpolated into
# a gateway or pinning-service URL.
_IPFS_CID_RE = re.compile(r"b[a-z2-7]{20,}|Qm[1-9A-HJ-NP-Za-km-z]{44}")


def is_ipfs_cid(s) -> bool:
    return isinstance(s, str) and bool(_IPFS_CID_RE.fullmatch(s))


def has_control_or_space(s: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 for ch in s)


# Characters that can change how a terminal (or a reader) displays text:
# C0/C1 controls (ESC, CR, BEL, ...), format characters (bidi overrides,
# zero-width joiners) and the Unicode line/paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def has_unsafe_chars(s: str) -> bool:
    return any(unicodedata.category(ch) in _UNSAFE_CATEGORIES for ch in s)


def safe_text(s) -> str:
    """``s`` (anything; non-strings via str()) with every control, format and
    line-separator character replaced by a visible escape (\\x1b, \\u202e).
    Use for every untrusted string printed to a terminal."""
    if not isinstance(s, str):
        s = str(s)
    out = []
    for ch in s:
        if unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            o = ord(ch)
            out.append(f"\\x{o:02x}" if o < 0x100 else (f"\\u{o:04x}" if o < 0x10000 else f"\\U{o:08x}"))
        else:
            out.append(ch)
    return "".join(out)


_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".arpa", ".lan", ".home", ".corp", ".intranet")
_HEX_LABEL_RE = re.compile(r"0x[0-9a-f]*")
# A host name as an HTTP client will contact it: ASCII letters, digits and
# hyphens only (internationalised names in xn-- punycode form). Anything else
# ('%3F', '#', '@', U+3002 and other full stops that IDNA maps to '.') makes
# the string we check differ from the host the client connects to.
_LDH_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def is_ldh_host(host) -> bool:
    """True if ``host`` (a trailing dot allowed) is a pure ASCII LDH name:
    labels of 1-63 letters/digits/hyphens, no leading or trailing hyphen."""
    if not isinstance(host, str) or not host.isascii():
        return False
    h = host.lower()
    h = h[:-1] if h.endswith(".") else h
    return bool(h) and len(h) <= 253 and all(_LDH_LABEL_RE.fullmatch(lab) for lab in h.split("."))


def check_public_host(host: str | None, *, resolve: bool = False) -> None:
    """Raise ValueError unless ``host`` is a public DNS name.

    Refuses IP literals in every spelling a URL parser or ``inet_aton``
    accepts (127.1, 0x7f.0.0.1, 2130706433, [::1]), names whose last label is
    numeric (WHATWG "ends in a number": parsed as IPv4), localhost and
    private-use suffixes. With ``resolve=True`` the name is also looked up and
    refused if ANY address it resolves to is not globally routable. A failed
    lookup is ignored (the request fails anyway). Residual: DNS rebinding
    between this check and the connection is not prevented."""
    if not host or not isinstance(host, str):
        raise ValueError("no host")
    h = host.lower().rstrip(".")
    bare = h.strip("[]")
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        raise ValueError(f"IP address hosts are not allowed: {host}")
    try:
        socket.inet_aton(bare)
    except (OSError, ValueError):
        pass
    else:
        raise ValueError(f"IP address hosts are not allowed: {host}")
    last = h.rsplit(".", 1)[-1]
    if last.isdigit() or _HEX_LABEL_RE.fullmatch(last):
        raise ValueError(f"host ends in a number (parsed as an IP address): {host}")
    if h == "localhost" or h.endswith(_PRIVATE_SUFFIXES) or "." not in h:
        raise ValueError(f"non-public host: {host}")
    if any(ch in h for ch in "[]%/\\@ ") or has_control_or_space(h) or not is_ldh_host(host):
        raise ValueError(f"malformed host: {host!r}")
    if resolve:
        try:
            infos = socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError, ValueError):
            return
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
            except ValueError:
                raise ValueError(f"{host} resolves to an unparseable address {addr!r}") from None
            if not ip.is_global:
                raise ValueError(f"{host} resolves to a non-public address {addr}")


def url_host_port_error(value: str) -> str | None:
    """Why the https URL ``value`` doesn't name a public host on the default
    port (no DNS lookup), or None."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(value)
        host, port = u.hostname, u.port
    except ValueError as e:
        return f"is not a valid URL ({e})"
    if port not in (None, 443):
        return f"must not name a port other than 443 (got {port})"
    try:
        check_public_host(host)
    except ValueError as e:
        return f"must name a public host ({e})"
    return None


def https_url_error(value) -> str | None:
    """Why ``value`` is not a plain public https URL, or None if it is."""
    from urllib.parse import urlsplit

    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if has_control_or_space(value):
        return "must not contain whitespace or control characters"
    if not value.isascii():
        # IDNA maps U+3002 etc. to '.', so the host a client contacts would
        # differ from the one checked. Give internationalised names as xn--.
        return "must be ASCII (use xn-- punycode for internationalised names)"
    try:
        u = urlsplit(value)
        host = u.hostname
        userinfo = u.username is not None or u.password is not None
        u.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError as e:
        return f"is not a valid URL ({e})"
    if u.scheme != "https":
        return "must be an https:// URL"
    if not host:
        return "must name a host"
    if userinfo:
        return "must not contain credentials (user@host)"
    return None


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
    if not _DID_RE.fullmatch(did):
        raise ValueError(f"AT-URI authority must be a DID: {uri!r}")
    if not _NSID_RE.fullmatch(coll):
        raise ValueError(f"invalid collection NSID in {uri!r}")
    if not _RKEY_RE.fullmatch(rkey) or rkey in (".", ".."):
        raise ValueError(f"invalid record key in {uri!r}")
    if collection and coll != collection:
        raise ValueError(f"expected a {collection} record, got {coll}: {uri}")
    return AtUri(did, coll, rkey)


def is_did(s: str) -> bool:
    return isinstance(s, str) and bool(_DID_RE.fullmatch(s))


def is_handle(s: str) -> bool:
    return isinstance(s, str) and bool(_HANDLE_RE.fullmatch(s))


def now_iso() -> str:
    """Current UTC time as an atproto datetime (millisecond precision, 'Z')."""
    return format_dt(datetime.now(UTC))


def format_dt(dt: datetime) -> str:
    """Canonical, fixed-width UTC form. Fixed width makes stored timestamps
    lexicographically comparable (the AppView relies on this). Built field by
    field: strftime('%Y') is not zero-padded for years < 1000 on glibc."""
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:"
        f"{dt.second:02d}.{dt.microsecond // 1000:03d}Z"
    )


_FRACTION_RE = re.compile(r"\.(\d+)(?=[+-]\d\d:\d\d$)")
# RFC 3339 profile required by atproto (atproto.com/specs/lexicon#datetime):
# uppercase 'T' and 'Z', whole seconds required, explicit offset, no '-00:00'.
# Anything else Python's fromisoformat would also take (ISO week dates, basic
# format, hour-only, comma fractions, second offsets) is refused.
_RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,20})?(Z|[+-]\d{2}:\d{2})", re.ASCII)


def parse_dt(s) -> datetime | None:
    """Parse an atproto datetime. Requires an explicit timezone (per spec);
    returns None for anything else."""
    if not isinstance(s, str) or not _RFC3339_RE.fullmatch(s) or s.endswith("-00:00"):
        return None
    if int(s[:4]) < 1000:
        return None
    s = s.replace("Z", "+00:00")
    # Python 3.10's fromisoformat only takes 3- or 6-digit fractions; pad/trim.
    s = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    try:
        # e.g. 9999-12-31T23:59:59-05:00 parses but overflows in UTC; reject
        # it here so normalize_dt/format_dt can never raise on parsed values.
        dt.astimezone(UTC)
    except (OverflowError, ValueError):
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


# The only values a video record's mimeType may take. It is copied into
# manifests as encodingFormat, which Croissant loaders act on (e.g.
# 'git+https' makes mlcroissant git-clone the URL and skip the hash check).
VIDEO_MIME_TYPES = frozenset(_MIME_BY_SUFFIX.values())


def mime_for(path_or_name) -> str | None:
    return _MIME_BY_SUFFIX.get(Path(str(path_or_name)).suffix.lower())
