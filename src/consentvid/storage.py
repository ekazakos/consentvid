"""Storage backends for video bytes.

The video record only commits to a content hash and a storage pointer; the bytes
themselves can live anywhere. Keeping ConsentVid non-custodial — the platform
stores kilobyte-sized consent records and hashes, never the video — is the whole
economic argument, so the backends here are deliberately pluggable:

  r2        Cloudflare R2 (S3-compatible object storage). Cheap, zero egress.
  ipfs      Content-addressed P2P storage. The CID *is* the content hash, so it
            dovetails with the record's SHA-256 commitment. Persistence needs a
            pin (a local Kubo node or a pinning service such as Pinata).
  peertube  Federated open-source video host. Gives transcoding + adaptive +
            P2P (WebTorrent) delivery off the shelf.
  url/local A bare pointer the contributor manages themselves.

Each uploader returns the string that goes in ``storageRef.value``; ``public_url``
turns a stored ``storageRef`` back into a dereferenceable URL for manifests.

boto3 / httpx are optional; install with ``pip install -e ".[r2]"`` /
``".[ipfs]"`` / ``".[peertube]"``.
"""
from __future__ import annotations

import os
from pathlib import Path


# ---------------------------------------------------------------- helpers ----

def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _require_httpx():
    try:
        import httpx
    except ImportError as e:
        raise RuntimeError(
            'httpx is required for this backend. Install with: pip install -e ".[ipfs]" '
            'or ".[peertube]".'
        ) from e
    return httpx


def public_url(storage_ref: dict) -> str:
    """Resolve a stored storageRef to a dereferenceable URL.

    r2/url/peertube values are already URLs. An ipfs value is a bare CID, turned
    into a gateway URL (CONSENTVID_IPFS_GATEWAY, default https://ipfs.io). A
    local value is a filesystem path and is returned unchanged (not remotely
    dereferenceable — a manifest built from local clips is not portable).
    """
    kind = (storage_ref or {}).get("kind", "url")
    value = (storage_ref or {}).get("value", "")
    if kind == "ipfs":
        gateway = os.environ.get("CONSENTVID_IPFS_GATEWAY", "https://ipfs.io").rstrip("/")
        cid = value.removeprefix("ipfs://")
        return f"{gateway}/ipfs/{cid}"
    return value


# ------------------------------------------------------------------- r2 ------

def upload_to_r2(local_path: Path, content_hash: str) -> str:
    """Upload a file to Cloudflare R2 and return its public URL.

    Required env vars:
      CONSENTVID_R2_ENDPOINT       https://<accountid>.r2.cloudflarestorage.com
      CONSENTVID_R2_BUCKET         your bucket name
      CONSENTVID_R2_ACCESS_KEY_ID
      CONSENTVID_R2_SECRET_ACCESS_KEY
      CONSENTVID_R2_PUBLIC_URL     e.g. https://videos.consentvid.org

    Install boto3 with: pip install -e ".[r2]"
    """
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            'boto3 is required for R2 uploads. Install with: pip install -e ".[r2]"'
        ) from e

    endpoint = _required("CONSENTVID_R2_ENDPOINT")
    bucket = _required("CONSENTVID_R2_BUCKET")
    access_key = _required("CONSENTVID_R2_ACCESS_KEY_ID")
    secret_key = _required("CONSENTVID_R2_SECRET_ACCESS_KEY")
    public_base = _required("CONSENTVID_R2_PUBLIC_URL").rstrip("/")

    object_key = f"videos/{content_hash}"

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    s3.upload_file(str(local_path), bucket, object_key)
    return f"{public_base}/{object_key}"


# ------------------------------------------------------------------ ipfs -----

def add_to_ipfs(local_path: Path) -> str:
    """Add a file to IPFS and return its CID.

    Uses a pinning service if CONSENTVID_PINATA_JWT is set (durable, hosted),
    otherwise a local IPFS (Kubo) node's RPC API at CONSENTVID_IPFS_API
    (default http://127.0.0.1:5001). A local `ipfs add` keeps the bytes only as
    long as that node runs and pins them; a pinning service guarantees
    persistence — that distinction is the whole "IPFS is not free forever" story.

    Install httpx with: pip install -e ".[ipfs]"
    """
    httpx = _require_httpx()
    jwt = os.environ.get("CONSENTVID_PINATA_JWT")
    if jwt:
        with local_path.open("rb") as f:
            resp = httpx.post(
                "https://api.pinata.cloud/pinning/pinFileToIPFS",
                headers={"Authorization": f"Bearer {jwt}"},
                files={"file": (local_path.name, f)},
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
        raise RuntimeError(
            f"No IPFS node reachable at {api}. Start a local node (`ipfs daemon`) "
            "or set CONSENTVID_PINATA_JWT to use a pinning service."
        ) from e
    resp.raise_for_status()
    # `add` streams one JSON object per added object; the last line is the root.
    last = resp.text.strip().splitlines()[-1]
    import json

    return json.loads(last)["Hash"]


# --------------------------------------------------------------- peertube ----

def upload_to_peertube(local_path: Path, title: str) -> str:
    """Upload a video to a PeerTube instance and return its watch URL.

    Required env vars:
      CONSENTVID_PEERTUBE_URL       https://your.peertube.instance
      CONSENTVID_PEERTUBE_USERNAME
      CONSENTVID_PEERTUBE_PASSWORD
    Optional:
      CONSENTVID_PEERTUBE_CHANNEL_ID  numeric channel id (default: user's first)
      CONSENTVID_PEERTUBE_PRIVACY     1 public, 2 unlisted, 3 private (default 1)

    Install httpx with: pip install -e ".[peertube]"
    """
    httpx = _require_httpx()
    base = _required("CONSENTVID_PEERTUBE_URL").rstrip("/")
    username = _required("CONSENTVID_PEERTUBE_USERNAME")
    password = _required("CONSENTVID_PEERTUBE_PASSWORD")
    privacy = os.environ.get("CONSENTVID_PEERTUBE_PRIVACY", "1")

    with httpx.Client(base_url=base, timeout=None) as client:
        # OAuth2 password grant: fetch the client id/secret, then a token.
        oauth = client.get("/api/v1/oauth-clients/local").raise_for_status().json()
        token = (
            client.post(
                "/api/v1/users/token",
                data={
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                    "grant_type": "password",
                    "response_type": "code",
                    "username": username,
                    "password": password,
                },
            )
            .raise_for_status()
            .json()["access_token"]
        )
        auth = {"Authorization": f"Bearer {token}"}

        channel_id = os.environ.get("CONSENTVID_PEERTUBE_CHANNEL_ID")
        if not channel_id:
            me = client.get("/api/v1/users/me", headers=auth).raise_for_status().json()
            channel_id = str(me["videoChannels"][0]["id"])

        with local_path.open("rb") as f:
            resp = client.post(
                "/api/v1/videos/upload",
                headers=auth,
                data={"channelId": channel_id, "name": title, "privacy": privacy},
                files={"videofile": (local_path.name, f)},
            )
        resp.raise_for_status()
        short = resp.json()["video"]["shortUUID"]
    return f"{base}/w/{short}"
