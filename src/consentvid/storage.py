"""Storage backends for video bytes.

ConsentVid is non-custodial: a video record commits to a SHA-256 content hash
and a ``storageRef`` pointer; the bytes live wherever the contributor chooses.

  r2        Cloudflare R2 (S3-compatible, zero egress). Revocable: the object
            can be deleted.
  ipfs      Content-addressed P2P storage. NOT reliably revocable: unpinning
            stops *your* node/pinning service from serving the bytes, but any
            other node or gateway that fetched them may keep a copy.
            Note the CID is the root of a UnixFS DAG, not the SHA-256 of the
            file — integrity is still checked against the record's contentHash.
  peertube  Federated video host (transcoding, adaptive streaming). The URL is
            a watch page and served renditions are transcodes, so they won't
            match contentHash. Uploaded unlisted by default.
  url       A URL the contributor hosts and manages themselves.
  local     Not published anywhere; an opaque name for the contributor's own
            bookkeeping. Never dereferenceable by others.

Uploaders return the string for ``storageRef.value``; ``public_url`` turns a
``storageRef`` back into a URL for manifests; ``delete_bytes`` removes the
bytes where the backend allows it, and only when the storageRef is exactly the
content-addressed object this clip's upload would have created (records come
from a PDS, which is untrusted, and must never steer a delete elsewhere).

``--delete-bytes`` assumes the bucket / pinning account / PeerTube channel is
yours alone: on storage shared by several contributors, identical bytes share
one object and deleting it removes it for everyone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from consentvid.errors import ConfigError, ConsentVidError
from consentvid.records import https_url_error, is_ipfs_cid, mime_for

UPLOAD_KINDS = ("r2", "ipfs", "peertube")
_CONTENT_HASH_RE = re.compile(r"sha256-[0-9a-f]{64}")
_PEERTUBE_SHORT_RE = re.compile(r"[A-Za-z0-9-]{8,40}")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class Upload:
    value: str  # storageRef.value
    created: bool  # False if the object already existed (we must not delete it on cleanup)


def require_https(name: str, url: str) -> str:
    """Credentials and uploads only go to https:// endpoints. Plain http is
    allowed for localhost only, and only when CONSENTVID_ALLOW_INSECURE=1 is
    set in the real environment (never from an env file)."""
    if https_url_error(url) is None:
        return url
    try:
        u = urlsplit(url)
        host = u.hostname
    except ValueError:
        u, host = None, None
    if (
        u is not None
        and u.scheme == "http"
        and host in _LOOPBACK_HOSTS
        and os.environ.get("CONSENTVID_ALLOW_INSECURE") == "1"
    ):
        return url
    raise ConfigError(f"{name} must be an https:// URL (got {url!r})")


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name) or default
    if not val:
        raise ConfigError(f"missing required setting {name} (see .env.example)")
    return val


def _httpx():
    try:
        import httpx
    except ImportError as e:
        raise ConfigError('httpx is required for this backend: pip install "consentvid[ipfs]"') from e
    return httpx


def configured(kind: str) -> bool:
    """Whether the env has what this backend needs to upload."""
    need = {
        "r2": [
            "CONSENTVID_R2_ENDPOINT",
            "CONSENTVID_R2_BUCKET",
            "CONSENTVID_R2_ACCESS_KEY_ID",
            "CONSENTVID_R2_SECRET_ACCESS_KEY",
            "CONSENTVID_R2_PUBLIC_URL",
        ],
        "peertube": [
            "CONSENTVID_PEERTUBE_URL",
            "CONSENTVID_PEERTUBE_USERNAME",
            "CONSENTVID_PEERTUBE_PASSWORD",
        ],
    }.get(kind)
    if kind == "ipfs":  # a pinning service, or an explicitly configured Kubo node
        return bool(os.environ.get("CONSENTVID_PINATA_JWT") or os.environ.get("CONSENTVID_IPFS_API"))
    return need is not None and all(os.environ.get(n) for n in need)


def public_url(storage_ref: dict) -> str | None:
    """A dereferenceable https URL for a storageRef, or None (``local``,
    unknown kinds, or a value that fails the per-kind syntax rules)."""
    from consentvid.validate import storage_ref_errors

    if not isinstance(storage_ref, dict):
        return None
    kind, value = storage_ref.get("kind"), storage_ref.get("value")
    if kind not in ("url", "r2", "peertube", "ipfs", "bsky", "pds") or not isinstance(value, str) or not value:
        return None
    if storage_ref_errors(storage_ref):
        return None
    if kind == "pds":
        # A blob in the contributor's own PDS: the view carries the repo DID
        # (versions.video_view(..., did)); without it, nothing to resolve.
        from consentvid import bsky

        did = storage_ref.get("did")
        if not isinstance(did, str) or not did:
            return None
        try:
            return bsky.blob_url(bsky.pds_of(did), did, value)
        except Exception:  # noqa: BLE001
            return None
    if kind == "bsky":
        # The contributor's PDS serves the post's video blob (DID resolution:
        # network; None if it fails).
        from consentvid import bsky

        try:
            return bsky.url_for_ref(storage_ref)
        except Exception:  # noqa: BLE001
            return None
    if kind == "ipfs":
        gateway = require_https(
            "CONSENTVID_IPFS_GATEWAY", os.environ.get("CONSENTVID_IPFS_GATEWAY") or "https://ipfs.io"
        ).rstrip("/")
        return f"{gateway}/ipfs/{value.removeprefix('ipfs://')}"
    return value


def upload(kind: str, path: Path, *, content_hash: str, title: str) -> Upload:
    if kind not in UPLOAD_KINDS:
        raise ConsentVidError(f"backend {kind!r} does not support uploads")
    if not configured(kind):
        raise ConfigError(f"storage backend {kind!r} is not configured (see .env.example)")
    if kind == "r2":
        return upload_to_r2(path, content_hash)
    if kind == "ipfs":
        return add_to_ipfs(path)
    return Upload(upload_to_peertube(path, title, content_hash), True)


def delete_bytes(storage_ref: dict, content_hash: str) -> str:
    """Remove the bytes behind a storageRef where possible. Returns a
    human-readable description of what happened. Raises ConsentVidError when
    the storageRef is not exactly this clip's own object."""
    kind, value = storage_ref.get("kind"), storage_ref.get("value", "")
    if not isinstance(value, str):
        raise ConsentVidError("storageRef.value is not a string; not deleting")
    if kind == "r2":
        return _delete_r2(value, content_hash)
    if kind == "ipfs":
        return _unpin_ipfs(value.removeprefix("ipfs://"))
    if kind == "peertube":
        return _delete_peertube(value, content_hash)
    if kind == "url":
        return f"bytes at {value} are self-hosted: delete them yourself"
    if kind == "pds":
        return (
            "the bytes are a blob in your own PDS: it deletes them once no record references "
            "them any more (deleting this video record does that)"
        )
    if kind == "bsky":
        post = (storage_ref.get("post") or {}).get("uri")
        return f"the bytes are your Bluesky post's video: delete the post ({post}) on Bluesky to remove them"
    return "nothing to delete (local)"


# ------------------------------------------------------------------- r2 ------


def _r2():
    try:
        import boto3
    except ImportError as e:
        raise ConfigError('boto3 is required for R2: pip install "consentvid[r2]"') from e
    return boto3.client(
        "s3",
        endpoint_url=_env("CONSENTVID_R2_ENDPOINT"),
        aws_access_key_id=_env("CONSENTVID_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("CONSENTVID_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _r2_public_base() -> str:
    return require_https("CONSENTVID_R2_PUBLIC_URL", _env("CONSENTVID_R2_PUBLIC_URL")).rstrip("/")


def upload_to_r2(local_path: Path, content_hash: str) -> Upload:
    """Upload to R2 under a content-addressed key; returns the public URL and
    whether this call created the object. Idempotent: an object already stored
    under that hash is reused (created=False)."""
    if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
        raise ConsentVidError(f"not a content hash: {content_hash!r}")
    bucket = _env("CONSENTVID_R2_BUCKET")
    public_base = _r2_public_base()
    key = f"videos/{content_hash}"
    s3 = _r2()
    created = False
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        # Only a definite "not found" means the object is ours to create (and
        # to delete again if the record write fails). A 403/5xx/timeout says
        # nothing about whether someone else's object is there: stop.
        if not _is_not_found(e):
            raise
        s3.upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={"ContentType": mime_for(local_path) or "application/octet-stream"},
        )
        created = True
    return Upload(f"{public_base}/{key}", created)


def _is_not_found(e: BaseException) -> bool:
    try:
        from botocore.exceptions import ClientError
    except ImportError:
        return False
    if not isinstance(e, ClientError):
        return False
    resp = e.response or {}
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    code = str((resp.get("Error") or {}).get("Code", ""))
    return status == 404 or code in ("404", "NoSuchKey", "NotFound")


def _delete_r2(url: str, content_hash: str) -> str:
    if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
        raise ConsentVidError(f"not a content hash: {content_hash!r}; not deleting")
    key = f"videos/{content_hash}"
    if url != f"{_r2_public_base()}/{key}":
        raise ConsentVidError(
            f"storageRef {url!r} does not point at this clip's content-addressed object "
            f"({key} under CONSENTVID_R2_PUBLIC_URL); not deleting"
        )
    _r2().delete_object(Bucket=_env("CONSENTVID_R2_BUCKET"), Key=key)
    return f"deleted R2 object {key} (CDN caches may serve it briefly)"


# ------------------------------------------------------------------ ipfs -----


def add_to_ipfs(local_path: Path) -> Upload:
    """Add and pin a file; returns its CID and whether this call created the
    pin. Uses Pinata if CONSENTVID_PINATA_JWT is set, else the Kubo node at
    CONSENTVID_IPFS_API (which must be set explicitly).

    ``created`` fails closed: it is True only when the service positively says
    the pin is new (Pinata isDuplicate == false; Kubo pin/ls 'not pinned'
    before the add), so a failed register never unpins someone else's pin."""
    httpx = _httpx()
    jwt = os.environ.get("CONSENTVID_PINATA_JWT")
    if jwt:
        with local_path.open("rb") as f:
            resp = httpx.post(
                "https://api.pinata.cloud/pinning/pinFileToIPFS",
                headers={"Authorization": f"Bearer {jwt}"},
                files={"file": (local_path.name, f)},
                data={"pinataOptions": json.dumps({"cidVersion": 1})},
                timeout=None,
            )
        resp.raise_for_status()
        body = resp.json()
        return Upload(_checked_cid(body["IpfsHash"]), body.get("isDuplicate") is False)
    api = os.environ.get("CONSENTVID_IPFS_API")
    if not api:
        raise ConfigError("IPFS is not configured: set CONSENTVID_PINATA_JWT or CONSENTVID_IPFS_API")
    api = api.rstrip("/")

    def add(**extra):
        with local_path.open("rb") as f:
            resp = httpx.post(
                f"{api}/api/v0/add",
                params={"cid-version": "1", **extra},
                files={"file": (local_path.name, f)},
                timeout=None,
            )
        resp.raise_for_status()
        return _checked_cid(json.loads(resp.text.strip().splitlines()[-1])["Hash"])

    try:
        cid = add(**{"only-hash": "true", "pin": "false"})  # the CID, without storing anything
    except httpx.ConnectError as e:
        raise ConfigError(f"no IPFS node at {api}: run `ipfs daemon` or set CONSENTVID_PINATA_JWT") from e
    created = False
    try:
        ls = httpx.post(f"{api}/api/v0/pin/ls", params={"arg": cid, "type": "recursive"}, timeout=60)
        if ls.status_code >= 400:
            try:
                message = str(ls.json().get("Message", ""))
            except ValueError:
                message = ""
            created = "not pinned" in message
    except httpx.HTTPError:
        created = False  # can't tell: never delete it later
    pinned = add(pin="true")
    return Upload(pinned, created and pinned == cid)


def _checked_cid(cid) -> str:
    if not is_ipfs_cid(cid):
        raise ConsentVidError(f"IPFS service returned an unexpected CID: {cid!r}")
    return cid


def _unpin_ipfs(cid: str) -> str:
    if not is_ipfs_cid(cid):
        raise ConsentVidError(f"{cid!r} is not a CID; not unpinning")
    httpx = _httpx()
    jwt = os.environ.get("CONSENTVID_PINATA_JWT")
    if jwt:
        resp = httpx.delete(
            f"https://api.pinata.cloud/pinning/unpin/{quote(cid, safe='')}",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=60,
        )
    else:
        api = os.environ.get("CONSENTVID_IPFS_API")
        if not api:
            raise ConfigError("IPFS is not configured: set CONSENTVID_PINATA_JWT or CONSENTVID_IPFS_API")
        resp = httpx.post(f"{api.rstrip('/')}/api/v0/pin/rm", params={"arg": cid}, timeout=60)
    resp.raise_for_status()
    return (
        f"unpinned {cid}. Other IPFS nodes/gateways that fetched it may still "
        "serve copies — IPFS content cannot be reliably deleted"
    )


# --------------------------------------------------------------- peertube ----


def _peertube_session():
    """A logged-in client (the caller closes it) and the instance base URL."""
    httpx = _httpx()
    base = require_https("CONSENTVID_PEERTUBE_URL", _env("CONSENTVID_PEERTUBE_URL")).rstrip("/")
    client = httpx.Client(base_url=base, timeout=None, follow_redirects=False)
    try:
        oauth = client.get("/api/v1/oauth-clients/local").raise_for_status().json()
        token = (
            client.post(
                "/api/v1/users/token",
                data={
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                    "grant_type": "password",
                    "response_type": "code",
                    "username": _env("CONSENTVID_PEERTUBE_USERNAME"),
                    "password": _env("CONSENTVID_PEERTUBE_PASSWORD"),
                },
            )
            .raise_for_status()
            .json()["access_token"]
        )
    except BaseException:
        client.close()
        raise
    client.headers["Authorization"] = f"Bearer {token}"
    return client, base


def _peertube_marker(content_hash: str) -> str:
    """Written into the video's 'support' field at upload, checked before any
    delete: ties the PeerTube video to this clip's bytes."""
    return f"consentvid:{content_hash}"


def upload_to_peertube(local_path: Path, title: str, content_hash: str) -> str:
    """Upload and return the watch URL. Privacy defaults to unlisted (2):
    reachable by link for dataset consumers, but not advertised across the
    fediverse. Set CONSENTVID_PEERTUBE_PRIVACY=1 for public."""
    if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
        raise ConsentVidError(f"not a content hash: {content_hash!r}")
    client, base = _peertube_session()
    # Not 'with client:': httpx refuses to re-enter a client that has
    # already sent requests (the login above).
    try:
        channel_id = os.environ.get("CONSENTVID_PEERTUBE_CHANNEL_ID")
        if not channel_id:
            me = client.get("/api/v1/users/me").raise_for_status().json()
            channel_id = str(me["videoChannels"][0]["id"])
        with local_path.open("rb") as f:
            resp = client.post(
                "/api/v1/videos/upload",
                data={
                    "channelId": channel_id,
                    "name": title[:120],
                    "privacy": os.environ.get("CONSENTVID_PEERTUBE_PRIVACY", "2"),
                    "support": _peertube_marker(content_hash),
                },
                files={"videofile": (local_path.name, f, mime_for(local_path) or "video/mp4")},
            )
        resp.raise_for_status()
        short = resp.json()["video"]["shortUUID"]
    finally:
        client.close()
    if not isinstance(short, str) or not _PEERTUBE_SHORT_RE.fullmatch(short):
        raise ConsentVidError(f"PeerTube returned an unexpected video id: {short!r}")
    return f"{base}/w/{short}"


def _peertube_short_id(watch_url: str, base: str) -> str:
    """The video's short id if ``watch_url`` is exactly <base>/w/<short>."""
    try:
        u, b = urlsplit(watch_url), urlsplit(base)
        same_origin = u.scheme == b.scheme and (u.hostname or "").lower() == (b.hostname or "").lower()
        same_origin = same_origin and u.port == b.port and u.username is None and u.password is None
    except ValueError:
        same_origin = False
    prefix = b.path.rstrip("/") + "/w/" if same_origin else None
    short = u.path[len(prefix) :] if prefix and u.path.startswith(prefix) else ""
    if not same_origin or u.query or u.fragment or not _PEERTUBE_SHORT_RE.fullmatch(short):
        raise ConsentVidError(f"{watch_url} is not a video watch URL on CONSENTVID_PEERTUBE_URL ({base}); not deleting")
    return short


def _delete_peertube(watch_url: str, content_hash: str) -> str:
    """Delete the PeerTube video behind ``watch_url`` only if it is this
    clip's own upload: owned by the logged-in account, in one of its channels,
    on this instance, and carrying this clip's content-hash marker. A
    moderator/admin account could otherwise be steered (by a storageRef from
    an untrusted PDS) into deleting anyone's video."""
    base = require_https("CONSENTVID_PEERTUBE_URL", _env("CONSENTVID_PEERTUBE_URL")).rstrip("/")
    short = _peertube_short_id(watch_url, base)  # before any network call or login
    if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
        raise ConsentVidError(f"not a content hash: {content_hash!r}; not deleting")
    client, _ = _peertube_session()
    try:
        path = f"/api/v1/videos/{quote(short, safe='')}"
        me = client.get("/api/v1/users/me").raise_for_status().json()
        video = client.get(path).raise_for_status().json()
        username = me.get("username") if isinstance(me, dict) else None
        channels = me.get("videoChannels") if isinstance(me, dict) else None
        my_channels = {c.get("id") for c in channels if isinstance(c, dict)} if isinstance(channels, list) else set()
        account = video.get("account") if isinstance(video, dict) else None
        channel = video.get("channel") if isinstance(video, dict) else None
        account = account if isinstance(account, dict) else {}
        channel = channel if isinstance(channel, dict) else {}
        instance = (urlsplit(base).netloc or "").lower()
        owned = (
            isinstance(username, str)
            and account.get("name") == username
            and str(account.get("host", "")).lower() == instance
            and channel.get("id") is not None
            and channel.get("id") in my_channels
        )
        if not owned:
            raise ConsentVidError(f"PeerTube video {short} is not owned by {username}; not deleting")
        if video.get("support") != _peertube_marker(content_hash):
            raise ConsentVidError(
                f"PeerTube video {short} does not carry this clip's marker ({_peertube_marker(content_hash)}) "
                "in its support field; not deleting. If it is yours, delete it manually."
            )
        client.delete(path).raise_for_status()
    finally:
        client.close()
    return f"deleted PeerTube video {short} (federated mirrors are asked to delete it too)"
