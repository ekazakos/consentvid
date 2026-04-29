"""Storage backends for video bytes.

The video record only commits to a content hash and a storage pointer; the
bytes themselves can live anywhere. This module implements the optional
R2 (Cloudflare object storage) backend.

R2 is S3-compatible. Object keys are content-addressed (`videos/<sha256>`)
so identical bytes from different contributors deduplicate naturally.
"""
from __future__ import annotations

import os
from pathlib import Path


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


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val
