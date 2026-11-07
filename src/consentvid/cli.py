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
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import click
from atproto import Client
from atproto_client.exceptions import AtProtocolError, NetworkError
from dotenv import dotenv_values

from consentvid import __version__
from consentvid.errors import ConfigError, ConsentVidError
from consentvid.paths import secure_dir, write_private
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
    safe_text,
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


# Settings an env file may set. Never CONSENTVID_HOME (read at import time),
# the env-file pointer itself, or the insecure-transport escape hatch, and
# never anything outside our prefix (HTTPS_PROXY, SSL_CERT_FILE, ... would
# subvert the TLS trust in plc.directory that resync relies on).
_ENV_PREFIX = "CONSENTVID_"
_ENV_FILE_DENY = frozenset({"CONSENTVID_HOME", "CONSENTVID_ENV_FILE", "CONSENTVID_ALLOW_INSECURE"})


def _checkout_env() -> Path | None:
    """The source checkout's .env (editable installs), if this really is one."""
    root = Path(__file__).resolve().parents[2]
    try:
        if 'name = "consentvid"' in (root / "pyproject.toml").read_text(encoding="utf-8"):
            return root / ".env"
    except OSError:
        pass
    return None


def _env_files() -> list[Path]:
    """Env files in precedence order. Never the current directory or its
    parents: datasets are downloaded and opened there, and a planted .env
    could redirect credentials or the DID-resolution trust root."""
    files = []
    explicit = os.environ.get("CONSENTVID_ENV_FILE")  # real environment only
    if explicit:
        files.append(Path(explicit).expanduser())
    files.append(CONFIG_DIR / "config.env")
    checkout = _checkout_env()
    if checkout:
        files.append(checkout)
    return files


def _env_file_too_open(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def _apply_env_files() -> None:
    """Settings precedence: real env > $CONSENTVID_ENV_FILE >
    ~/.consentvid/config.env > the source checkout's .env. Only CONSENTVID_*
    keys are taken from files."""
    for path in _env_files():
        if not path.is_file():
            continue
        if _env_file_too_open(path):
            _warn(f"{path} may contain credentials but is readable by other users: chmod 600 {path}")
        for key, value in dotenv_values(path, interpolate=False).items():
            if (
                key.startswith(_ENV_PREFIX)
                and key not in _ENV_FILE_DENY
                and value is not None
                and key not in os.environ
            ):
                os.environ[key] = value


def _load_env() -> None:
    _apply_env_files()


def _secure_config_dir() -> None:
    secure_dir(CONFIG_DIR)


def _safe(s) -> str:
    """An untrusted value, safe to print: control/format characters (ESC, CR,
    BEL, bidi overrides, U+2028, ...) become visible escapes."""
    return safe_text(s)


def _xrpc_message(e: AtProtocolError) -> str:
    content = getattr(getattr(e, "response", None), "content", None)
    err, msg = getattr(content, "error", None), getattr(content, "message", None)
    if err or msg:
        return _safe(f"{err or 'Error'}: {msg or ''}".strip())
    if isinstance(e, NetworkError):
        return f"network error talking to the PDS: {_safe(e)}"
    return _safe(str(e) or type(e).__name__)


class _CLI(click.Group):
    """Turn expected failures into one-line errors instead of tracebacks."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ConsentVidError as e:
            raise click.ClickException(_safe_lines(str(e))) from e
        except AtProtocolError as e:
            raise click.ClickException(_xrpc_message(e)) from e


def _safe_lines(msg: str) -> str:
    """Like _safe, but keeps the message's own line breaks."""
    return "\n".join(_safe(line) for line in str(msg).split("\n"))


def _warn(msg: str) -> None:
    # Callers wrap untrusted values in _safe(); this is the backstop.
    click.secho(f"warning: {_safe_lines(msg)}", fg="yellow", err=True)


def _service_url() -> str:
    from consentvid.storage import require_https

    return require_https("CONSENTVID_SERVICE", os.environ.get("CONSENTVID_SERVICE") or "https://bsky.social")


def _new_client() -> Client:
    client = Client(_service_url())

    # Persist every new or refreshed session; otherwise the cached refresh
    # token eventually expires and every command fails with ExpiredToken.
    def _save(_event, _session):
        _secure_config_dir()
        write_private(SESSION_PATH, client.export_session_string())

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
    """Bounded listRecords pagination (see identity.list_all)."""
    from consentvid.identity import list_all

    return list_all(client, did, collection)


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
                help="Existing pointer (URL or CID). Omit for r2/ipfs/peertube to upload. "
                "The file must already be stripped of metadata: if not, a clean copy is "
                "written next to it for you to host instead.",
            ),
            click.option(
                "--keep-metadata",
                is_flag=True,
                help="Do NOT strip location/device metadata from the file (not recommended). "
                "Without it every clip is stripped, whatever the storage kind (needs ffmpeg).",
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


def _refuse_attested(ctx, param, value):
    """'attested' is documented as capture-time cryptographic provenance, but
    nothing verifies it yet: writing or filtering on it would pass a
    self-declared string off as proof (R3-05)."""
    values = value if isinstance(value, tuple) else (value,)
    if "attested" in values:
        raise click.BadParameter(
            "'attested' is reserved for capture-time cryptographic provenance (C2PA/CAWG bound to "
            "your DID), which ConsentVid cannot verify yet; use selfAttested.",
            ctx=ctx,
            param=param,
        )
    return value


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
                callback=_refuse_attested,
                help="'attested' is reserved for capture-time cryptographic provenance, which "
                "ConsentVid cannot verify yet, so it is refused.",
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
    """Strip identifying metadata unless told not to. Returns
    (path_to_publish, probe_or_None, content_hash); the record's contentHash
    is the hash of exactly the bytes at the returned path.

    Every kind is sanitised (ffprobe can't see every vendor box or in-band SEI,
    so its tags never decide), to a fixed point: sanitising the result again
    changes nothing. Then:

    * uploads publish the clean copy from the private workdir;
    * if the file already IS that fixed point, it is used as is (so re-running
      on a clean copy is accepted);
    * otherwise 'local' commits to a clean copy written next to the original,
      and pointer kinds (url, or --storage-value) are refused with a clean copy
      to host instead, because the bytes behind the pointer still carry the
      metadata.

    ffmpeg is required unless --keep-metadata."""
    from consentvid import media, storage

    uploading = kind in storage.UPLOAD_KINDS and not pointer_given
    probe = media.probe(path)
    if keep_metadata:
        if probe is None:
            _warn("ffprobe not found: can't read duration/size or check for metadata (brew install ffmpeg)")
        elif probe.needs_sanitising:
            found = list({**probe.sensitive_tags, **probe.unknown_tags}.items())[:6]
            _warn("publishing WITH identifying metadata: " + ", ".join(f"{_safe(k)}={_safe(v)}" for k, v in found))
        return path, probe, hash_file(path)
    if probe is None:  # (sanitise() also refuses without ffmpeg)
        raise ConfigError(
            "ffmpeg is required to strip identifying metadata (GPS, device, serials) before "
            "publishing; install it (brew install ffmpeg) or pass --keep-metadata"
        )
    found = list({**probe.sensitive_tags, **probe.unknown_tags}.items())[:6]
    click.echo("Stripping identifying metadata (lossless, no re-encode):")
    for k, v in found:
        click.echo(f"  - {_safe(k)} = {_safe(v)}")
    for extra in probe.extra_streams:
        click.echo(f"  - extra stream {_safe(extra)}")
    clean, clean_probe, clean_hash = _sanitise_to_fixed_point(path, workdir)
    if uploading:
        return clean, clean_probe, clean_hash
    if clean_hash == hash_file(path):
        return path, clean_probe, clean_hash  # already clean: the file itself
    out = path.with_name(f"{path.stem}.clean{path.suffix}")
    media.publish_file(clean, out)
    if pointer_given or kind != "local":
        raise ConsentVidError(
            f"{path.name} contains metadata that sanitising removes, so the bytes at "
            f"--storage-value would too. A clean copy was written to {out}: host that file "
            "instead and re-run on it with its pointer, drop --storage-value to let consentvid "
            "upload it, or use --keep-metadata."
        )
    # Kept by the contributor: the clean copy lives next to the original.
    click.echo(f"  -> clean copy: {out}  (the record commits to this file's hash)")
    return out, clean_probe, clean_hash


# Remuxing a remux can still shift a few bytes (edit lists, Matroska timing);
# real files settle by the 2nd or 3rd pass. One spare pass, then fail closed.
_SANITISE_PASSES = 4


def _sanitise_to_fixed_point(src: Path, workdir: Path):
    """Sanitise repeatedly until a pass no longer changes the bytes. Returns
    (path, probe, hash) of that fixed point, in ``workdir``. Fails closed if
    no fixed point is reached within _SANITISE_PASSES passes."""
    from consentvid import media

    prev, cur = hash_file(src), src
    for i in range(1, _SANITISE_PASSES + 1):
        out = Path(workdir) / f"pass{i}{src.suffix.lower()}"
        result = media.sanitise(cur, out)
        h = result.content_hash or hash_file(out)
        if h == prev:
            return out, result, h
        prev, cur = h, out
    raise ConsentVidError(
        f"stripping metadata from {src.name} did not converge after {_SANITISE_PASSES} passes; "
        "refusing to publish bytes that may still carry it (re-encode the video, or use --keep-metadata)"
    )


def _backend_identity(ref: dict) -> tuple[str, str] | None:
    """What actually identifies the stored object, independent of how the
    pointer is spelled (ipfs:// prefix, public base URL, trailing slash)."""
    if not isinstance(ref, dict):
        return None
    kind, value = ref.get("kind"), ref.get("value")
    if not isinstance(value, str) or not value:
        return None
    if kind == "r2":
        m = re.search(r"/(videos/sha256-[0-9a-f]{64})/?$", value)
        return ("r2", m.group(1)) if m else ("r2", value)
    if kind == "ipfs":
        return ("ipfs", value.removeprefix("ipfs://"))
    if kind == "peertube":
        return ("peertube", value.rstrip("/").rsplit("/", 1)[-1])
    return (str(kind), value)


def _bytes_shared(
    client: Client, did: str, ref: dict, content_hash: str | None, exclude_uri: str | None = None
) -> str | None:
    """URI of another of your video records using the same stored bytes, if
    any: same contentHash (R2 is content-addressed, so equal hashes share one
    object) or the same backend object however its pointer is spelled."""
    ident = _backend_identity(ref)
    for r in _list_all(client, did, VIDEO_NSID):
        if r.uri == exclude_uri:
            continue
        v = to_dict(r.value)
        if not isinstance(v, dict):
            continue
        if content_hash and v.get("contentHash") == content_hash:
            return r.uri
        if ident and _backend_identity(v.get("storageRef") or {}) == ident:
            return r.uri
    return None


_PENDING_UPLOAD = "pending-upload"


def _validation_view(record: dict) -> dict:
    """The record as validated before its upload pointer exists: the
    placeholder is swapped for a syntactically valid stand-in of its kind."""
    from consentvid import storage

    ref = record.get("storageRef") or {}
    if ref.get("value") != _PENDING_UPLOAD:
        return record
    kind = ref.get("kind")
    stand_in = "b" + "a" * 58 if kind == "ipfs" else "https://pending-upload.invalid/x"
    # The configured public base ends up in the record: check it (host, port)
    # before anything is uploaded.
    try:
        if kind == "r2":
            stand_in = f"{storage._r2_public_base()}/videos/{record.get('contentHash')}"
        elif kind == "peertube":
            base = storage.require_https("CONSENTVID_PEERTUBE_URL", os.environ.get("CONSENTVID_PEERTUBE_URL") or "")
            stand_in = f"{base.rstrip('/')}/w/pendingupload"
    except ConfigError:
        pass  # not configured: storage.upload refuses it
    return {**record, "storageRef": {**ref, "value": stand_in}}


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
    if storage_value is not None:
        from consentvid.validate import storage_ref_errors

        errs = storage_ref_errors({"kind": kind, "value": storage_value})
        if errs:
            raise click.BadParameter(errs[0], param_hint="--storage-value")

    _secure_config_dir()
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as tmp:
        path, probe, content_hash = _prepare_bytes(video_path, kind, keep_metadata, Path(tmp), bool(storage_value))

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
            # A content-derived name: camera file names (PXL_20261106_101530123,
            # VID-YYYYMMDD-WA..., DJI_0001) leak the capture time and device
            # that sanitising strips, and the record is public.
            record["storageRef"]["value"] = f"{content_hash}{path.suffix.lower()}"
            click.echo(
                f"Published file name: {record['storageRef']['value']} (rename or link your local copy "
                "to it if you want tools to find it)"
            )
        uploading = kind in storage.UPLOAD_KINDS and not storage_value
        if uploading:
            record["storageRef"]["value"] = _PENDING_UPLOAD
        # Validate BEFORE uploading so a bad record never leaves orphaned bytes.
        _check_before_write(VIDEO_NSID, _validation_view(record), did)

        uploaded = False  # True only if THIS call created the stored object
        try:
            if uploading:
                click.echo(f"Uploading to {kind} ...")
                result = storage.upload(kind, path, content_hash=content_hash, title=title)
                record["storageRef"]["value"] = result.value
                uploaded = result.created
                click.echo(f"  -> {record['storageRef']['value']}")
                _check_before_write(VIDEO_NSID, record, did)
            resp = client.com.atproto.repo.create_record(_write_params(did, VIDEO_NSID, record, validate))
        except Exception:
            if uploaded:
                try:
                    other = _bytes_shared(client, did, record["storageRef"], content_hash)
                except Exception:  # noqa: BLE001 — can't tell: keep the bytes
                    other = "an unknown record (could not check)"
                if other:
                    _warn(f"record creation failed; keeping the stored bytes (also used by {other})")
                else:
                    _warn("record creation failed; removing the uploaded bytes")
                    try:
                        storage.delete_bytes(record["storageRef"], content_hash)
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
    click.echo(f"Signing in to {_service_url()} ...")
    client.login(handle.removeprefix("@"), pw)
    click.echo(f"Logged in as {client.me.handle} ({client.me.did})")


@main.command()
def logout():
    """End the session on the server and delete the cached copy."""
    if not SESSION_PATH.exists():
        click.echo("No cached session.")
        return
    try:
        client = _new_client()
        client._import_session_string(SESSION_PATH.read_text().strip())  # no network
        # deleteSession authenticates with the REFRESH token and invalidates it.
        client.com.atproto.server.delete_session(
            headers=client._get_refresh_auth_headers(), ignore_session_check=True
        )
        click.echo("Server session ended.")
    except Exception as e:  # noqa: BLE001 — best effort: always remove the local copy
        _warn(f"could not end the session on the server ({type(e).__name__}: {e}); it expires on its own")
    finally:
        SESSION_PATH.unlink(missing_ok=True)
    click.echo("Logged out.")
    click.echo(
        "If you think the session or app password leaked, revoke the app password at "
        "https://bsky.app/settings/app-passwords"
    )


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
        click.secho(_safe(repr(v.get("title"))), bold=True)
        click.echo(f"  video:   {_safe(r.uri)}\n  storage: {_safe(ref.get('kind'))}  {_safe(ref.get('value'))}")
        linked = by_video.pop(r.uri, [])
        if not linked:
            click.secho("  consent: none", fg="yellow")
        for c in linked:
            cv = to_dict(c.value)
            state = " (EXPIRED)" if is_expired(cv) else ""
            click.echo(f"  consent: {_safe(c.uri)}{state}\n           {_safe(describe_terms(cv))}")
    for vuri, cs in by_video.items():
        for c in cs:
            click.secho(f"consent {_safe(c.uri)} -> missing video {_safe(vuri)}", fg="yellow")
    if not videos and not consents:
        click.echo("Nothing registered yet. Try: consentvid contribute --help")


@main.command()
@click.argument("uri")
@click.option(
    "--delete-bytes", is_flag=True, help="Also delete the video bytes from R2/PeerTube or unpin from IPFS."
)
@click.option(
    "--all",
    "all_for_video",
    is_flag=True,
    help="When revoking a consent, also withdraw your other consents for the same video "
    "(otherwise the newest remaining one governs).",
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def revoke(uri, delete_bytes, all_for_video, yes):
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
        consents = [(r.uri, to_dict(r.value)) for r in _list_all(client, did, CONSENT_NSID)]
        video_uri = next((v.get("videoUri") for u, v in consents if u == uri), None)
        siblings = [u for u, v in consents if u != uri and video_uri and v.get("videoUri") == video_uri]
        if not yes:
            click.confirm(f"Withdraw consent {uri}?", abort=True)
        client.com.atproto.repo.delete_record({"repo": did, "collection": CONSENT_NSID, "rkey": at.rkey})
        click.secho(f"Withdrawn: {uri}", fg="green")
        if siblings:
            _warn(
                f"{len(siblings)} other consent(s) for the same video remain; the newest of them "
                "now governs this video:\n  " + "\n  ".join(siblings)
            )
            if all_for_video or (not yes and _interactive() and click.confirm("Withdraw them too?", default=True)):
                for s_uri in siblings:
                    client.com.atproto.repo.delete_record(
                        {"repo": did, "collection": CONSENT_NSID, "rkey": parse_at_uri(s_uri).rkey}
                    )
                    click.secho(f"Withdrawn: {s_uri}", fg="green")
            else:
                click.echo("Withdraw them with `consentvid revoke <uri>`, or re-run with --all.")
        return

    video = to_dict(
        client.com.atproto.repo.get_record({"repo": did, "collection": VIDEO_NSID, "rkey": at.rkey}).value
    )
    linked = [r for r in _list_all(client, did, CONSENT_NSID) if to_dict(r.value).get("videoUri") == uri]
    ref = video.get("storageRef", {})
    click.echo(f"Video {_safe(repr(video.get('title')))} ({_safe(ref.get('kind'))}: {_safe(ref.get('value'))})")
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
        other = _bytes_shared(client, did, ref, video.get("contentHash"), exclude_uri=uri)
        if other:
            _warn(f"not deleting the bytes: another of your videos uses them ({other})")
        else:
            try:
                click.echo(storage.delete_bytes(ref, video.get("contentHash") or ""))
            except Exception as e:  # noqa: BLE001 — the records are gone; say the bytes may not be
                where = storage.public_url(ref) or ref.get("value")
                raise ConsentVidError(
                    f"the records were deleted, but deleting the bytes failed ({_safe(e)}). "
                    f"They may still be public at {_safe(where)}: delete them manually."
                ) from e
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
        if not isinstance(c, dict) or not isinstance(c.get("videoUri"), str):
            _warn(f"skipping malformed consent {_safe(r.uri)}")
            continue
        consents.append((r.uri, c))
        cids[r.uri] = str(r.cid)
    # Supersession first (over ALL consents, invalid ones included), then
    # validity, then expiry: an invalid or expired newer consent must not
    # revive an older, broader one.
    best, superseded = latest_per_video(consents)
    for s in superseded:
        _warn(f"ignoring superseded consent {_safe(s)} (a newer one exists for the same video)")
    for vuri, (curi, c) in list(best.items()):
        errors = validate_in_repo(CONSENT_NSID, c, did)
        if errors:
            _warn(
                f"leaving out {_safe(vuri)}: its newest consent {_safe(curi)} is invalid "
                f"({_safe(errors[0])}), so nothing is granted"
            )
            del best[vuri]
        elif is_expired(c):
            _warn(f"skipping expired consent {_safe(curi)}")
            del best[vuri]

    handle = display_name(did)
    clips = []
    for vuri, (curi, c) in best.items():
        if vuri not in videos:
            _warn(f"consent {_safe(curi)} references missing video {_safe(vuri)}")
            continue
        vcid, v = videos[vuri]
        if c.get("videoCid") and c["videoCid"] != vcid:
            _warn(f"skipping {_safe(curi)}: video record changed after consent was given")
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
            f"left out {_safe(repr(clip.video.get('title')))}: its bytes aren't publicly reachable "
            f"(storage kind {_safe(clip.video.get('storageRef', {}).get('kind'))})"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    click.secho(f"Wrote {output_path}: {len(manifest['distribution'])} clip(s) from {_safe(handle)}", fg="green")


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
        click.secho(f"ABSENT — {how} (rev {_safe(rec.rev or '?')})", fg="red")
    else:
        click.secho("PRESENT — signature and Merkle proof verified", fg="green")
        click.echo(f"  cid: {_safe(rec.cid)}\n  rev: {_safe(rec.rev)}")
        if errors:
            click.secho("  INVALID: " + "; ".join(_safe(e) for e in errors), fg="red")
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
    "unbound": ("UNBOUND", "red"),
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
    help="Also drop clips whose capture provenance is below this tier. 'attested' is not "
    "verifiable yet (a signed 'attested' counts as selfAttested), so it drops every clip.",
)
@click.option(
    "--strict/--lenient",
    default=True,
    show_default=True,
    help="--strict (default) drops clips that couldn't be checked (network errors, PDS 5xx); "
    "--lenient keeps them as they are, unverified. Whoever runs a contributor's PDS can make "
    "its clips uncheckable, so only use --lenient on manifests you built yourself.",
)
@click.option("--check", is_flag=True, help="Exit with status 1 if any clip must be dropped.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--workers", default=8, show_default=True, help="Parallel checks.")
def resync(input_path, write_path, min_provenance, strict, check, as_json, workers):
    """Verify that every clip's consent still stands, before you use a dataset.

    INPUT_PATH is a Croissant manifest or a C2PA-stamped video file. Each
    consent and video is verified against the contributor's repo with signature
    and Merkle proofs. Run this before every training run. No login needed.

    A C2PA file is reported UNBOUND at best: its embedded receipt points at the
    consent, but nothing binds that file's bytes to it. Use the registered
    bytes (same sha256) instead. --write rebuilds every kept manifest entry
    from the verified records.
    """
    from consentvid import resync as rs

    try:
        manifest = rs.load_manifest_json(input_path.read_bytes())
    except ConsentVidError:
        raise  # duplicate keys / nesting: a manifest, but not one we can trust
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
            raise ConsentVidError(
                f"{input_path} is neither a manifest nor a readable C2PA file: {_safe(e)}"
            ) from e
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
            # Names and reasons come from the manifest and the PDS: untrusted.
            click.echo(_safe(r.name) + (f"  — {_safe(r.reason)}" if r.reason else ""))
        counts = {s: sum(r.status == s for r in results) for s in rs.STATUSES}
        reset = click.style("", reset=True)
        click.echo(reset + "\n" + ", ".join(f"{s}: {n}" for s, n in counts.items() if n))
        click.echo(reset + f"{n_drop} of {len(results)} clip(s) must be dropped before use.")
        if counts[rs.UNKNOWN] and not strict:
            _warn("some clips couldn't be checked (network); --lenient keeps them. Re-run to check again.")
        if manifest is not None and not write_path:
            click.echo(
                "Note: dataset-level text (description, rai:*, creator) is not verified; use --write "
                "to get a manifest rebuilt from signed records only."
            )
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
    help="Default: <video>.c2pa.<ext>. An existing file (or symlink) there is refused, never overwritten.",
)
def embed_c2pa(video_path, consent_uri, output_path):
    """Stamp a signed C2PA manifest carrying the consent receipt into a copy of
    the video, so a pointer to the consent survives the file leaving the
    platform. It is a pointer, not proof: resync reports stamped files as
    UNBOUND (the stamped bytes differ from the registered ones).

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
        "signer untrusted. The stamped file's hash differs from the registered one, so "
        "`consentvid resync` reports it UNBOUND: the receipt points at the consent but does "
        "not prove this file is the consented bytes. Share the registered bytes for training."
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
        raise ConsentVidError(f"no readable C2PA manifest in {video_path.name}: {_safe(e)}") from e
    active = man.get("manifests", {}).get(man.get("active_manifest"), {})
    click.echo(f"validation_state: {_safe(man.get('validation_state'))}")
    click.echo(f"title:            {_safe(active.get('title'))}")
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
    click.echo(f"{lx.dns_record_name(nsid)} -> {_safe(did)}")
    resp = public_client(did).com.atproto.repo.get_record(
        {"repo": did, "collection": lx.SCHEMA_COLLECTION, "rkey": nsid}
    )
    click.echo(f"Resolved {_safe(resp.uri)}")
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
    for env_file in _env_files():
        if env_file.is_file():
            too_open = _env_file_too_open(env_file)
            line(
                not too_open,
                f"settings file {env_file}",
                f"readable by other users: chmod 600 {env_file}" if too_open else "private (0600)",
            )
    try:
        client = get_client()
        line(True, "logged in", f"{client.me.handle} ({client.me.did})")
    except Exception as e:  # noqa: BLE001
        line(False, "logged in", _safe((str(e).splitlines() or [""])[0]))
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
            line(False, f"lexicon {nsid}", _safe(str(e)[:100]))
    if av.db_path().exists():
        s = av.stats(av.connect())
        line(True, "appview index", f"{s['consents']} consents, {s['videos']} videos, cursor {s['cursor']}")
        if s.get("gap_since"):
            line(
                None,
                "appview gap",
                f"events after {s['gap_since']} may be missing (oversized message); run `consentvid appview backfill`",
            )
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

    conn = av.connect()
    for repo in repos:
        try:
            stored, invalid = _backfill_repo(conn, repo)
        except Exception as e:  # noqa: BLE001 — one bad repo must not stop the others
            conn.rollback()
            _warn(f"{_safe(repo)}: backfill failed ({type(e).__name__}: {_safe(e)}); continuing")
            continue
        click.echo(f"{_safe(repo)}: {stored} record(s) indexed, {invalid} skipped")


def _backfill_repo(conn, repo: str) -> tuple[int, int]:
    from consentvid import appview as av
    from consentvid.identity import public_client, resolve_did

    did = resolve_did(repo)
    client = public_client(did)
    # Fence: anything the live indexer writes for this repo from now on is
    # newer than our listing, and must win over it (R3-07).
    # Timestamps have millisecond precision: start the fence on a fresh
    # millisecond, so writes made before this call are strictly older.
    before = now_iso()
    while (started := now_iso()) == before:
        time.sleep(0.0002)
    # Read everything BEFORE writing: the listing is unbounded-latency network
    # I/O against an untrusted PDS and must not run while we hold the
    # database's write lock (the live indexer would time out behind us).
    listings = {collection: _list_all(client, did, collection) for collection in (VIDEO_NSID, CONSENT_NSID)}
    stored = invalid = skipped = 0
    # Take the write lock, then see what changed while we were listing.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    touched = {
        CONSENT_NSID: {
            row["uri"]
            for row in conn.execute(
                "SELECT uri FROM consents WHERE did = ? AND "
                "max(indexed_at, COALESCE(revoked_at, ''), COALESCE(invalid_at, '')) >= ?",
                (did, started),
            )
        },
        VIDEO_NSID: {
            row["uri"]
            for row in conn.execute(
                "SELECT uri FROM videos WHERE did = ? AND indexed_at >= ? "
                "UNION SELECT uri FROM video_tombstones WHERE did = ? AND deleted_at >= ?",
                (did, started, did, started),
            )
        },
    }
    for collection in (VIDEO_NSID, CONSENT_NSID):
        live = set()
        for r in listings[collection]:
            # listRecords comes from the contributor's PDS (untrusted): only
            # accept URIs in this repo and collection, and rebuild the URI
            # ourselves, so one repo can never overwrite another's rows.
            try:
                at = parse_at_uri(r.uri)
            except (ValueError, TypeError):
                invalid += 1
                click.echo(f"  skipped malformed URI {_safe(repr(r.uri))}")
                continue
            if at.did != did or at.collection != collection:
                invalid += 1
                _warn(f"skipped foreign URI {_safe(r.uri)} listed by {did}'s PDS")
                continue
            uri = f"at://{did}/{collection}/{at.rkey}"
            live.add(uri)
            if uri in touched[collection]:
                skipped += 1
                continue
            try:
                # On invalid records ingest itself makes sure older terms stop
                # being usable (an invalid row, a revoke, or a removed video).
                errors = av.ingest(conn, collection, uri, did, at.rkey, str(r.cid), to_dict(r.value))
            except Exception as e:  # noqa: BLE001 — poison record: skip it, but never keep old terms
                errors = [f"could not ingest: {type(e).__name__}"]
                av.invalidate(conn, collection, uri)
            if errors:
                invalid += 1
                click.echo(f"  skipped {_safe(uri)}: {_safe(errors[0])}")
            else:
                stored += 1
        # Anything indexed earlier that's gone now was deleted while we
        # weren't watching.
        table = "videos" if collection == VIDEO_NSID else "consents"
        gone = [
            row["uri"]
            for row in conn.execute(f"SELECT uri FROM {table} WHERE did = ?", (did,)).fetchall()
            if row["uri"] not in live and row["uri"] not in touched[collection]
        ]
        for uri in gone:
            if collection == VIDEO_NSID:
                av.delete_video(conn, uri)
            else:
                av.revoke_consent(conn, uri, "consent deleted")
    acct = conn.execute("SELECT updated_at FROM accounts WHERE did = ?", (did,)).fetchone()
    if acct is None or acct["updated_at"] < started:
        av.set_account_status(conn, did, True, None)
    conn.commit()
    if skipped:
        click.echo(f"  {skipped} record(s) skipped (changed during backfill; the live index is newer)")
    return stored, invalid


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
            av.index_loop(
                conn, on_event=lambda op, x: click.echo(f"  [{_safe(op)}] {_safe(x)}"), stop_after=stop_after
            )
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
        click.echo(f"{_safe(r['revoked_at'])}  {_safe(r['uri'])}  ({_safe(r['revoke_reason'] or 'deleted')})")
    click.echo(f"{len(rows)} revocation(s)" + (f" since {_safe(since)}" if since else "") + ".")


@appview.command("query")
@_filter_options
@click.option(
    "--capture-provenance",
    "provenance",
    multiple=True,
    type=click.Choice(CAPTURE_PROVENANCE),
    callback=_refuse_attested,
    help="'attested' is refused: it is self-declared and nothing verifies it yet.",
)
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
    def terms(col):
        return _safe(", ".join(map(str, json.loads(r[col]))))

    for r in rows:
        state = f"  REVOKED ({_safe(r['revoke_reason'])})" if r["revoked_at"] else ""
        click.secho(f"{_safe(repr(r['title']))}{state}", bold=True)
        click.echo(
            f"  consent:    {_safe(r['uri'])}\n  purposes:   {terms('purposes')}"
            f"\n  models:     {terms('model_classes')}"
            f"\n  modalities: {terms('modalities')}"
            f"\n  provenance: {_safe(r['capture_provenance'])}"
        )
    click.echo(f"{len(rows)} result(s).")


@appview.command("export")
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@_filter_options
@click.option(
    "--capture-provenance",
    "provenance",
    multiple=True,
    type=click.Choice(CAPTURE_PROVENANCE),
    callback=_refuse_attested,
    help="'attested' is refused: it is self-declared and nothing verifies it yet.",
)
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
        try:
            consent, video = json.loads(r["record"]), json.loads(r["video_record"])
            # Re-validate: the index may predate today's rules.
            if validate_in_repo(CONSENT_NSID, consent, r["did"]) or validate_in_repo(
                VIDEO_NSID, video, r["did"]
            ):
                raise ValueError("no longer valid")
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the export
            _warn(f"skipping indexed consent {_safe(r['uri'])}: {type(e).__name__}: {_safe(e)}")
            continue
        clips.append(
            Clip(
                r["uri"],
                r["cid"],
                consent,
                r["video_uri"],
                r["current_video_cid"],
                video,
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
