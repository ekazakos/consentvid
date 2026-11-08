"""Bluesky posts as a source of video bytes (videoV2 ``#bskyPostRef``).

A contributor who posted a video on Bluesky already has the bytes on their
PDS: the post (``app.bsky.feed.post``) embeds them as a blob
(``app.bsky.embed.video``, directly or as the media of
``app.bsky.embed.recordWithMedia``), and the PDS serves them through
``com.atproto.sync.getBlob``. A videoV2 record can point at that post instead
of re-hosting the bytes:

    storage = {"$type": "org.consentvid.videoV2#bskyPostRef",
               "post": {"uri": "at://<did>/app.bsky.feed.post/<rkey>", "cid": "<post cid>"},
               "blob": "<video blob cid>"}            # == contentHash

The internal view (versions.py) spells it ``storageRef = {"kind": "bsky",
"value": <blob cid>, "post": {"uri", "cid"}}``.

Why not a ``blob``-typed field in our own record: a blob referenced by our
record would outlive the post (the PDS keeps a blob while ANY record
references it). Pointing at the post keeps the bytes' lifetime the post's:
delete the post and the bytes are gone, and readers report the clip
UNAVAILABLE.

Reader rules (enforced by validate.py and check_post):
* post.uri is an app.bsky.feed.post in the SAME repo as the video record
  (nobody can register someone else's post);
* blob == contentHash (a blob CID is the raw-codec sha-256 of the bytes);
* the post, read with a proof, still exists at post.cid and embeds exactly
  that blob.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from consentvid.errors import ConsentVidError
from consentvid.records import AtUri, is_did, is_handle, is_record_key, parse_at_uri

POST_NSID = "app.bsky.feed.post"
VIDEO_EMBED = "app.bsky.embed.video"
RECORD_WITH_MEDIA = "app.bsky.embed.recordWithMedia"
KIND = "bsky"  # internal storageRef kind (versions.py)
GET_BLOB = "/xrpc/com.atproto.sync.getBlob"
# Bluesky accepts videos up to 300 MB (social-app VIDEO_MAX_SIZE); other
# PDSes may allow more. A download stops here whatever the record claims.
MAX_BLOB_BYTES = 1024 * 1024 * 1024
_APP_HOSTS = ("bsky.app", "www.bsky.app")
_transport = None  # httpx transport for download_blob (tests inject a MockTransport)


@dataclass(frozen=True)
class VideoBlob:
    cid: str
    mime_type: str | None
    size: int | None


def video_blob_of(post: dict) -> VideoBlob | None:
    """The video blob an app.bsky.feed.post record embeds, or None."""
    if not isinstance(post, dict):
        return None
    embed = post.get("embed")
    if not isinstance(embed, dict):
        return None
    if embed.get("$type") == RECORD_WITH_MEDIA:
        embed = embed.get("media")
        if not isinstance(embed, dict):
            return None
    if embed.get("$type") != VIDEO_EMBED:
        return None
    blob = embed.get("video")
    if not isinstance(blob, dict):
        return None
    cid = _link(blob.get("ref"))
    if not cid:
        return None
    mime = blob.get("mimeType")
    size = blob.get("size")
    return VideoBlob(
        cid,
        mime if isinstance(mime, str) else None,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


def _link(ref) -> str | None:
    """A CID link as a base32 string, from any of the shapes a record can
    arrive in: atproto JSON ({"$link": ...}, from listRecords / Jetstream), a
    decoded DAG-CBOR link (binary CID bytes, from a verified CAR read), or a
    plain string."""
    if isinstance(ref, dict):
        ref = ref.get("$link")
    if isinstance(ref, (bytes, bytearray)):
        try:
            import libipld

            return libipld.encode_cid(bytes(ref))
        except Exception:  # noqa: BLE001
            return None
    return ref if isinstance(ref, str) and ref else None


def post_text(post: dict) -> str:
    text = post.get("text") if isinstance(post, dict) else None
    return text if isinstance(text, str) else ""


def parse_post_ref(ref: str) -> tuple[str, str]:
    """An at:// post URI or a https://bsky.app/profile/<handle-or-did>/post/<rkey>
    link -> (handle-or-did, rkey). Raises ValueError."""
    ref = (ref or "").strip()
    if ref.startswith("at://"):
        at = parse_at_uri(ref, POST_NSID)
        return at.did, at.rkey
    u = urlsplit(ref)
    if u.scheme != "https" or (u.hostname or "").lower() not in _APP_HOSTS:
        raise ValueError("expected an at://…/app.bsky.feed.post/… URI or a https://bsky.app/profile/…/post/… link")
    parts = [unquote(p) for p in u.path.split("/") if p]
    if len(parts) != 4 or parts[0] != "profile" or parts[2] != "post":
        raise ValueError("expected a https://bsky.app/profile/<handle>/post/<id> link")
    actor, rkey = parts[1], parts[3]
    if not (is_did(actor) or is_handle(actor.lower())):
        raise ValueError(f"{actor!r} is neither a handle nor a DID")
    if not is_record_key(rkey):
        raise ValueError(f"{rkey!r} is not a record key")
    return (actor if is_did(actor) else actor.lower()), rkey


def resolve_post_uri(ref: str) -> AtUri:
    """parse_post_ref, with a handle resolved to its DID."""
    from consentvid.identity import resolve_did

    actor, rkey = parse_post_ref(ref)
    did = actor if is_did(actor) else resolve_did(actor)
    return parse_at_uri(f"at://{did}/{POST_NSID}/{rkey}", POST_NSID)


def pds_of(did: str) -> str:
    """The PDS hosting ``did`` (cached DID resolution). Monkeypatched in tests."""
    from consentvid.identity import resolve_identity

    return resolve_identity(did).pds


def blob_url(pds: str, did: str, cid: str) -> str:
    """The getBlob URL for a blob. The query is written exactly this way
    (resync compares it)."""
    return f"{pds.rstrip('/')}{GET_BLOB}?did={did}&cid={cid}"


def post_did(ref: dict) -> str | None:
    post = ref.get("post") if isinstance(ref, dict) else None
    uri = post.get("uri") if isinstance(post, dict) else None
    try:
        return parse_at_uri(uri, POST_NSID).did if isinstance(uri, str) else None
    except ValueError:
        return None


def url_for_ref(ref: dict) -> str:
    """The getBlob URL for an internal ``bsky`` storageRef. Raises on a bad
    ref or a failed DID resolution."""
    did = post_did(ref)
    if did is None or not isinstance(ref.get("value"), str) or not ref["value"]:
        raise ValueError("not a bsky storage reference")
    return blob_url(pds_of(did), did, ref["value"])


_QUERY_RE = re.compile(r"did=(did:[a-z]+:[A-Za-z0-9._:%-]+)&cid=([a-z2-7]+)")


def url_matches(url: str, ref: dict) -> bool:
    """Does ``url`` denote this ref's blob? Any public https host serving
    getBlob for exactly this DID and CID: the PDS may change (account
    migration) without the bytes changing, and the sha256 in the manifest
    binds the bytes whichever host serves them."""
    return blob_url_matches(url, post_did(ref), ref.get("value") if isinstance(ref, dict) else None)


def blob_url_matches(url: str, did: str | None, cid: str | None) -> bool:
    """Does ``url`` denote blob ``cid`` of repo ``did`` (see url_matches)?"""
    from consentvid.records import check_public_host

    if not did or not cid or not isinstance(url, str):
        return False
    try:
        u = urlsplit(url)
        userinfo = u.username is not None or u.password is not None
        port = u.port
        check_public_host(u.hostname)
    except ValueError:
        return False
    m = _QUERY_RE.fullmatch(u.query or "")
    return (
        u.scheme == "https"
        and not userinfo
        and port in (None, 443)
        and not u.fragment
        and u.path == GET_BLOB
        and m is not None
        and m.group(1) == did
        and m.group(2) == cid
        # mlcroissant special-cases these hosts (git clone): never one of them.
        and not u.netloc.lower().startswith(("github.com", "gitlab.com", "huggingface.co"))
    )


# ------------------------------------------------------------ checks ------


def check_post(ref: dict, repo_did: str, fetch) -> tuple[str, str] | None:
    """Verify the post behind a ``bsky`` storageRef with ``fetch`` (a
    verified read, e.g. verify.fetch_verified). Returns None if the post
    still exists at the pinned CID and embeds the blob, else (problem,
    reason) with problem one of 'gone', 'modified', 'invalid'. Network,
    proof and repo-status errors propagate (the caller classifies them)."""
    post = ref.get("post") if isinstance(ref, dict) else None
    if not isinstance(post, dict):
        return "invalid", "storage has no post reference"
    try:
        at = parse_at_uri(post.get("uri") or "", POST_NSID)
    except ValueError as e:
        return "invalid", f"bad post URI: {e}"
    if at.did != repo_did:
        return "invalid", "the post belongs to another account"
    got = fetch(at)
    if not got.present:
        return "gone", "the Bluesky post with the video was deleted (its bytes are gone)"
    if got.cid != post.get("cid"):
        return "modified", "the Bluesky post was rewritten after the video was registered"
    blob = video_blob_of(got.value)
    if blob is None or blob.cid != ref.get("value"):
        return "invalid", "the Bluesky post doesn't embed this video"
    return None


# ---------------------------------------------------------- download ------


def download_blob(did: str, cid: str, dest_dir: Path, *, max_bytes: int = MAX_BLOB_BYTES) -> Path:
    """Download a blob from ``did``'s PDS into ``dest_dir`` and check that
    its bytes hash to ``cid`` (raw codec, sha-256). Redirects are not
    followed. Returns the file path. Raises ConsentVidError on a mismatch or
    an oversized answer."""
    import httpx

    from consentvid.versions import content_hash_of_cid

    want = content_hash_of_cid(cid)
    if want is None:
        raise ConsentVidError(f"blob {cid} is not a raw sha-256 CID")
    url = blob_url(pds_of(did), did, cid)
    fd = tempfile.NamedTemporaryFile(dir=dest_dir, suffix=".blob", delete=False)  # noqa: SIM115
    path = Path(fd.name)
    h, n = hashlib.sha256(), 0
    try:
        with fd, httpx.Client(follow_redirects=False, timeout=60.0, transport=_transport) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ConsentVidError(f"the PDS answered {resp.status_code} for the video blob")
                for chunk in resp.iter_bytes():
                    n += len(chunk)
                    if n > max_bytes:
                        raise ConsentVidError(f"the video blob is larger than {max_bytes} bytes")
                    h.update(chunk)
                    fd.write(chunk)
        if "sha256-" + h.hexdigest() != want:
            raise ConsentVidError("the PDS served bytes that don't match the post's video CID")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def default_title(post: dict, limit: int = 300, max_bytes: int = 3000) -> str:
    """A title from the post's text: its first non-empty line, with control
    and format characters dropped and whitespace collapsed, at most
    ``limit`` graphemes and ``max_bytes`` UTF-8 bytes (videoV2's title
    limits). 'Bluesky video' if nothing is left."""
    import unicodedata

    import regex

    for line in post_text(post).splitlines():
        line = "".join(ch for ch in line if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
        line = " ".join(line.split())
        if not line:
            continue
        out, size = [], 0
        for g in regex.findall(r"\X", line)[:limit]:
            size += len(g.encode())
            if size > max_bytes:
                break
            out.append(g)
        return "".join(out)
    return "Bluesky video"
