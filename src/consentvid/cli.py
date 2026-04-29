"""ConsentVid CLI: register videos and attach consent receipts on ATproto."""
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click
from atproto import Client
from dotenv import load_dotenv

from consentvid.croissant import build_manifest


VIDEO_NSID = "org.consentvid.video"
CONSENT_NSID = "org.consentvid.consent"
SESSION_PATH = Path.home() / ".consentvid" / "session.txt"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def get_client() -> Client:
    service = os.environ.get("CONSENTVID_SERVICE", "https://bsky.social")
    client = Client(service)
    if SESSION_PATH.exists():
        client.login(session_string=SESSION_PATH.read_text().strip())
        return client
    handle = os.environ.get("CONSENTVID_HANDLE")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD")
    if not handle or not pw:
        raise click.ClickException(
            "No cached session and no credentials. Run `consentvid login` "
            "or set CONSENTVID_HANDLE and CONSENTVID_APP_PASSWORD in .env."
        )
    client.login(handle, pw)
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(client.export_session_string())
    return client


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256-{h.hexdigest()}"


def _record_value_to_dict(value):
    """Recursively coerce an atproto record value to JSON-friendly types.

    The SDK may return a Pydantic model (for known lexicons) or a DotDict-like
    wrapper (for unknown lexicons). DotDict's __getattr__ returns None for
    missing keys, so a hasattr check on 'model_dump' lies — we check callable.
    """
    mdump = getattr(value, "model_dump", None)
    if callable(mdump):
        return mdump(by_alias=True)
    if isinstance(value, dict):
        return {k: _record_value_to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_record_value_to_dict(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return {k: _record_value_to_dict(v) for k, v in dict(value).items()}
    except Exception:
        return repr(value)


def probe_video(path: Path) -> dict:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "json", str(path),
            ],
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    streams = json.loads(out).get("streams", [])
    if not streams:
        return {}
    s = streams[0]
    result = {}
    if "width" in s:
        result["width"] = int(s["width"])
    if "height" in s:
        result["height"] = int(s["height"])
    if "duration" in s:
        result["durationMs"] = int(round(float(s["duration"]) * 1000))
    return result


@click.group()
def main():
    """consentvid: register videos and consent receipts on ATproto."""
    load_dotenv()


@main.command()
def login():
    """Authenticate and cache a session locally."""
    handle = os.environ.get("CONSENTVID_HANDLE") or click.prompt("Handle")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD") or click.prompt(
        "App password", hide_input=True
    )
    service = os.environ.get("CONSENTVID_SERVICE", "https://bsky.social")
    client = Client(service)
    client.login(handle, pw)
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(client.export_session_string())
    click.echo(f"Logged in as {client.me.handle} ({client.me.did})")
    click.echo(f"Session cached at {SESSION_PATH}")


@main.command()
def whoami():
    """Show the cached session's identity."""
    client = get_client()
    click.echo(f"{client.me.handle} ({client.me.did})")


@main.command("register-video")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--title", required=True)
@click.option("--description", default="")
@click.option("--language", default="")
@click.option("--tag", "tags", multiple=True, help="Repeatable.")
@click.option(
    "--storage-kind",
    default="local",
    type=click.Choice(["local", "url", "ipfs", "r2"]),
)
@click.option(
    "--storage-value",
    default=None,
    help="Pointer to where the bytes live; defaults to the local path.",
)
def register_video(
    video_path, title, description, language, tags, storage_kind, storage_value
):
    """Compute hash + metadata and create a video record on the user's PDS."""
    client = get_client()
    content_hash = hash_file(video_path)

    if storage_kind == "r2" and not storage_value:
        from consentvid.storage import upload_to_r2
        try:
            storage_value = upload_to_r2(video_path, content_hash)
        except RuntimeError as e:
            raise click.ClickException(str(e))
        click.echo(f"Uploaded to R2: {storage_value}")
    elif storage_kind == "url" and not storage_value:
        raise click.ClickException("--storage-kind url requires --storage-value <https://...>")
    elif storage_kind == "ipfs" and not storage_value:
        raise click.ClickException("--storage-kind ipfs requires --storage-value <CID>")
    elif storage_kind == "local":
        storage_value = storage_value or str(video_path.resolve())

    record = {
        "$type": VIDEO_NSID,
        "title": title,
        "contentHash": content_hash,
        "createdAt": now_iso(),
        "storageRef": {
            "kind": storage_kind,
            "value": storage_value,
        },
    }
    if description:
        record["description"] = description
    if language:
        record["language"] = language
    if tags:
        record["tags"] = list(tags)
    record.update(probe_video(video_path))
    resp = client.com.atproto.repo.create_record(
        {
            "repo": client.me.did,
            "collection": VIDEO_NSID,
            "validate": False,
            "record": record,
        }
    )
    click.echo("Created video record:")
    click.echo(f"  uri:  {resp.uri}")
    click.echo(f"  cid:  {resp.cid}")
    click.echo(f"  hash: {content_hash}")


@main.command("attach-consent")
@click.argument("video_uri")
@click.option(
    "--modality",
    "modalities",
    multiple=True,
    default=("video", "audio"),
    type=click.Choice(["video", "audio", "transcript", "face", "voice"]),
)
@click.option(
    "--purpose",
    "purposes",
    multiple=True,
    default=("training",),
    type=click.Choice(["training", "evaluation", "benchmarking", "researchOnly"]),
)
@click.option(
    "--model-class",
    "model_classes",
    multiple=True,
    default=("academic",),
    type=click.Choice(["academic", "openWeights", "commercial", "foundationModel"]),
)
@click.option("--expires-at", default=None, help="ISO-8601 datetime. Optional.")
@click.option(
    "--compensation",
    default="none",
    type=click.Choice(["none", "creditOnly", "monetary"]),
)
@click.option(
    "--non-revocable",
    is_flag=True,
    default=False,
    help="Mark consent as non-revocable (default: revocable).",
)
def attach_consent(
    video_uri,
    modalities,
    purposes,
    model_classes,
    expires_at,
    compensation,
    non_revocable,
):
    """Create a consent receipt referencing a video record by AT-URI."""
    client = get_client()
    record = {
        "$type": CONSENT_NSID,
        "videoUri": video_uri,
        "modalities": list(modalities),
        "purposes": list(purposes),
        "modelClasses": list(model_classes),
        "compensation": compensation,
        "revocable": not non_revocable,
        "createdAt": now_iso(),
    }
    if expires_at:
        record["expiresAt"] = expires_at
    resp = client.com.atproto.repo.create_record(
        {
            "repo": client.me.did,
            "collection": CONSENT_NSID,
            "validate": False,
            "record": record,
        }
    )
    click.echo("Created consent receipt:")
    click.echo(f"  uri: {resp.uri}")
    click.echo(f"  cid: {resp.cid}")


@main.command("list")
@click.option(
    "--collection",
    default=CONSENT_NSID,
    type=click.Choice([VIDEO_NSID, CONSENT_NSID]),
)
@click.option("--limit", default=50, type=int)
def list_records(collection, limit):
    """List the user's video or consent records."""
    client = get_client()
    resp = client.com.atproto.repo.list_records(
        {"repo": client.me.did, "collection": collection, "limit": limit}
    )
    if not resp.records:
        click.echo(f"No records in {collection}.")
        return
    for r in resp.records:
        click.echo(r.uri)
        click.echo(json.dumps(_record_value_to_dict(r.value), default=str, indent=2))
        click.echo()


@main.command()
@click.argument("uri")
def revoke(uri):
    """Delete a record by AT-URI from your repo.

    Note: deleting from your repo does not remove copies already collected
    into datasets. A revocation feed that consumers must re-sync is future work.
    """
    client = get_client()
    parts = uri.removeprefix("at://").split("/")
    if len(parts) != 3:
        raise click.ClickException(f"Bad AT-URI: {uri}")
    repo, collection, rkey = parts
    if repo != client.me.did:
        raise click.ClickException(f"Not your record (repo={repo}).")
    client.com.atproto.repo.delete_record(
        {"repo": repo, "collection": collection, "rkey": rkey}
    )
    click.echo(f"Deleted {uri}")


@main.command()
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--name", default=None, help="Dataset name. Defaults to consentvid-<handle>-<date>.")
@click.option("--description", default=None)
@click.option(
    "--purpose",
    "purposes",
    multiple=True,
    help="Filter consents to those covering this purpose. Repeatable.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Filter videos by tag. Repeatable.",
)
def export(output_path, name, description, purposes, tags):
    """Emit a Croissant-RAI dataset manifest from this user's consents."""
    client = get_client()
    consent_resp = client.com.atproto.repo.list_records(
        {"repo": client.me.did, "collection": CONSENT_NSID, "limit": 100}
    )
    video_resp = client.com.atproto.repo.list_records(
        {"repo": client.me.did, "collection": VIDEO_NSID, "limit": 100}
    )
    videos_by_uri = {r.uri: _record_value_to_dict(r.value) for r in video_resp.records}
    consents_with_uri = [
        (r.uri, _record_value_to_dict(r.value)) for r in consent_resp.records
    ]

    if purposes:
        wanted = set(purposes)
        consents_with_uri = [
            (uri, c) for uri, c in consents_with_uri
            if wanted & set(c.get("purposes", []))
        ]
    if tags:
        wanted = set(tags)
        consents_with_uri = [
            (uri, c) for uri, c in consents_with_uri
            if wanted & set(videos_by_uri.get(c.get("videoUri", ""), {}).get("tags", []))
        ]

    manifest = build_manifest(
        creator_handle=client.me.handle,
        creator_did=client.me.did,
        consents_with_uri=consents_with_uri,
        videos_by_uri=videos_by_uri,
        name=name,
        description=description,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2))

    matched = sum(1 for _, c in consents_with_uri if c.get("videoUri") in videos_by_uri)
    click.echo(f"Wrote {output_path}")
    click.echo(f"  consents listed:  {len(consent_resp.records)}")
    click.echo(f"  videos listed:    {len(video_resp.records)}")
    click.echo(f"  consents matched: {matched} (after filters)")


@main.group()
def appview():
    """AppView: index org.consentvid.* records across users into a local SQLite db."""
    pass


@appview.command("backfill")
@click.argument("handle_or_did")
def appview_backfill(handle_or_did):
    """Index existing records from a specific user's repo (run before live indexing)."""
    from consentvid import appview as av

    client = get_client()
    if handle_or_did.startswith("did:"):
        did = handle_or_did
    else:
        resp = client.com.atproto.identity.resolve_handle({"handle": handle_or_did})
        did = resp.did
    conn = av.connect()
    counts = {"videos": 0, "consents": 0}
    for collection, ingest, key in (
        (av.VIDEO_NSID, av.upsert_video, "videos"),
        (av.CONSENT_NSID, av.upsert_consent, "consents"),
    ):
        cursor = None
        while True:
            params = {"repo": did, "collection": collection, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = client.com.atproto.repo.list_records(params)
            for r in resp.records:
                rkey = r.uri.rsplit("/", 1)[-1]
                ingest(conn, r.uri, did, rkey, str(r.cid), _record_value_to_dict(r.value))
                counts[key] += 1
            cursor = resp.cursor
            if not cursor:
                break
    conn.commit()
    click.echo(f"Backfilled from {did}:")
    click.echo(f"  videos:   {counts['videos']}")
    click.echo(f"  consents: {counts['consents']}")


@appview.command("index")
@click.option(
    "--stop-after",
    type=int,
    default=None,
    help="Process N events then exit (useful for testing).",
)
def appview_index(stop_after):
    """Subscribe to Jetstream and index org.consentvid.* records (long-running)."""
    from consentvid import appview as av

    conn = av.connect()
    click.echo(f"Indexing into {av.db_path()} (Ctrl-C to stop)")

    def on_event(op, uri):
        click.echo(f"  [{op}] {uri}")

    try:
        n = asyncio.run(av.index_loop(conn, on_event=on_event, stop_after=stop_after))
        click.echo(f"Processed {n} events.")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@appview.command("stats")
def appview_stats():
    """Show index counts."""
    from consentvid import appview as av

    conn = av.connect()
    n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    n_consents = conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0]
    n_revoked = conn.execute(
        "SELECT COUNT(*) FROM consents WHERE revoked_at IS NOT NULL"
    ).fetchone()[0]
    n_dids = conn.execute(
        "SELECT COUNT(DISTINCT did) FROM consents"
    ).fetchone()[0]
    click.echo(f"videos:        {n_videos}")
    click.echo(f"consents:      {n_consents}  ({n_revoked} revoked)")
    click.echo(f"contributors:  {n_dids}")


@appview.command("query")
@click.option("--purpose", multiple=True, help="Filter by purpose. Repeatable.")
@click.option("--model-class", "model_classes", multiple=True, help="Filter by model class. Repeatable.")
@click.option("--did", default=None, help="Filter by contributor DID.")
@click.option("--include-revoked", is_flag=True, default=False)
@click.option("--limit", default=20, type=int)
def appview_query(purpose, model_classes, did, include_revoked, limit):
    """Query indexed consent receipts."""
    from consentvid import appview as av

    conn = av.connect()
    where, args = [], []
    if not include_revoked:
        where.append("c.revoked_at IS NULL")
    if did:
        where.append("c.did = ?")
        args.append(did)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"""
        SELECT c.uri, c.did, c.video_uri, c.purposes, c.model_classes, c.modalities,
               c.created_at, v.title, v.duration_ms
        FROM consents c
        LEFT JOIN videos v ON v.uri = c.video_uri
        {where_sql}
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        (*args, limit * 4),
    ).fetchall()

    def matches(row):
        if purpose and not (set(purpose) & set(json.loads(row["purposes"] or "[]"))):
            return False
        if model_classes and not (
            set(model_classes) & set(json.loads(row["model_classes"] or "[]"))
        ):
            return False
        return True

    rows = [r for r in rows if matches(r)][:limit]
    if not rows:
        click.echo("No matching consents.")
        return
    for r in rows:
        click.echo(r["uri"])
        click.echo(f"  did:        {r['did']}")
        click.echo(f"  title:      {r['title']!r}")
        click.echo(f"  video_uri:  {r['video_uri']}")
        click.echo(f"  purposes:      {r['purposes']}")
        click.echo(f"  model_classes: {r['model_classes']}")
        click.echo(f"  modalities:    {r['modalities']}")
        click.echo()


if __name__ == "__main__":
    main()
