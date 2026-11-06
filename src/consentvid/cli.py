"""ConsentVid command-line interface.

Contributor commands write to your own ATproto repo and need `consentvid
login`. Everything that only reads (verify, resync, export --repo, embed/read
C2PA, appview) talks directly to the relevant PDS and needs no account.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import click
from atproto import Client
from atproto_client.exceptions import AtProtocolError, NetworkError
from dotenv import find_dotenv, load_dotenv

from consentvid import __version__
from consentvid.errors import ConfigError, ConsentVidError
from consentvid.records import (
    CONSENT_NSID,
    VIDEO_NSID,
    format_dt,
    hash_file,
    is_expired,
    mime_for,
    now_iso,
    parse_at_uri,
    parse_dt,
    to_dict,
)
from consentvid.validate import validate_in_repo, write_policy_errors
from consentvid.vocab import (
    CAPTURE_PROVENANCE,
    COMPENSATION,
    MODALITIES,
    MODEL_CLASSES,
    OFFERED_MODEL_CLASSES,
    OFFERED_PURPOSES,
    PEOPLE_PRESENT,
    PURPOSES,
    RIGHTS_BASIS,
    STORAGE_KINDS,
)

CONFIG_DIR = Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid"))
SESSION_PATH = CONFIG_DIR / "session.txt"


# ------------------------------------------------------------ plumbing -------


def _load_env() -> None:
    """Settings precedence: real env > ./.env (or a parent) > ~/.consentvid/config.env
    > the source checkout's .env (editable installs)."""
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(CONFIG_DIR / "config.env")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _secure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)


def _xrpc_message(e: AtProtocolError) -> str:
    content = getattr(getattr(e, "response", None), "content", None)
    err, msg = getattr(content, "error", None), getattr(content, "message", None)
    if err or msg:
        return f"{err or 'Error'}: {msg or ''}".strip()
    if isinstance(e, NetworkError):
        return f"network error talking to the PDS: {e}"
    return str(e) or type(e).__name__


class _CLI(click.Group):
    """Turn expected failures into one-line errors instead of tracebacks."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ConsentVidError as e:
            raise click.ClickException(str(e)) from e
        except AtProtocolError as e:
            raise click.ClickException(_xrpc_message(e)) from e


def _warn(msg: str) -> None:
    click.secho(f"warning: {msg}", fg="yellow", err=True)


def _new_client() -> Client:
    client = Client(os.environ.get("CONSENTVID_SERVICE", "https://bsky.social"))

    # Persist every new or refreshed session; otherwise the cached refresh
    # token eventually expires and every command fails with ExpiredToken.
    def _save(_event, _session):
        _secure_config_dir()
        SESSION_PATH.write_text(client.export_session_string())
        SESSION_PATH.chmod(0o600)

    client.on_session_change(_save)
    return client


def get_client() -> Client:
    """An authenticated client: cached session, else credentials from env."""
    if SESSION_PATH.exists():
        client = _new_client()
        try:
            client.login(session_string=SESSION_PATH.read_text().strip())
            return client
        except Exception:  # noqa: BLE001 — expired/invalid: fall back to credentials
            pass
    handle = os.environ.get("CONSENTVID_HANDLE")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD")
    if not handle or not pw:
        raise ConfigError(
            "not logged in. Run `consentvid login` (use an app password from "
            "https://bsky.app/settings/app-passwords, never your main password)."
        )
    client = _new_client()
    client.login(handle, pw)
    return client


def _list_all(client: Client, did: str, collection: str) -> list:
    out, cursor = [], None
    while True:
        params = {"repo": did, "collection": collection, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.com.atproto.repo.list_records(params)
        out.extend(resp.records)
        cursor = resp.cursor
        if not cursor or not resp.records:
            return out


def _validate_flag(override: bool | None) -> bool | None:
    """Value for createRecord's `validate`. None = the PDS's default
    ("optimistic": validate lexicons it knows, accept others), which is the
    right setting for third-party lexicons; True forces strict validation and
    fails on PDSes that don't resolve them (bsky.social doesn't)."""
    if override is not None:
        return override
    env = os.environ.get("CONSENTVID_VALIDATE", "").lower()
    return True if env in ("1", "true", "yes") else None


def _check_before_write(nsid: str, record: dict, did: str) -> None:
    errors = validate_in_repo(nsid, record, did) + write_policy_errors(nsid, record)
    if errors:
        raise ConsentVidError("record failed validation:\n  - " + "\n  - ".join(errors))


def _write_params(did, collection, record, validate, rkey=None) -> dict:
    params = {"repo": did, "collection": collection, "record": record}
    if rkey:
        params["rkey"] = rkey
    v = _validate_flag(validate)
    if v is not None:
        params["validate"] = v
    return params


def _parse_expiry(value: str | None) -> str | None:
    """Accept an atproto datetime, a date (midnight UTC) or a duration like
    30d / 12w / 2y. Must be in the future."""
    if not value:
        return None
    now = datetime.now(UTC)
    m = re.fullmatch(r"(\d+)([dwy])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        dt = now + timedelta(days=n * {"d": 1, "w": 7, "y": 365}[unit])
    else:
        dt = parse_dt(value)
        if dt is None:
            try:
                dt = datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
            except ValueError:
                raise click.BadParameter(
                    "use an ISO datetime with timezone (2027-01-01T00:00:00Z), a date "
                    "(2027-01-01) or a duration (90d, 12w, 2y)",
                    param_hint="--expires-at",
                ) from None
    if dt <= now:
        raise click.BadParameter("expiry must be in the future", param_hint="--expires-at")
    return format_dt(dt)


def _require_attestation(attest: bool) -> None:
    if not attest:
        raise click.UsageError(
            "consent requires --attest-authorship: you affirm (signed by your key) that "
            "you recorded this video yourself or otherwise hold the right to consent. "
            "Consenting on behalf of someone else's video (e.g. a YouTube re-upload) is "
            "unlawful. If you hold a licence rather than having filmed it, also pass "
            "--rights-basis licensed."
        )


_TERM_PROMPTS = (
    ("modalities", "--modality", MODALITIES, "What may be used"),
    ("purposes", "--purpose", OFFERED_PURPOSES, "What it may be used for"),
    ("model_classes", "--model-class", OFFERED_MODEL_CLASSES, "Who may use it"),
)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_multi(label: str, choices) -> tuple:
    choice_str = ", ".join(choices)
    while True:
        raw = click.prompt(f"{label} (comma-separated; options: {choice_str})", default="", show_default=False)
        vals = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
        bad = [v for v in vals if v not in choices]
        if vals and not bad:
            return vals
        click.echo(f"  pick at least one of: {choice_str}" + (f" (unknown: {', '.join(bad)})" if bad else ""))


def _require_terms(terms: dict) -> None:
    """Consent must be specific (GDPR Art. 4(11)): nothing is granted by
    default. Missing terms are asked for interactively, else refused. Runs
    before any login or upload."""
    _require_attestation(terms["attest"])
    missing = [t for t in _TERM_PROMPTS if not terms.get(t[0])]
    if not terms.get("people"):
        missing.append(("people", "--people", PEOPLE_PRESENT, None))
    if missing and not _interactive():
        raise click.UsageError(
            "consent terms must be stated explicitly; nothing is granted by default. Missing: "
            + ", ".join(flag for _, flag, _, _ in missing)
        )
    for key, _flag, choices, label in missing:
        if key == "people":
            click.echo(
                "Who identifiably appears or is heard in the video?\n"
                "  onlyMe               only you\n"
                "  allConsented         others too, and every one of them agreed to this use\n"
                "  noIdentifiablePeople no identifiable face or voice (e.g. hands-only footage)"
            )
            terms["people"] = click.prompt("peoplePresent", type=click.Choice(PEOPLE_PRESENT))
        else:
            terms[key] = _prompt_multi(label, choices)
    if not terms.get("adult"):
        if not _interactive():
            raise click.UsageError(
                "consent requires --attest-adult: you affirm (signed by your key) that you are "
                "18 or older (or the age of majority where you live) and able to consent."
            )
        if not click.confirm("I am 18 or older (or the age of majority where I live) and able to consent"):
            raise click.UsageError("only adults able to consent may contribute")
        terms["adult"] = True
    if terms["people"] == "noIdentifiablePeople" and {"face", "voice"} & set(terms["modalities"]):
        raise click.UsageError("--modality face/voice contradicts --people noIdentifiablePeople")


def _default_storage() -> str:
    from consentvid import storage

    env = os.environ.get("CONSENTVID_STORAGE")
    if env:
        return env
    return "r2" if storage.configured("r2") else "local"


# ------------------------------------------------------------- options -------


def _video_options(f):
    for opt in reversed(
        [
            click.option("--title", required=True, help="Short title (max 300 bytes)."),
            click.option("--description", default="", help="Optional longer description."),
            click.option("--language", default="", help="BCP-47 tag of spoken language, e.g. en, cs."),
            click.option("--tag", "tags", multiple=True, help="Topic tag. Repeatable."),
            click.option(
                "--storage-kind",
                type=click.Choice(STORAGE_KINDS),
                default=None,
                help="Where the bytes live. Default: $CONSENTVID_STORAGE, else r2 if "
                "configured, else local (not usable in shared datasets).",
            ),
            click.option(
                "--storage-value",
                default=None,
                help="Existing pointer (URL or CID). Omit for r2/ipfs/peertube to upload.",
            ),
            click.option(
                "--keep-metadata",
                is_flag=True,
                help="Do NOT strip location/device metadata from the file (not recommended).",
            ),
            click.option(
                "--allow-duplicate",
                is_flag=True,
                help="Register even if you already registered identical bytes.",
            ),
        ]
    ):
        f = opt(f)
    return f


def _consent_options(f):
    for opt in reversed(
        [
            click.option(
                "--attest-authorship",
                "attest",
                is_flag=True,
                help="REQUIRED. You affirm, signed by your key, that you recorded this "
                "video (or otherwise hold the rights) and may grant consent.",
            ),
            click.option(
                "--modality",
                "modalities",
                multiple=True,
                type=click.Choice(MODALITIES),
                help="What may be used. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--purpose",
                "purposes",
                multiple=True,
                type=click.Choice(OFFERED_PURPOSES),
                help="What it may be used for. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--model-class",
                "model_classes",
                multiple=True,
                type=click.Choice(OFFERED_MODEL_CLASSES),
                help="Who may use it. Repeatable. REQUIRED (asked if interactive).",
            ),
            click.option(
                "--people",
                type=click.Choice(PEOPLE_PRESENT),
                default=None,
                help="REQUIRED. Who identifiably appears: onlyMe, allConsented (everyone shown "
                "agreed to this use), or noIdentifiablePeople (excludes face/voice).",
            ),
            click.option(
                "--attest-adult",
                "adult",
                is_flag=True,
                help="REQUIRED. You affirm, signed by your key, that you are an adult able to consent.",
            ),
            click.option(
                "--expires-at",
                default=None,
                help="Optional expiry: ISO datetime, date, or duration (90d, 12w, 2y).",
            ),
            click.option(
                "--compensation",
                type=click.Choice(COMPENSATION),
                default="none",
                show_default=True,
                help="What you expect in return. A declaration only: ConsentVid does not pay or enforce it.",
            ),
            click.option(
                "--rights-basis",
                type=click.Choice(RIGHTS_BASIS),
                default="ownRecording",
                show_default=True,
                help="Why you may grant consent.",
            ),
            click.option(
                "--capture-provenance",
                type=click.Choice(CAPTURE_PROVENANCE),
                default="selfAttested",
                show_default=True,
                help="'attested' is reserved for capture-time cryptographic provenance.",
            ),
        ]
    ):
        f = opt(f)
    return f


def _validate_option(f):
    return click.option(
        "--validate/--no-validate",
        "validate",
        default=None,
        help="Force (or disable) PDS-side lexicon validation. Default: the PDS's own "
        "policy. Records are always validated locally first.",
    )(f)


# ---------------------------------------------------- register / attach ------


def _prepare_bytes(path: Path, kind: str, keep_metadata: bool, workdir: Path, pointer_given: bool):
    """Inspect the file and strip identifying metadata unless told not to.
    Returns (path_to_publish, probe_or_None). The record's contentHash is the
    hash of the returned path, so those must be the bytes the pointer serves."""
    from consentvid import media

    probe = media.probe(path)
    if probe is None:
        _warn(
            "ffprobe not found: can't read duration/size or check for location metadata (brew install ffmpeg)"
        )
        return path, None
    if not probe.needs_sanitising:
        return path, probe
    found = list(probe.sensitive_tags.items())[:6]
    if keep_metadata:
        _warn("publishing WITH identifying metadata: " + ", ".join(f"{k}={v}" for k, v in found))
        return path, probe
    click.echo("Stripping identifying metadata (lossless, no re-encode):")
    for k, v in found:
        click.echo(f"  - {k} = {v}")
    for s in probe.extra_streams:
        click.echo(f"  - extra stream {s}")
    if pointer_given:
        # The bytes behind an existing pointer still carry the metadata, and
        # the record would commit to bytes nobody serves. Hand over a clean copy.
        out = path.with_name(f"{path.stem}.clean{path.suffix}")
        media.sanitise(path, out)
        raise ConsentVidError(
            f"{path.name} contains identifying metadata, so the bytes at --storage-value "
            f"would too. A clean copy was written to {out}: host that file instead and "
            "pass its pointer, drop --storage-value to let consentvid upload it, or use "
            "--keep-metadata."
        )
    if kind == "local":
        # Kept by the contributor: leave the clean copy next to the original.
        out = path.with_name(f"{path.stem}.clean{path.suffix}")
        click.echo(f"  -> clean copy: {out}  (the record commits to this file's hash)")
    else:
        out = workdir / f"clean{path.suffix.lower()}"
    return out, media.sanitise(path, out)


def _bytes_shared(client: Client, did: str, ref: dict, exclude_uri: str | None = None) -> str | None:
    """URI of another of your video records using the same stored bytes, if any
    (R2/IPFS storage is content-addressed, so duplicates share one object)."""
    for r in _list_all(client, did, VIDEO_NSID):
        if r.uri != exclude_uri and (to_dict(r.value).get("storageRef") or {}) == ref:
            return r.uri
    return None


def _register(
    client: Client,
    video_path: Path,
    *,
    title,
    description,
    language,
    tags,
    storage_kind,
    storage_value,
    keep_metadata,
    allow_duplicate,
    validate,
):
    from consentvid import storage

    did = client.me.did
    kind = storage_kind or _default_storage()
    if kind == "local":
        _warn(
            "storage kind 'local': nobody else can fetch these bytes, so this clip "
            "can't appear in shared datasets. Use --storage-kind r2/ipfs/peertube/url."
        )
    if kind == "url" and not storage_value:
        raise click.UsageError("--storage-kind url requires --storage-value https://...")
    if kind == "url" and not storage_value.startswith("https://"):
        raise click.UsageError("--storage-value for url must be an https:// URL")

    _secure_config_dir()
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as tmp:
        path, probe = _prepare_bytes(video_path, kind, keep_metadata, Path(tmp), bool(storage_value))
        content_hash = hash_file(path)

        if not allow_duplicate:
            for r in _list_all(client, did, VIDEO_NSID):
                if to_dict(r.value).get("contentHash") == content_hash:
                    raise ConsentVidError(
                        f"these bytes are already registered as {r.uri} "
                        "(pass --allow-duplicate to register again)"
                    )

        record = {
            "$type": VIDEO_NSID,
            "title": title,
            "contentHash": content_hash,
            "storageRef": {"kind": kind, "value": storage_value or ""},
            "createdAt": now_iso(),
        }
        if mime := mime_for(path):
            record["mimeType"] = mime
        if description:
            record["description"] = description
        if language:
            record["language"] = language
        if tags:
            record["tags"] = list(dict.fromkeys(tags))
        if probe:
            record.update(probe.record_fields())
        if kind == "local" and not storage_value:
            record["storageRef"]["value"] = path.name  # the file actually hashed
        uploading = kind in storage.UPLOAD_KINDS and not storage_value
        if uploading:
            record["storageRef"]["value"] = "pending-upload"
        # Validate BEFORE uploading so a bad record never leaves orphaned bytes.
        _check_before_write(VIDEO_NSID, record, did)

        uploaded = False
        try:
            if uploading:
                click.echo(f"Uploading to {kind} ...")
                record["storageRef"]["value"] = storage.upload(
                    kind, path, content_hash=content_hash, title=title
                )
                uploaded = True
                click.echo(f"  -> {record['storageRef']['value']}")
                _check_before_write(VIDEO_NSID, record, did)
            resp = client.com.atproto.repo.create_record(_write_params(did, VIDEO_NSID, record, validate))
        except Exception:
            if uploaded:
                try:
                    other = _bytes_shared(client, did, record["storageRef"])
                except Exception:  # noqa: BLE001 — can't tell: keep the bytes
                    other = "an unknown record (could not check)"
                if other:
                    _warn(f"record creation failed; keeping the stored bytes (also used by {other})")
                else:
                    _warn("record creation failed; removing the uploaded bytes")
                    try:
                        storage.delete_bytes(record["storageRef"])
                    except Exception as cleanup:  # noqa: BLE001
                        _warn(f"cleanup failed, remove manually: {cleanup}")
            raise
    return resp.uri, resp.cid, record


def _attach(
    client: Client,
    video_uri: str,
    *,
    modalities,
    purposes,
    model_classes,
    expires_at,
    compensation,
    attest,
    people,
    adult,
    rights_basis,
    capture_provenance,
    replace,
    validate,
):
    _require_attestation(attest)
    if not (modalities and purposes and model_classes and people and adult):
        raise ConsentVidError("consent terms, --people and --attest-adult are required")
    did = client.me.did
    try:
        vat = parse_at_uri(video_uri, VIDEO_NSID)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="VIDEO_URI") from None
    if vat.did != did:
        raise ConsentVidError(
            f"{video_uri} is not in your repo ({did}): you can only consent for videos you registered"
        )
    video = client.com.atproto.repo.get_record({"repo": did, "collection": VIDEO_NSID, "rkey": vat.rkey})

    existing = sorted(
        (
            (r.uri, to_dict(r.value))
            for r in _list_all(client, did, CONSENT_NSID)
            if to_dict(r.value).get("videoUri") == str(vat)
        ),
        key=lambda x: x[1].get("createdAt", ""),
    )
    if existing and not replace:
        raise ConsentVidError(
            f"this video already has consent {existing[-1][0]}.\n"
            "Pass --replace to change its terms (downstream datasets will see it as "
            "modified), or `consentvid revoke` it first."
        )

    record = {
        "$type": CONSENT_NSID,
        "videoUri": str(vat),
        "videoCid": str(video.cid),
        "modalities": list(dict.fromkeys(modalities)),
        "purposes": list(dict.fromkeys(purposes)),
        "modelClasses": list(dict.fromkeys(model_classes)),
        "compensation": compensation,
        "revocable": True,
        "authorshipAttested": True,
        "rightsBasis": rights_basis,
        "captureProvenance": capture_provenance,
        "peoplePresent": people,
        "adultAttested": True,
        "createdAt": now_iso(),
    }
    if expires_at:
        record["expiresAt"] = _parse_expiry(expires_at)
    _check_before_write(CONSENT_NSID, record, did)

    if existing:
        keep_uri = existing[-1][0]
        resp = client.com.atproto.repo.put_record(
            _write_params(did, CONSENT_NSID, record, validate, rkey=parse_at_uri(keep_uri).rkey)
        )
        for old_uri, _ in existing[:-1]:  # legacy duplicates: superseded
            client.com.atproto.repo.delete_record(
                {"repo": did, "collection": CONSENT_NSID, "rkey": parse_at_uri(old_uri).rkey}
            )
            click.echo(f"Deleted superseded consent {old_uri}")
        return resp.uri, resp.cid, record, True
    resp = client.com.atproto.repo.create_record(_write_params(did, CONSENT_NSID, record, validate))
    return resp.uri, resp.cid, record, False


def _print_consent(uri, cid, record, replaced=False):
    from consentvid.policy import describe_terms

    click.secho(("Replaced" if replaced else "Created") + " consent receipt:", fg="green")
    click.echo(f"  uri:   {uri}")
    click.echo(f"  cid:   {cid}")
    click.echo(f"  terms: {describe_terms(record)}")
    click.echo("  Withdraw any time with: consentvid revoke " + uri)


# ---------------------------------------------------------------- CLI --------


@click.group(cls=_CLI, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="consentvid")
def main():
    """ConsentVid: register videos and signed consent receipts on ATproto, and
    build datasets that honour them."""
    _load_env()


@main.command()
def login():
    """Log in with an app password and cache the session (~/.consentvid)."""
    handle = os.environ.get("CONSENTVID_HANDLE") or click.prompt("Handle (e.g. you.bsky.social)")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD") or click.prompt(
        "App password (bsky.app/settings/app-passwords)", hide_input=True
    )
    client = _new_client()
    client.login(handle.removeprefix("@"), pw)
    click.echo(f"Logged in as {client.me.handle} ({client.me.did})")


@main.command()
def logout():
    """Delete the cached session."""
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
        click.echo("Logged out.")
    else:
        click.echo("No cached session.")


@main.command()
def whoami():
    """Show the logged-in identity."""
    client = get_client()
    click.echo(f"{client.me.handle} ({client.me.did})")


@main.command("register-video")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_video_options
@_validate_option
def register_video(video_path, validate, **video_opts):
    """Register a video: strip identifying metadata, hash, upload, and write a
    signed org.consentvid.video record. Then use attach-consent (or use
    `contribute` to do both in one step)."""
    client = get_client()
    uri, cid, record = _register(client, video_path, validate=validate, **video_opts)
    click.secho("Created video record:", fg="green")
    click.echo(f"  uri:  {uri}\n  cid:  {cid}\n  hash: {record['contentHash']}")
    click.echo(f"Next: consentvid attach-consent {uri} --attest-authorship --attest-adult ...")


@main.command("attach-consent")
@click.argument("video_uri")
@_consent_options
@click.option("--replace", is_flag=True, help="Replace this video's existing consent terms.")
@_validate_option
def attach_consent(video_uri, replace, validate, **terms):
    """Attach a signed consent receipt to one of your videos (one per video)."""
    _require_terms(terms)
    client = get_client()
    uri, cid, record, replaced = _attach(client, video_uri, replace=replace, validate=validate, **terms)
    _print_consent(uri, cid, record, replaced)


@main.command()
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_video_options
@_consent_options
@_validate_option
def contribute(video_path, validate, **opts):
    """Register a video AND attach consent in one step (the usual workflow).

    \b
    Example:
      consentvid contribute clip.mp4 --title "Making pasta" --tag cooking \\
          --attest-authorship --attest-adult --people onlyMe \\
          --modality video --modality audio --purpose training --model-class academic

    Any term you leave out is asked for when run interactively; nothing is
    granted by default.
    """
    consent_keys = (
        "attest",
        "people",
        "adult",
        "modalities",
        "purposes",
        "model_classes",
        "expires_at",
        "compensation",
        "rights_basis",
        "capture_provenance",
    )
    terms = {k: opts.pop(k) for k in consent_keys}
    _require_terms(terms)  # fail before uploading anything
    _parse_expiry(terms["expires_at"])
    client = get_client()
    uri, cid, record = _register(client, video_path, validate=validate, **opts)
    click.secho("Created video record:", fg="green")
    click.echo(f"  uri:  {uri}\n  hash: {record['contentHash']}")
    try:
        c_uri, c_cid, c_record, _ = _attach(client, uri, replace=False, validate=validate, **terms)
    except Exception:
        _warn(f"video registered but consent failed; retry with: consentvid attach-consent {uri}")
        raise
    _print_consent(c_uri, c_cid, c_record)


@main.command("list")
@click.option("--videos", "what", flag_value="videos", help="Only videos.")
@click.option("--consents", "what", flag_value="consents", help="Only consents.")
@click.option("--json", "as_json", is_flag=True, help="Full records as JSON.")
def list_records(what, as_json):
    """List your videos and consent receipts."""
    from consentvid.policy import describe_terms

    client = get_client()
    did = client.me.did
    videos = [] if what == "consents" else _list_all(client, did, VIDEO_NSID)
    consents = [] if what == "videos" else _list_all(client, did, CONSENT_NSID)
    if as_json:
        dump = lambda rs: [{"uri": r.uri, "cid": str(r.cid), "value": to_dict(r.value)} for r in rs]  # noqa: E731
        click.echo(json.dumps({"videos": dump(videos), "consents": dump(consents)}, indent=2))
        return
    by_video: dict[str, list] = {}
    for r in consents:
        by_video.setdefault(to_dict(r.value).get("videoUri"), []).append(r)
    for r in videos:
        v = to_dict(r.value)
        ref = v.get("storageRef", {})
        click.secho(f"{v.get('title')!r}", bold=True)
        click.echo(f"  video:   {r.uri}\n  storage: {ref.get('kind')}  {ref.get('value')}")
        linked = by_video.pop(r.uri, [])
        if not linked:
            click.secho("  consent: none", fg="yellow")
        for c in linked:
            cv = to_dict(c.value)
            state = " (EXPIRED)" if is_expired(cv) else ""
            click.echo(f"  consent: {c.uri}{state}\n           {describe_terms(cv)}")
    for vuri, cs in by_video.items():
        for c in cs:
            click.secho(f"consent {c.uri} -> missing video {vuri}", fg="yellow")
    if not videos and not consents:
        click.echo("Nothing registered yet. Try: consentvid contribute --help")


@main.command()
@click.argument("uri")
@click.option(
    "--delete-bytes", is_flag=True, help="Also delete the video bytes from R2/PeerTube or unpin from IPFS."
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def revoke(uri, delete_bytes, yes):
    """Withdraw consent: delete a consent receipt, or a video together with all
    its consents.

    Datasets learn about it when they run `consentvid resync`, and the AppView
    sees the deletion on the firehose immediately.
    """
    from consentvid import storage

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    if at.collection not in (VIDEO_NSID, CONSENT_NSID):
        raise ConsentVidError(f"not a ConsentVid record: {at.collection}")
    if at.collection == CONSENT_NSID and delete_bytes:
        raise click.UsageError("--delete-bytes applies when revoking a video URI")
    client = get_client()
    did = client.me.did
    if at.did != did:
        raise ConsentVidError(f"not your record (it belongs to {at.did})")

    if at.collection == CONSENT_NSID:
        if not yes:
            click.confirm(f"Withdraw consent {uri}?", abort=True)
        client.com.atproto.repo.delete_record({"repo": did, "collection": CONSENT_NSID, "rkey": at.rkey})
        click.secho(f"Withdrawn: {uri}", fg="green")
        return

    video = to_dict(
        client.com.atproto.repo.get_record({"repo": did, "collection": VIDEO_NSID, "rkey": at.rkey}).value
    )
    linked = [r for r in _list_all(client, did, CONSENT_NSID) if to_dict(r.value).get("videoUri") == uri]
    ref = video.get("storageRef", {})
    click.echo(f"Video {video.get('title')!r} ({ref.get('kind')}: {ref.get('value')})")
    click.echo(f"  {len(linked)} consent receipt(s) will be deleted with it.")
    if delete_bytes:
        click.echo("  The stored bytes will be deleted too.")
    if not yes:
        click.confirm("Proceed?", abort=True)
    for r in linked:
        client.com.atproto.repo.delete_record(
            {"repo": did, "collection": CONSENT_NSID, "rkey": parse_at_uri(r.uri).rkey}
        )
        click.echo(f"Withdrawn: {r.uri}")
    client.com.atproto.repo.delete_record({"repo": did, "collection": VIDEO_NSID, "rkey": at.rkey})
    click.echo(f"Deleted video record: {uri}")
    if delete_bytes:
        other = _bytes_shared(client, did, ref, exclude_uri=uri)
        if other:
            _warn(f"not deleting the bytes: another of your videos uses them ({other})")
        else:
            click.echo(storage.delete_bytes(ref))
    elif storage.public_url(ref):
        _warn(
            f"the bytes are still publicly reachable at {storage.public_url(ref)}; "
            "pass --delete-bytes to remove them"
        )


# ------------------------------------------------------------- datasets ------


def _own_did() -> str:
    handle = os.environ.get("CONSENTVID_HANDLE")
    if handle:
        from consentvid.identity import resolve_did

        return resolve_did(handle)
    return get_client().me.did


def _filter_options(f):
    for opt in reversed(
        [
            click.option(
                "--purpose",
                "purposes",
                multiple=True,
                type=click.Choice(PURPOSES),
                help="Require this purpose. Repeatable (all required).",
            ),
            click.option(
                "--model-class",
                "model_classes",
                multiple=True,
                type=click.Choice(MODEL_CLASSES),
                help="Require this model class. Repeatable (all required).",
            ),
            click.option(
                "--modality",
                "modalities",
                multiple=True,
                type=click.Choice(MODALITIES),
                help="Require this modality. Repeatable (all required).",
            ),
        ]
    ):
        f = opt(f)
    return f


@main.command()
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--repo", default=None, help="Contributor handle or DID (default: you). No login needed for others."
)
@_filter_options
@click.option("--tag", "tags", multiple=True, help="Only videos with any of these tags.")
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def export(output_path, repo, purposes, model_classes, modalities, tags, name, description):
    """Write a Croissant-RAI manifest of one contributor's consented clips."""
    from consentvid.croissant import Clip, build_manifest
    from consentvid.identity import display_name, public_client, resolve_did
    from consentvid.policy import covers, latest_per_video

    did = resolve_did(repo) if repo else _own_did()
    client = public_client(did)
    videos = {r.uri: (str(r.cid), to_dict(r.value)) for r in _list_all(client, did, VIDEO_NSID)}
    consents, cids = [], {}
    for r in _list_all(client, did, CONSENT_NSID):
        c = to_dict(r.value)
        errors = validate_in_repo(CONSENT_NSID, c, did)
        if errors:
            _warn(f"skipping invalid consent {r.uri}: {errors[0]}")
        else:
            consents.append((r.uri, c))
            cids[r.uri] = str(r.cid)
    # Supersession first, expiry second: an expired newer consent must not
    # revive an older, broader one.
    best, superseded = latest_per_video(consents)
    for s in superseded:
        _warn(f"ignoring superseded consent {s} (a newer one exists for the same video)")
    for vuri, (curi, c) in list(best.items()):
        if is_expired(c):
            _warn(f"skipping expired consent {curi}")
            del best[vuri]

    handle = display_name(did)
    clips = []
    for vuri, (curi, c) in best.items():
        if vuri not in videos:
            _warn(f"consent {curi} references missing video {vuri}")
            continue
        vcid, v = videos[vuri]
        if c.get("videoCid") and c["videoCid"] != vcid:
            _warn(f"skipping {curi}: video record changed after consent was given")
            continue
        if not covers(c, purposes=purposes, model_classes=model_classes, modalities=modalities):
            continue
        if tags and not set(tags) & set(v.get("tags") or []):
            continue
        clips.append(Clip(curi, cids[curi], c, vuri, vcid, v, did, handle))

    name = name or f"consentvid-{handle.replace('.', '-').replace(':', '-')}-{date.today()}"
    manifest, skipped = build_manifest(clips, name=name, description=description)
    for clip in skipped:
        _warn(
            f"left out {clip.video.get('title')!r}: its bytes aren't publicly reachable "
            f"(storage kind {clip.video.get('storageRef', {}).get('kind')})"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    click.secho(f"Wrote {output_path}: {len(manifest['distribution'])} clip(s) from {handle}", fg="green")


@main.command()
@click.argument("uri")
@click.option("--json", "as_json", is_flag=True)
def verify(uri, as_json):
    """Cryptographically verify a record against its author's repo (no login).

    Checks the commit signature against the DID's key and the Merkle proof of
    the record's presence or absence, then validates the record.
    """
    from consentvid.verify import fetch_verified

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    rec = fetch_verified(at)
    errors = validate_in_repo(at.collection, rec.value, at.did) if rec.present else []
    if as_json:
        click.echo(
            json.dumps(
                {
                    "uri": uri,
                    "present": rec.present,
                    "cid": rec.cid,
                    "rev": rec.rev,
                    "proven": rec.proven,
                    "errors": errors,
                    "value": rec.value,
                },
                indent=2,
            )
        )
    elif not rec.present:
        how = "proven absent by a signed repo commit" if rec.proven else "reported absent by the PDS"
        click.secho(f"ABSENT — {how} (rev {rec.rev or '?'})", fg="red")
    else:
        click.secho("PRESENT — signature and Merkle proof verified", fg="green")
        click.echo(f"  cid: {rec.cid}\n  rev: {rec.rev}")
        if errors:
            click.secho("  INVALID: " + "; ".join(errors), fg="red")
        click.echo(json.dumps(rec.value, indent=2, default=str))
    if not rec.present or errors:
        sys.exit(1)


_MARK = {
    "valid": ("ok", "green"),
    "revoked": ("REVOKED", "red"),
    "expired": ("EXPIRED", "red"),
    "modified": ("MODIFIED", "red"),
    "invalid": ("INVALID", "red"),
    "unattested": ("UNATTEST", "red"),
    "unavailable": ("UNAVAIL", "red"),
    "unknown": ("?", "yellow"),
}


@main.command()
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--write",
    "write_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a cleaned manifest without the clips that must be dropped.",
)
@click.option(
    "--min-provenance",
    type=click.Choice(CAPTURE_PROVENANCE),
    help="Also drop clips whose capture provenance is below this tier.",
)
@click.option(
    "--strict/--lenient",
    default=True,
    show_default=True,
    help="--strict (default) drops clips that couldn't be checked (e.g. network errors); "
    "--lenient keeps them. A consent-first dataset should not train on unverified consent.",
)
@click.option("--check", is_flag=True, help="Exit with status 1 if any clip must be dropped.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--workers", default=8, show_default=True, help="Parallel checks.")
def resync(input_path, write_path, min_provenance, strict, check, as_json, workers):
    """Verify that every clip's consent still stands, before you use a dataset.

    INPUT_PATH is a Croissant manifest or a C2PA-stamped video file. Each
    consent and video is verified against the contributor's repo with signature
    and Merkle proofs. Run this before every training run. No login needed.
    """
    from consentvid import resync as rs

    try:
        manifest = json.loads(input_path.read_bytes())
    except (UnicodeDecodeError, ValueError):
        manifest = None
    if isinstance(manifest, dict):
        items = rs.items_from_manifest(manifest)
    else:
        manifest = None
        from consentvid import c2pa_embed as ce

        try:
            receipt, c2pa_state = ce.extract_consent_receipt(input_path)
        except ConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConsentVidError(f"{input_path} is neither a manifest nor a readable C2PA file: {e}") from e
        if not receipt:
            raise ConsentVidError("embedded C2PA manifest has no org.consentvid.consent assertion")
        items = [rs.item_from_c2pa(receipt, input_path.name, c2pa_state)]
    if not items:
        raise ConsentVidError("no clips with consent receipts found")

    results = rs.check_all(items, min_provenance=min_provenance, workers=workers)
    drop_statuses = rs.DROP_STATUSES + ((rs.UNKNOWN,) if strict else ())
    n_drop = sum(r.status in drop_statuses for r in results)
    if as_json:
        click.echo(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        for r in results:
            label, color = _MARK[r.status]
            click.secho(f"  [{label:>8}] ", fg=color, nl=False)
            click.echo(f"{r.name}" + (f"  — {r.reason}" if r.reason else ""))
        counts = {s: sum(r.status == s for r in results) for s in rs.STATUSES}
        click.echo("\n" + ", ".join(f"{s}: {n}" for s, n in counts.items() if n))
        click.echo(f"{n_drop} of {len(results)} clip(s) must be dropped before use.")
        if counts[rs.UNKNOWN] and not strict:
            _warn("some clips couldn't be checked (network); --lenient keeps them. Re-run to check again.")
    if write_path:
        if manifest is None:
            raise click.UsageError("--write needs a manifest as input")
        cleaned = rs.filter_manifest(manifest, results, now_iso(), strict=strict)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(json.dumps(cleaned, indent=2) + "\n")
        if not as_json:
            click.echo(f"Wrote {write_path} ({len(cleaned['distribution'])} clip(s)).")
    if check and n_drop:
        sys.exit(1)


@main.command("embed-c2pa")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--consent", "consent_uri", required=True, help="AT-URI of the consent receipt.")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Default: <video>.c2pa.<ext>",
)
def embed_c2pa(video_path, consent_uri, output_path):
    """Stamp a signed C2PA manifest carrying the consent receipt into a copy of
    the video, so provenance survives the file leaving the platform.

    The file must be exactly the registered bytes. Needs the [c2pa] extra.
    """
    from consentvid import c2pa_embed as ce
    from consentvid.identity import display_name
    from consentvid.verify import fetch_verified

    try:
        consent_at = parse_at_uri(consent_uri, CONSENT_NSID)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--consent") from None
    consent = fetch_verified(consent_at)
    if not consent.present:
        raise ConsentVidError(f"{consent_uri} does not exist (withdrawn?)")
    errors = validate_in_repo(CONSENT_NSID, consent.value, consent_at.did)
    if errors:
        raise ConsentVidError(f"consent is invalid: {errors[0]}")
    video_uri = consent.value["videoUri"]
    video = fetch_verified(video_uri)
    if not video.present:
        raise ConsentVidError(f"the consent's video {video_uri} no longer exists")
    if consent.value.get("videoCid") and consent.value["videoCid"] != video.cid:
        raise ConsentVidError("the video record changed after consent was given; consent no longer applies")
    actual = hash_file(video_path)
    if actual != video.value.get("contentHash"):
        raise ConsentVidError(
            f"{video_path.name} is not the registered video\n"
            f"  file:   {actual}\n  record: {video.value.get('contentHash')}"
        )
    output_path = output_path or video_path.with_suffix(f".c2pa{video_path.suffix}")
    out = ce.embed_consent(
        video_path=video_path,
        out_path=output_path,
        consent_uri=consent_uri,
        consent_cid=consent.cid,
        consent=consent.value,
        video_uri=video_uri,
        video_cid=video.cid,
        video=video.value,
        contributor_handle=display_name(consent_at.did),
        contributor_did=consent_at.did,
    )
    click.secho(f"Embedded C2PA consent manifest -> {out}", fg="green")
    _warn(
        "signed with a self-signed prototype certificate; C2PA readers will mark the "
        "signer untrusted. The stamped file's hash differs from the registered one."
    )


@main.command("read-c2pa")
@click.argument("video_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def read_c2pa(video_path):
    """Show the consent receipt embedded in a C2PA-stamped file."""
    from consentvid import c2pa_embed as ce

    try:
        man = ce.read_manifest(video_path)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConsentVidError(f"no readable C2PA manifest in {video_path.name}: {e}") from e
    active = man.get("manifests", {}).get(man.get("active_manifest"), {})
    click.echo(f"validation_state: {man.get('validation_state')}")
    click.echo(f"title:            {active.get('title')}")
    receipt = next(
        (a.get("data") for a in active.get("assertions", []) if a.get("label") == ce.CONSENT_ASSERTION_LABEL),
        None,
    )
    if receipt is None:
        click.echo("No org.consentvid.consent assertion present.")
        return
    click.echo(json.dumps(receipt, indent=2))
    click.echo(f"\nCheck it is still valid: consentvid resync {video_path}")


# ------------------------------------------------------------- lexicons ------


@main.command("publish-lexicons")
@click.option("--nsid", "nsids", multiple=True, help="Only these NSIDs (default: all).")
def publish_lexicons(nsids):
    """Publish the lexicons as com.atproto.lexicon.schema records in your repo."""
    from consentvid import lexicons as lx

    client = get_client()
    targets = list(nsids) or lx.discover_lexicons()
    for nsid in targets:
        record = lx.build_schema_record(lx.load_lexicon(nsid))
        client.com.atproto.repo.put_record(
            {
                "repo": client.me.did,
                "collection": lx.SCHEMA_COLLECTION,
                "rkey": nsid,
                "validate": False,
                "record": record,
            }
        )
        click.echo(f"Published {nsid} -> at://{client.me.did}/{lx.SCHEMA_COLLECTION}/{nsid}")
    by_authority = {lx.nsid_authority(n): n for n in targets}
    for auth, nsid in sorted(by_authority.items()):
        if lx.resolve_did_via_dns(nsid) == client.me.did:
            click.echo(f"DNS _lexicon.{auth} already points at you.")
        else:
            click.echo(f'Add DNS TXT:  _lexicon.{auth}  "did={client.me.did}"')


@main.command("resolve-lexicon")
@click.argument("nsid")
@click.option("--did", "did_override", default=None, help="Skip DNS; read from this DID.")
def resolve_lexicon(nsid, did_override):
    """Resolve a lexicon like a validator would (DNS -> DID -> schema record)
    and compare it with the local copy. No login needed."""
    from consentvid import lexicons as lx
    from consentvid.identity import public_client

    did = did_override or lx.resolve_did_via_dns(nsid)
    if not did:
        raise ConsentVidError(f"no TXT record at {lx.dns_record_name(nsid)}")
    click.echo(f"{lx.dns_record_name(nsid)} -> {did}")
    resp = public_client(did).com.atproto.repo.get_record(
        {"repo": did, "collection": lx.SCHEMA_COLLECTION, "rkey": nsid}
    )
    click.echo(f"Resolved {resp.uri}")
    try:
        same = lx.schema_matches(lx.load_lexicon(nsid), to_dict(resp.value))
    except FileNotFoundError:
        click.echo("(no local copy to compare)")
        return
    if same:
        click.secho("Published schema matches the local lexicon.", fg="green")
    else:
        _warn("published schema differs from the local lexicon: run `consentvid publish-lexicons`")


# --------------------------------------------------------------- doctor ------


@main.command()
def doctor():
    """Check your setup: login, tools, storage backends, lexicons, AppView."""
    from consentvid import appview as av
    from consentvid import lexicons as lx
    from consentvid import media, storage
    from consentvid.identity import public_client

    def line(ok, label, detail=""):
        mark, color = {True: ("ok", "green"), False: ("!!", "red"), None: ("--", "yellow")}[ok]
        click.secho(f"  [{mark}] ", fg=color, nl=False)
        click.echo(label + (f": {detail}" if detail else ""))

    click.echo(f"consentvid {__version__}, Python {sys.version.split()[0]}")
    line(sys.version_info < (3, 14), "Python < 3.14", "the atproto SDK breaks on 3.14")
    try:
        client = get_client()
        line(True, "logged in", f"{client.me.handle} ({client.me.did})")
    except Exception as e:  # noqa: BLE001
        line(False, "logged in", str(e).splitlines()[0])
    line(
        media.have_ffprobe() or None,
        "ffprobe",
        "duration, size, metadata check" if media.have_ffprobe() else "missing (brew install ffmpeg)",
    )
    line(
        media.have_ffmpeg() or None,
        "ffmpeg",
        "metadata stripping" if media.have_ffmpeg() else "missing: can't strip GPS/device tags",
    )
    for kind in storage.UPLOAD_KINDS:
        ok = storage.configured(kind)
        line(ok or None, f"storage {kind}", "configured" if ok else "not configured")
    click.echo(f"       default storage: {_default_storage()}")
    try:
        import c2pa  # noqa: F401

        line(True, "c2pa extra", "installed")
    except ImportError:
        line(None, "c2pa extra", 'not installed (pip install "consentvid[c2pa]")')
    for nsid in lx.discover_lexicons():
        try:
            did = lx.resolve_did_via_dns(nsid)
            if not did:
                line(None, f"lexicon {nsid}", "no _lexicon DNS record")
                continue
            pub = to_dict(
                public_client(did)
                .com.atproto.repo.get_record({"repo": did, "collection": lx.SCHEMA_COLLECTION, "rkey": nsid})
                .value
            )
            same = lx.schema_matches(lx.load_lexicon(nsid), pub)
            line(
                same,
                f"lexicon {nsid}",
                "published, up to date" if same else "published copy is outdated: run publish-lexicons",
            )
        except Exception as e:  # noqa: BLE001
            line(False, f"lexicon {nsid}", str(e)[:100])
    if av.db_path().exists():
        s = av.stats(av.connect())
        line(True, "appview index", f"{s['consents']} consents, {s['videos']} videos, cursor {s['cursor']}")
    else:
        line(None, "appview index", "not created yet")


# -------------------------------------------------------------- appview ------


@main.group()
def appview():
    """Index ConsentVid records from the whole network (local SQLite)."""


@appview.command("backfill")
@click.argument("repos", nargs=-1, required=True)
def appview_backfill(repos):
    """Index existing records from one or more contributors (handles or DIDs)."""
    from consentvid import appview as av
    from consentvid.identity import public_client, resolve_did

    conn = av.connect()
    for repo in repos:
        did = resolve_did(repo)
        client = public_client(did)
        stored = invalid = 0
        for collection in (VIDEO_NSID, CONSENT_NSID):
            live = set()
            for r in _list_all(client, did, collection):
                live.add(r.uri)
                errors = av.ingest(
                    conn, collection, r.uri, did, parse_at_uri(r.uri).rkey, str(r.cid), to_dict(r.value)
                )
                if errors:
                    invalid += 1
                    click.echo(f"  skipped {r.uri}: {errors[0]}")
                else:
                    stored += 1
            # Anything indexed earlier that's gone now was deleted while we
            # weren't watching.
            table = "videos" if collection == VIDEO_NSID else "consents"
            gone = [
                row["uri"]
                for row in conn.execute(f"SELECT uri FROM {table} WHERE did = ?", (did,)).fetchall()
                if row["uri"] not in live
            ]
            for uri in gone:
                if collection == VIDEO_NSID:
                    av.delete_video(conn, uri)
                else:
                    av.revoke_consent(conn, uri, "consent deleted")
        av.set_account_status(conn, did, True, None)
        conn.commit()
        click.echo(f"{repo}: {stored} record(s) indexed, {invalid} skipped")


@appview.command("index")
@click.option("--stop-after", type=int, default=None, help="Exit after N relevant events.")
def appview_index(stop_after):
    """Follow the firehose (Jetstream) and keep the index current. Resumes from
    where it stopped."""
    from consentvid import appview as av

    conn = av.connect()
    s = av.stats(conn)
    click.echo(
        f"Indexing into {av.db_path()}"
        + (f", resuming from {s['cursor']}" if s["cursor"] else "")
        + " (Ctrl-C to stop)"
    )
    try:
        n = asyncio.run(
            av.index_loop(conn, on_event=lambda op, x: click.echo(f"  [{op}] {x}"), stop_after=stop_after)
        )
        click.echo(f"Processed {n} events.")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@appview.command("stats")
def appview_stats():
    """Show index counts and freshness."""
    from consentvid import appview as av

    for k, v in av.stats(av.connect()).items():
        click.echo(f"{k + ':':<19}{v}")


@appview.command("revocations")
@click.option("--since", default=None, help="Only revocations after this ISO timestamp.")
@click.option("--json", "as_json", is_flag=True)
def appview_revocations(since, as_json):
    """Feed of withdrawn consents (poll with --since for incremental re-sync)."""
    from consentvid import appview as av

    rows = av.revocations(av.connect(), since)
    if as_json:
        click.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    for r in rows:
        click.echo(f"{r['revoked_at']}  {r['uri']}  ({r['revoke_reason'] or 'deleted'})")
    click.echo(f"{len(rows)} revocation(s)" + (f" since {since}" if since else "") + ".")


@appview.command("query")
@_filter_options
@click.option("--capture-provenance", "provenance", multiple=True, type=click.Choice(CAPTURE_PROVENANCE))
@click.option("--repo", default=None, help="Only this contributor (handle or DID).")
@click.option("--include-revoked", is_flag=True, help="Also show withdrawn/inactive consents.")
@click.option("--include-expired", is_flag=True)
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def appview_query(
    purposes, model_classes, modalities, provenance, repo, include_revoked, include_expired, limit, as_json
):
    """Find consented clips across contributors."""
    from consentvid import appview as av
    from consentvid.identity import resolve_did

    rows = av.query_consents(
        av.connect(),
        purposes=purposes,
        model_classes=model_classes,
        modalities=modalities,
        provenance=provenance,
        did=resolve_did(repo) if repo else None,
        include_revoked=include_revoked,
        include_expired=include_expired,
        limit=limit,
    )
    if as_json:
        click.echo(
            json.dumps(
                [{k: r[k] for k in r.keys() if k not in ("record", "video_record")} for r in rows], indent=2
            )
        )
        return
    for r in rows:
        state = f"  REVOKED ({r['revoke_reason']})" if r["revoked_at"] else ""
        click.secho(f"{r['title']!r}{state}", bold=True)
        click.echo(
            f"  consent:    {r['uri']}\n  purposes:   {', '.join(json.loads(r['purposes']))}"
            f"\n  models:     {', '.join(json.loads(r['model_classes']))}"
            f"\n  modalities: {', '.join(json.loads(r['modalities']))}"
            f"\n  provenance: {r['capture_provenance']}"
        )
    click.echo(f"{len(rows)} result(s).")


@appview.command("export")
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@_filter_options
@click.option("--capture-provenance", "provenance", multiple=True, type=click.Choice(CAPTURE_PROVENANCE))
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def appview_export(output_path, purposes, model_classes, modalities, provenance, name, description):
    """Build a multi-contributor Croissant-RAI manifest from the index.

    Run `consentvid resync` on the result before use: the index is fed by an
    unsigned firehose, resync verifies everything cryptographically.
    """
    from consentvid import appview as av
    from consentvid.croissant import Clip, build_manifest
    from consentvid.identity import display_name

    rows = av.query_consents(
        av.connect(),
        purposes=purposes,
        model_classes=model_classes,
        modalities=modalities,
        provenance=provenance,
        limit=None,
    )
    clips, missing = [], 0
    for r in rows:
        if not r["record"] or not r["video_record"]:
            missing += 1
            continue
        clips.append(
            Clip(
                r["uri"],
                r["cid"],
                json.loads(r["record"]),
                r["video_uri"],
                r["current_video_cid"],
                json.loads(r["video_record"]),
                r["did"],
                display_name(r["did"]),
            )
        )
    if missing:
        _warn(
            f"{missing} indexed consent(s) predate full-record storage; "
            "re-run `appview backfill` for their contributors"
        )
    manifest, skipped = build_manifest(
        clips, name=name or f"consentvid-{date.today()}", description=description
    )
    if skipped:
        _warn(f"left out {len(skipped)} clip(s) without publicly reachable bytes")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    click.secho(
        f"Wrote {output_path}: {len(manifest['distribution'])} clip(s). "
        f"Next: consentvid resync {output_path}",
        fg="green",
    )


if __name__ == "__main__":
    main()
