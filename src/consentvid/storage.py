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
bytes where the backend allows it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from consentvid.errors import ConfigError, ConsentVidError
from consentvid.records import mime_for

UPLOAD_KINDS = ("r2", "ipfs", "peertube")


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
    """A dereferenceable URL for a storageRef, or None (``local``)."""
    kind = (storage_ref or {}).get("kind", "url")
    value = (storage_ref or {}).get("value", "")
    if kind == "local" or not value:
        return None
    if kind == "ipfs":
        gateway = os.environ.get("CONSENTVID_IPFS_GATEWAY", "https://ipfs.io").rstrip("/")
        return f"{gateway}/ipfs/{value.removeprefix('ipfs://')}"
    return value


def upload(kind: str, path: Path, *, content_hash: str, title: str) -> str:
    if kind == "r2":
        return upload_to_r2(path, content_hash)
    if kind == "ipfs":
        return add_to_ipfs(path)
    if kind == "peertube":
        return upload_to_peertube(path, title)
    raise ConsentVidError(f"backend {kind!r} does not support uploads")


def delete_bytes(storage_ref: dict) -> str:
    """Remove the bytes behind a storageRef where possible. Returns a
    human-readable description of what happened."""
    kind, value = storage_ref.get("kind"), storage_ref.get("value", "")
    if kind == "r2":
        return _delete_r2(value)
    if kind == "ipfs":
        return _unpin_ipfs(value.removeprefix("ipfs://"))
    if kind == "peertube":
        return _delete_peertube(value)
    if kind == "url":
        return f"bytes at {value} are self-hosted: delete them yourself"
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


def upload_to_r2(local_path: Path, content_hash: str) -> str:
    """Upload to R2 under a content-addressed key; returns the public URL.
    Idempotent: an object already stored under that hash is reused."""
    bucket = _env("CONSENTVID_R2_BUCKET")
    public_base = _env("CONSENTVID_R2_PUBLIC_URL").rstrip("/")
    key = f"videos/{content_hash}"
    s3 = _r2()
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 — absent (404) or not visible: upload
        s3.upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={"ContentType": mime_for(local_path) or "application/octet-stream"},
        )
    return f"{public_base}/{key}"


def _delete_r2(url: str) -> str:
    public_base = _env("CONSENTVID_R2_PUBLIC_URL").rstrip("/") + "/"
    if not url.startswith(public_base):
        raise ConsentVidError(f"{url} is not under CONSENTVID_R2_PUBLIC_URL; can't map it to an object")
    key = url[len(public_base) :]
    _r2().delete_object(Bucket=_env("CONSENTVID_R2_BUCKET"), Key=key)
    return f"deleted R2 object {key} (CDN caches may serve it briefly)"


# ------------------------------------------------------------------ ipfs -----


def add_to_ipfs(local_path: Path) -> str:
    """Add and pin a file; returns its CID. Uses Pinata if
    CONSENTVID_PINATA_JWT is set, else a local Kubo node at CONSENTVID_IPFS_API."""
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
        return resp.json()["IpfsHash"]
    api = os.environ.get("CONSENTVID_IPFS_API", "http://127.0.0.1:5001").rstrip("/")
    try:
        with local_path.open("rb") as f:
            resp = httpx.post(
                f"{api}/api/v0/add",
                params={"cid-version": "1", "pin": "true"},
                files={"file": (local_path.name, f)},
                timeout=None,
            )
    except httpx.ConnectError as e:
        raise ConfigError(f"no IPFS node at {api}: run `ipfs daemon` or set CONSENTVID_PINATA_JWT") from e
    resp.raise_for_status()
    return json.loads(resp.text.strip().splitlines()[-1])["Hash"]


def _unpin_ipfs(cid: str) -> str:
    httpx = _httpx()
    jwt = os.environ.get("CONSENTVID_PINATA_JWT")
    if jwt:
        resp = httpx.delete(
            f"https://api.pinata.cloud/pinning/unpin/{cid}",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=60,
        )
    else:
        api = os.environ.get("CONSENTVID_IPFS_API", "http://127.0.0.1:5001").rstrip("/")
        resp = httpx.post(f"{api}/api/v0/pin/rm", params={"arg": cid}, timeout=60)
    resp.raise_for_status()
    return (
        f"unpinned {cid}. Other IPFS nodes/gateways that fetched it may still "
        "serve copies — IPFS content cannot be reliably deleted"
    )


# --------------------------------------------------------------- peertube ----


def _peertube_session():
    httpx = _httpx()
    base = _env("CONSENTVID_PEERTUBE_URL").rstrip("/")
    client = httpx.Client(base_url=base, timeout=None)
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
    client.headers["Authorization"] = f"Bearer {token}"
    return client, base


def upload_to_peertube(local_path: Path, title: str) -> str:
    """Upload and return the watch URL. Privacy defaults to unlisted (2):
    reachable by link for dataset consumers, but not advertised across the
    fediverse. Set CONSENTVID_PEERTUBE_PRIVACY=1 for public."""
    client, base = _peertube_session()
    with client:
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
                },
                files={"videofile": (local_path.name, f, mime_for(local_path) or "video/mp4")},
            )
        resp.raise_for_status()
        short = resp.json()["video"]["shortUUID"]
    return f"{base}/w/{short}"


def _delete_peertube(watch_url: str) -> str:
    short = watch_url.rstrip("/").rsplit("/", 1)[-1]
    client, base = _peertube_session()
    with client:
        if not watch_url.startswith(base):
            raise ConsentVidError(f"{watch_url} is not on CONSENTVID_PEERTUBE_URL ({base})")
        client.delete(f"/api/v1/videos/{short}").raise_for_status()
    return f"deleted PeerTube video {short} (federated mirrors are asked to delete it too)"
