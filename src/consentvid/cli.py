"""ConsentVid command-line interface.

Contributor commands write to your own ATproto repo and need `consentvid
login`. Everything that only reads (verify, resync, export --repo, embed/read
C2PA, appview) talks directly to the relevant PDS and needs no account.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
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

from consentvid import __version__, versions
from consentvid.errors import ConfigError, ConsentVidError
from consentvid.paths import secure_dir, write_private
from consentvid.records import (
    CONSENT_FOR_VIDEO,
    CONSENT_NSIDS,
    CONSENT_V2_NSID,
    RECORD_NSIDS,
    VIDEO_FOR_CONSENT,
    VIDEO_NSID,
    VIDEO_NSIDS,
    VIDEO_V2_NSID,
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
    SENSITIVE_SELF_LABELS,
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


_DEFAULT_SERVICE = "https://bsky.social"


def _service_url() -> str:
    from consentvid.storage import require_https

    return require_https("CONSENTVID_SERVICE", os.environ.get("CONSENTVID_SERVICE") or _DEFAULT_SERVICE)


# The SDK's httpx client would use httpx's 5 s default: shorter than a PDS
# may take to commit a write, so a write that succeeded would be reported as
# failed (R3-06). Reads of untrusted hosts use identity's capped client.
_WRITE_TIMEOUT_S = 60.0
_CONNECT_TIMEOUT_S = 10.0


def _new_client(base_url: str | None = None) -> Client:
    import httpx
    from atproto_client.request import Request

    client = Client(
        base_url or _service_url(),
        request=Request(timeout=httpx.Timeout(_WRITE_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)),
    )

    # Persist every new or refreshed session; otherwise the cached refresh
    # token eventually expires and every command fails with ExpiredToken.
    def _save(_event, _session):
        _secure_config_dir()
        write_private(SESSION_PATH, client.export_session_string())

    client.on_session_change(_save)
    return client


def _throwaway_client() -> Client:
    """A client with no session-saving hook (for ending a cached session)."""
    import httpx
    from atproto_client.request import Request

    return Client(_service_url(), request=Request(timeout=httpx.Timeout(_WRITE_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)))


def _end_cached_session(*, strict: bool = False) -> bool:
    """End the cached session on the server that issued it: deleteSession
    with its REFRESH token, which revokes it (otherwise a replaced session
    stays usable until it expires, and `logout` can no longer reach it).
    Importing the session points the client at the session's own PDS.
    Best effort: errors (the old PDS may be unreachable, or the account
    deactivated there) are ignored, or raised with ``strict``. Returns True
    if the server confirmed it. The file itself is left to the caller."""
    if not SESSION_PATH.exists():
        return False
    try:
        client = _throwaway_client()
        client._import_session_string(SESSION_PATH.read_text().strip())  # no network
        client.com.atproto.server.delete_session(
            headers=client._get_refresh_auth_headers(), ignore_session_check=True
        )
        return True
    except Exception:
        if strict:
            raise
        return False


def _set_me(client: Client, did: str, handle: str | None) -> None:
    """Who we are, from the session itself (no AppView profile lookup:
    Client.login's getProfile fails for accounts the Bluesky AppView
    doesn't index). 'handle.invalid' is shown as the DID."""
    import types

    shown = handle if handle and handle != "handle.invalid" else did
    client.me = types.SimpleNamespace(did=did, handle=shown)


def _session_client(identifier: str, password: str) -> Client:
    """Create a session on the account's OWN PDS: handle -> DID -> DID
    document -> PDS, the way goat and the reference clients log in. The app
    password is only ever sent to that PDS (or to CONSENTVID_SERVICE, an
    explicit override)."""
    from consentvid.identity import resolve_did, resolve_identity

    identifier = identifier.strip().removeprefix("@")
    if os.environ.get("CONSENTVID_SERVICE"):
        client = _new_client()
        click.echo(f"Signing in to {_service_url()} (CONSENTVID_SERVICE) ...")
        session = _create_session(client, identifier, password)
    elif "@" in identifier:
        # An email address can't be resolved to a PDS.
        _warn(f"signing in with an email address: using {_DEFAULT_SERVICE} (set CONSENTVID_SERVICE for another PDS)")
        client = _new_client(_DEFAULT_SERVICE)
        session = _create_session(client, identifier, password)
    else:
        did = resolve_did(identifier)  # handle -> DID: the forward direction, proven
        ident = resolve_identity(did)
        # The back-link: the DID document must claim this handle. Together
        # that is the handle spec's bidirectional check; a second lookup of
        # the same handle (verified_handle) would add only a failure mode.
        if not identifier.startswith("did:") and getattr(ident, "claimed_handle", None) != identifier.lower():
            raise ConfigError(
                "handle doesn't verify against its DID document; log in with your DID or set CONSENTVID_SERVICE"
            )
        client = _new_client(ident.pds)
        click.echo(f"Signing in to {ident.pds} ...")
        session = _create_session(client, did, password)
    _set_me(client, session.did, session.handle)
    _check_session_scope(client, warn=True)
    moved_to = _check_session_state(client, session)
    if moved_to:
        _end_cached_session()  # the session just created on the wrong PDS
        SESSION_PATH.unlink(missing_ok=True)
        raise ConfigError(
            f"you signed in to {_session_pds(client)}, but your DID document names {_safe(moved_to)} as your PDS; "
            "log in with your handle or DID (without CONSENTVID_SERVICE) so ConsentVid finds the right one"
        )
    return client


def _create_session(client: Client, identifier: str, password: str):
    """createSession, with the 2FA refusal explained."""
    try:
        return client._get_and_set_session(identifier, password)
    except AtProtocolError as e:
        if _xrpc_error_name(e) == "AuthFactorTokenRequired":
            raise ConfigError(
                "your account requires an email code for password logins; use an app password instead "
                "(https://bsky.app/settings/app-passwords)"
            ) from e
        raise


def _session_pds(client) -> str | None:
    """The PDS this session talks to (the SDK moves the client to the PDS
    named in createSession's didDoc)."""
    pds = getattr(getattr(client, "_session", None), "pds_endpoint", None)
    if isinstance(pds, str) and pds:
        return pds
    base = getattr(client, "_base_url", None)
    return base if isinstance(base, str) else None


def _host(url) -> str | None:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".") or None
    except (ValueError, TypeError):
        return None


def _check_session_state(client, info) -> str | None:
    """Refuse a session whose account is not active (getSession /
    createSession report active=false: deactivated, e.g. left behind after a
    PDS migration, taken down or suspended), and compare the session's PDS
    with the one the DID document names, as the OAuth spec requires of a
    token's PDS. Returns the DID document's PDS when they differ, else None.
    A resolution failure is reported, never taken for a mismatch."""
    from consentvid.identity import resolve_identity

    pds = _session_pds(client) or "your PDS"
    if getattr(info, "active", None) is False:
        status = getattr(info, "status", None)
        _end_cached_session()
        SESSION_PATH.unlink(missing_ok=True)
        raise ConfigError(
            f"your account on {_safe(pds)} is {_safe(status or 'inactive')}; "
            "if you migrated PDS, run `consentvid login` again"
        )
    did = getattr(info, "did", None)
    try:
        doc_pds = resolve_identity(did).pds
    except ConsentVidError:
        raise
    except Exception as e:
        raise ConsentVidError(
            f"could not resolve {did} to check which PDS hosts your account: {type(e).__name__}: {_safe(e)}"
        ) from e
    here = _session_pds(client)
    if here is not None and _host(doc_pds) != _host(here):
        return doc_pds
    return None


_MAIN_PASSWORD_SCOPE = "com.atproto.access"
_PRIVILEGED_SCOPE = "com.atproto.appPassPrivileged"
_TAKENDOWN_SCOPE = "com.atproto.takendown"


def _session_scope(client) -> str | None:
    try:
        scope = client._session.access_jwt_payload.scope
    except Exception:  # noqa: BLE001 — no session, or a token we can't decode
        return None
    return scope if isinstance(scope, str) else None


def _check_session_scope(client, *, warn: bool) -> None:
    """The reference PDS marks what a session's credential was in the access
    token's scope. A taken-down account is refused; with ``warn``, a session
    from the MAIN password (or a privileged app password) is flagged. Other
    PDS implementations may use other scopes: those pass silently."""
    scope = _session_scope(client)
    if scope == _TAKENDOWN_SCOPE:
        try:
            client.com.atproto.server.delete_session(
                headers=client._get_refresh_auth_headers(), ignore_session_check=True
            )
        except Exception:  # noqa: BLE001 — best effort
            pass
        SESSION_PATH.unlink(missing_ok=True)
        raise ConfigError("this account is taken down")
    if not warn:
        return
    if scope == _MAIN_PASSWORD_SCOPE:
        _warn(
            "that looks like your MAIN account password: ConsentVid has stored a full-access session. "
            "Create an app password at https://bsky.app/settings/app-passwords, then run "
            "`consentvid logout` and `consentvid login` with it."
        )
    elif scope == _PRIVILEGED_SCOPE:
        _warn("this is a privileged app password; ConsentVid only needs a normal (non-privileged) one")


_AUTH_ERRORS = frozenset({"ExpiredToken", "InvalidToken", "AuthenticationRequired", "AuthMissing"})


def _definite_auth_failure(e: BaseException) -> bool:
    """The cached session is dead (as opposed to the network or the PDS
    having trouble, which must not burn a rate-limited createSession)."""
    from atproto_client.exceptions import LoginRequiredError

    if isinstance(e, LoginRequiredError):
        return True
    status = getattr(getattr(e, "response", None), "status_code", None)
    content = getattr(getattr(e, "response", None), "content", None)
    return status in (400, 401) and getattr(content, "error", None) in _AUTH_ERRORS


def get_client() -> Client:
    """An authenticated client: cached session, else credentials from env.
    Only a definitely dead session falls back to the credentials; network
    and server errors are reported as such."""
    client = None
    if SESSION_PATH.exists():
        client = _new_client()
        try:
            client._import_session_string(SESSION_PATH.read_text().strip())
        except Exception:  # noqa: BLE001 — unreadable session file: as good as none
            client = None
    if client is not None:
        try:
            info = client.com.atproto.server.get_session()  # refreshes an expired access token
        except Exception as e:
            if not _definite_auth_failure(e):
                if isinstance(e, AtProtocolError):
                    raise ConsentVidError(f"could not check the cached session: {_xrpc_message(e)}") from e
                raise ConsentVidError(f"could not check the cached session: {type(e).__name__}: {_safe(e)}") from e
        else:
            _set_me(client, info.did, info.handle)
            _check_session_scope(client, warn=False)
            # Before anything is uploaded or written: a session on a PDS the
            # account has left (or a deactivated account) must not be used.
            moved_to = _check_session_state(client, info)
            if not moved_to:
                return client
            _end_cached_session()  # the old PDS's session: don't leave it live
            SESSION_PATH.unlink(missing_ok=True)
            _warn(f"your account moved to {_safe(moved_to)}; signing in again")
    handle = os.environ.get("CONSENTVID_HANDLE")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD")
    if not handle or not pw:
        raise ConfigError(
            "not logged in. Run `consentvid login` (use an app password from "
            "https://bsky.app/settings/app-passwords, never your main password)."
        )
    return _session_client(handle, pw)


def _list_all(client: Client, did: str, collection: str) -> list:
    """Bounded listRecords pagination (see identity.list_all)."""
    from consentvid.identity import list_all

    return list_all(client, did, collection)


class _Rec:
    """A listed record with its collection and internal view (versions.py)."""

    __slots__ = ("uri", "cid", "nsid", "raw", "view")

    def __init__(self, uri, cid, nsid, raw):
        self.uri, self.cid, self.nsid, self.raw = uri, cid, nsid, raw
        try:
            self.view = versions.view(nsid, raw) if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001 — malformed: callers validate raw
            self.view = {}

    @property
    def rkey(self) -> str:
        return str(self.uri).rsplit("/", 1)[-1]


def _list_records(client: Client, did: str, nsids) -> list[_Rec]:
    """Every record in the given collections (both record versions)."""
    return [
        _Rec(r.uri, str(r.cid), nsid, to_dict(r.value)) for nsid in nsids for r in _list_all(client, did, nsid)
    ]


def _check_account(did: str, consent: dict) -> None:
    """Refuse to write a consent the account's own signals contradict: a
    bot account, or training while its AI-usage preference denies it
    (consentvid.signals). A failed lookup refuses too: fail closed."""
    from consentvid import signals

    try:
        account = signals.account_signals(did, fresh=True)
    except Exception as e:  # noqa: BLE001
        raise ConsentVidError(
            f"could not check your account's bot label and AI-usage preferences ({type(e).__name__}: {_safe(e)}); "
            "nothing was written, re-run"
        ) from e
    why = account.conflicts(consent)
    if why:
        hint = (
            " Remove the 'bot' self-label from your profile if this is a person's account."
            if account.bot
            else " Change that preference (or drop --purpose training) first."
        )
        raise ConsentVidError(f"not writing this consent: {why}.{hint}")


def _validate_flag(override: bool | None) -> bool | None:
    """Value for createRecord's `validate`. None = the PDS's default
    ("optimistic": validate lexicons it knows, accept others), which is the
    right setting for third-party lexicons; True forces strict validation,
    which fails on any reference-implementation PDS today: it validates only
    against its bundled lexicons and answers 'Unknown lexicon type'."""
    if override is not None:
        return override
    env = os.environ.get("CONSENTVID_VALIDATE", "").lower()
    return True if env in ("1", "true", "yes") else None


def _check_before_write(nsid: str, record: dict, did: str, rkey: str | None = None) -> None:
    """Validate before writing. ``rkey``: the key being written, or None when
    the PDS assigns it."""
    errors = validate_in_repo(nsid, record, did, rkey=rkey) + write_policy_errors(nsid, record)
    if errors:
        raise ConsentVidError("record failed validation:\n  - " + "\n  - ".join(_with_char_counts(errors, record)))


def _with_char_counts(errors: list[str], record: dict) -> list[str]:
    """Lexicon string limits are UTF-8 bytes; say how many characters that
    is (a CJK character is 3 bytes)."""
    out = []
    for e in errors:
        m = re.match(r"(\w+): (\d+) bytes exceeds maxLength (\d+)$", e)
        value = record.get(m.group(1)) if m else None
        if isinstance(value, str):
            e = f"{e} ({len(value)} characters; the limit is in UTF-8 bytes)"
        out.append(e)
    return out


def _xrpc_error_name(e: BaseException) -> str:
    content = getattr(getattr(e, "response", None), "content", None)
    return getattr(content, "error", None) or ""


def _get_record_or_none(client: Client, did: str, collection: str, rkey: str):
    """getRecord, or None if the record doesn't exist."""
    try:
        return client.com.atproto.repo.get_record({"repo": did, "collection": collection, "rkey": rkey})
    except Exception as e:
        if _xrpc_error_name(e) == "RecordNotFound" or "RecordNotFound" in str(e):
            return None
        raise


def _rate_limited(e: BaseException) -> bool:
    return getattr(getattr(e, "response", None), "status_code", None) == 429


def _rate_limit_error(e: BaseException) -> ConsentVidError:
    from consentvid.identity import rate_limit_reset

    reset = rate_limit_reset(e)
    until = (
        f"until {datetime.fromtimestamp(reset).astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        if reset
        else "for now"
    )
    return ConsentVidError(
        f"rate limited by your PDS {until}; re-run the same command (the uploaded bytes are kept)"
    )


def _note_validation(*results) -> None:
    """Show the PDS's validationStatus ('valid' / 'unknown') when it gives one."""
    for r in results:
        status = getattr(r, "validation_status", None)
        if isinstance(status, str):
            click.echo(f"  PDS validation: {_safe(status)}")


def _confirm_strict_validation(validate) -> None:
    """--validate would be refused by the PDS only after the bytes are
    uploaded: say so first, and go on only if the user insists."""
    if _validate_flag(validate) is not True:
        return
    _warn(
        "--validate (or CONSENTVID_VALIDATE) will likely be rejected by your PDS after the upload "
        "('Unknown lexicon type': no PDS resolves third-party lexicons yet); the bytes would then be cleaned up"
    )
    if not _interactive() or not click.confirm("Continue with strict PDS validation?", default=False):
        raise ConsentVidError("not uploading: drop --validate / unset CONSENTVID_VALIDATE")


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


def _normalise_language(ctx, param, value):
    """Lowercase the primary subtag ('EN-gb' -> 'en-gb'): atproto readers
    reject an uppercase one."""
    if not value:
        return value
    primary, sep, rest = value.partition("-")
    return primary.lower() + sep + rest


def _video_options(f):
    for opt in reversed(
        [
            click.option(
                "--title", required=True, help="Short title: max 300 characters."
            ),
            click.option(
                "--description",
                default="",
                help="Optional longer description: max 3000 characters.",
            ),
            click.option(
                "--language",
                default="",
                callback=_normalise_language,
                help="BCP-47 tag of spoken language, e.g. en, cs, pt-BR.",
            ),
            click.option(
                "--tag",
                "tags",
                multiple=True,
                help="Topic tag, max 64 characters. Repeatable, at most 8.",
            ),
            click.option(
                "--self-label",
                "self_labels",
                multiple=True,
                type=click.Choice(SENSITIVE_SELF_LABELS),
                help="Label your own video (Bluesky's global self-labels). Dataset tools leave labeled "
                "clips out unless asked to include them. Repeatable.",
            ),
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
        help="Force (or disable) PDS-side lexicon validation. --validate fails on any "
        "reference-implementation PDS today (none resolve third-party lexicons yet); leave it unset. "
        "Records are always validated locally first.",
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
    for r in _list_records(client, did, VIDEO_NSIDS):
        if r.uri == exclude_uri:
            continue
        v = r.view
        if not v:
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


def _register(client: Client, video_path: Path, **opts):
    """Register a video record (one createRecord). Returns (uri, cid, record)."""
    uri, cid, record, _ = _register_impl(client, video_path, **opts)
    return uri, cid, record


def _register_impl(
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
    self_labels=(),
    consent_for=None,
):
    """Strip, hash, upload and write the video record.

    With ``consent_for(video_uri, video_cid) -> consent record``, the video
    and its consent are written in ONE atomic com.atproto.repo.applyWrites
    commit, both under the same client-generated TID (the consent is the
    video's sidecar), with the video's CID computed locally. Either both
    records exist or neither does. Records are written as
    org.consentvid.videoV2 / consentV2. Returns (uri, cid, record, consent)
    where consent is None or (consent_uri, consent_cid, consent_record);
    ``record`` and ``consent_record`` are internal views (versions.py), and
    ``consent_for`` returns one too."""
    from consentvid import storage
    from consentvid.records import compute_cid, new_tid

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

    _confirm_strict_validation(validate)
    _secure_config_dir()
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as tmp:
        path, probe, content_hash = _prepare_bytes(video_path, kind, keep_metadata, Path(tmp), bool(storage_value))

        if not allow_duplicate:
            for r in _list_records(client, did, VIDEO_NSIDS):
                if r.view.get("contentHash") == content_hash:
                    raise ConsentVidError(
                        f"these bytes are already registered as {r.uri} "
                        "(pass --allow-duplicate to register again)"
                    )

        record = {
            "title": title,
            "contentHash": content_hash,
            "storageRef": {"kind": kind, "value": storage_value or ""},
            "createdAt": now_iso(),
            "size": path.stat().st_size,
        }
        if mime := mime_for(path):
            record["mimeType"] = mime
        if description:
            record["description"] = description
        if language:
            record["language"] = language
        if tags:
            record["tags"] = list(dict.fromkeys(tags))
        if self_labels:
            record["labels"] = list(dict.fromkeys(self_labels))
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
        # A client-chosen key (createRecord and applyWrites both take one), so
        # a write whose response is lost can be looked up afterwards.
        rkey = new_tid()
        video_uri = f"at://{did}/{VIDEO_V2_NSID}/{rkey}"
        # Validate BEFORE uploading so a bad record never leaves orphaned bytes.
        stand_in = versions.video_record_v2(_validation_view(record))
        _check_before_write(VIDEO_V2_NSID, stand_in, did, rkey)
        if consent_for:
            first = consent_for(video_uri, compute_cid(stand_in))
            _check_before_write(CONSENT_V2_NSID, versions.consent_record_v2(first), did, rkey)
            _check_account(did, first)

        uploaded = False  # True only if THIS call created the stored object
        consent = None
        try:
            if uploading:
                click.echo(f"Uploading to {kind} ...")
                result = storage.upload(kind, path, content_hash=content_hash, title=title)
                record["storageRef"]["value"] = result.value
                uploaded = result.created
                click.echo(f"  -> {record['storageRef']['value']}")
                _check_before_write(VIDEO_V2_NSID, versions.video_record_v2(record), did, rkey)
            if consent_for:
                uri, cid, consent = _write_video_and_consent(client, did, rkey, record, consent_for, validate)
            else:
                resp = client.com.atproto.repo.create_record(
                    _write_params(did, VIDEO_V2_NSID, versions.video_record_v2(record), validate, rkey=rkey)
                )
                uri, cid = resp.uri, resp.cid
                _note_validation(resp)
        except Exception as e:
            if _rate_limited(e):
                # The PDS refused for now; a re-run reuses the stored bytes.
                raise _rate_limit_error(e) from e
            if not _definite_refusal(e):
                # The request may have been committed and only the answer
                # lost (a timeout, a dropped connection, a 5xx after the
                # commit): look the records up by their known key.
                outcome = _write_outcome(client, did, rkey, record, consent_for is not None)
                if outcome is not None:
                    click.secho("the PDS committed the write although the response was lost", fg="yellow")
                    uri, cid, consent = outcome
                    return uri, cid, record, consent
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
    return uri, cid, record, consent


def _definite_refusal(e: BaseException) -> bool:
    """The PDS answered the write with a 4xx: nothing was committed. Anything
    else (a timeout, a network error, a 5xx, no answer) may have been
    committed with only the response lost."""
    from consentvid.identity import status_of

    code = status_of(e)
    if isinstance(code, int) and 400 <= code < 500:
        return True
    return _xrpc_error_name(e) in ("InvalidRequest", "InvalidSwap") or "InvalidSwap" in str(e)


def _write_outcome(client: Client, did: str, rkey: str, video: dict, with_consent: bool):
    """After a write whose outcome is unknown: did it commit? Returns
    (video uri, video cid, consent or None) if the records are there as we
    wrote them, None if nothing was written. Raises ConsentVidError if
    that can't be told (the check failed, or something else is there)."""
    from consentvid.records import compute_cid

    unknown = ConsentVidError(
        "the outcome of the write is unknown; run `consentvid list` to check (the uploaded bytes are kept)"
    )
    try:
        got = _get_record_or_none(client, did, VIDEO_V2_NSID, rkey)
        consent = _get_record_or_none(client, did, CONSENT_V2_NSID, rkey) if with_consent else None
    except Exception as check:  # noqa: BLE001 — can't tell what happened
        raise unknown from check
    if got is None and consent is None:
        return None  # nothing was written: the usual failure path
    vcid = compute_cid(versions.video_record_v2(video))
    if got is None or str(got.cid) != vcid:
        raise unknown
    uri = f"at://{did}/{VIDEO_V2_NSID}/{rkey}"
    if not with_consent:
        return uri, vcid, None
    value = to_dict(consent.value) if consent is not None else None
    view = versions.consent_view(CONSENT_V2_NSID, value) if isinstance(value, dict) else None
    if view is None or view.get("videoCid") != vcid:
        raise unknown
    return uri, vcid, (f"at://{did}/{CONSENT_V2_NSID}/{rkey}", str(consent.cid), view)


def _write_video_and_consent(client: Client, did: str, rkey: str, video: dict, consent_for, validate):
    """One applyWrites commit creating the video and its sidecar consent
    (videoV2 + consentV2). ``video`` and the consent are internal views."""
    from consentvid.records import compute_cid

    video_uri = f"at://{did}/{VIDEO_V2_NSID}/{rkey}"
    video_rec = versions.video_record_v2(video)
    vcid = compute_cid(video_rec)
    consent = consent_for(video_uri, vcid)
    consent_rec = versions.consent_record_v2(consent)
    _check_before_write(CONSENT_V2_NSID, consent_rec, did, rkey)
    params = {
        "repo": did,
        "writes": [
            {"$type": "com.atproto.repo.applyWrites#create", "collection": VIDEO_V2_NSID, "rkey": rkey,
             "value": video_rec},
            {"$type": "com.atproto.repo.applyWrites#create", "collection": CONSENT_V2_NSID, "rkey": rkey,
             "value": consent_rec},
        ],
    }
    v = _validate_flag(validate)
    if v is not None:
        params["validate"] = v
    resp = client.com.atproto.repo.apply_writes(params)
    results = list(getattr(resp, "results", None) or [])
    _note_validation(*results)
    consent_uri = f"at://{did}/{CONSENT_V2_NSID}/{rkey}"
    consent_cid = (
        str(results[1].cid) if len(results) > 1 and getattr(results[1], "cid", None) else compute_cid(consent_rec)
    )
    if results and getattr(results[0], "cid", None) and str(results[0].cid) != vcid:
        # The PDS stored the video under a different CID than we computed
        # (it re-encoded the record?): re-pin the consent to the real one.
        actual = str(results[0].cid)
        _warn(f"the PDS assigned the video CID {_safe(actual)}, not {vcid}; re-pinning the consent to it")
        consent = {**consent, "videoCid": actual}
        consent_rec = versions.consent_record_v2(consent)
        _check_before_write(CONSENT_V2_NSID, consent_rec, did, rkey)
        fix = _write_params(did, CONSENT_V2_NSID, consent_rec, validate, rkey=rkey)
        fix["swapRecord"] = consent_cid
        consent_cid = str(client.com.atproto.repo.put_record(fix).cid)
        vcid = actual
    return video_uri, vcid, (consent_uri, consent_cid, consent)


def _consent_record(video_uri: str, video_cid: str, *, modalities, purposes, model_classes, expires_at,
                    compensation, people, rights_basis, capture_provenance, **_ignored) -> dict:
    """The internal view (versions.py) of a new consent; writers publish it
    with versions.consent_record_v2."""
    record = {
        "videoUri": video_uri,
        "videoCid": video_cid,
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
    return record


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
    """Write the video's consent: a sidecar record under the video's own
    record key, so the PDS itself keeps it to one per video."""
    _require_attestation(attest)
    if not (modalities and purposes and model_classes and people and adult):
        raise ConsentVidError("consent terms, --people and --attest-adult are required")
    did = client.me.did
    try:
        vat = parse_at_uri(video_uri)
        if vat.collection == VIDEO_NSID:
            raise ValueError(
                f"{video_uri} is a version-1 video record; new consents are written as "
                f"{CONSENT_V2_NSID}, which binds only {VIDEO_V2_NSID} records. Register the video again "
                "(consentvid contribute) and revoke the old record"
            )
        vat = parse_at_uri(video_uri, VIDEO_V2_NSID)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="VIDEO_URI") from None
    if vat.did != did:
        raise ConsentVidError(
            f"{video_uri} is not in your repo ({did}): you can only consent for videos you registered"
        )
    video = client.com.atproto.repo.get_record({"repo": did, "collection": VIDEO_V2_NSID, "rkey": vat.rkey})
    record = _consent_record(
        str(vat),
        str(video.cid),
        modalities=modalities,
        purposes=purposes,
        model_classes=model_classes,
        expires_at=expires_at,
        compensation=compensation,
        people=people,
        rights_basis=rights_basis,
        capture_provenance=capture_provenance,
    )
    consent_rec = versions.consent_record_v2(record)
    _check_before_write(CONSENT_V2_NSID, consent_rec, did, vat.rkey)
    _check_account(did, record)
    consent_uri = f"at://{did}/{CONSENT_V2_NSID}/{vat.rkey}"
    exists_msg = (
        f"this video already has consent {consent_uri}; pass --replace to change its terms "
        "(downstream datasets will see it as modified) or `consentvid revoke` it"
    )
    params = _write_params(did, CONSENT_V2_NSID, consent_rec, validate, rkey=vat.rkey)
    if replace:
        current = _get_record_or_none(client, did, CONSENT_V2_NSID, vat.rkey)
        if current is not None:
            params["swapRecord"] = str(current.cid)
            try:
                resp = client.com.atproto.repo.put_record(params)
            except Exception as e:
                if _xrpc_error_name(e) == "InvalidSwap" or "InvalidSwap" in str(e):
                    raise ConsentVidError("the consent changed concurrently; re-run") from e
                raise
            return resp.uri, resp.cid, record, True
    try:
        resp = client.com.atproto.repo.create_record(params)
    except Exception as e:
        # A create under an existing key fails on the PDS: say why.
        try:
            exists = _get_record_or_none(client, did, CONSENT_V2_NSID, vat.rkey) is not None
        except Exception:  # noqa: BLE001 — report the original error
            exists = False
        if exists:
            raise ConsentVidError(exists_msg) from e
        raise
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
    """Log in with an app password and cache the session (~/.consentvid).

    Your PDS is found from your handle (handle -> DID -> DID document), so
    the password only goes to the server that hosts your account.
    CONSENTVID_SERVICE overrides that."""
    handle = os.environ.get("CONSENTVID_HANDLE") or click.prompt("Handle or DID (e.g. you.bsky.social)")
    pw = os.environ.get("CONSENTVID_APP_PASSWORD") or click.prompt(
        "App password (bsky.app/settings/app-passwords)", hide_input=True
    )
    # A new login replaces the cached session: end the old one on its server
    # first, or it stays live with nothing left to end it (R3-13).
    if _end_cached_session():
        click.echo("Ended the previous session.")
    SESSION_PATH.unlink(missing_ok=True)
    client = _session_client(handle, pw)
    click.echo(f"Logged in as {_safe(client.me.handle)} ({client.me.did})")


@main.command()
def logout():
    """End the session on the server and delete the cached copy."""
    if not SESSION_PATH.exists():
        click.echo("No cached session.")
        return
    try:
        # deleteSession authenticates with the REFRESH token and invalidates it.
        _end_cached_session(strict=True)
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
    _check_session_scope(client, warn=True)
    click.echo(f"{_safe(client.me.handle)} ({client.me.did})")


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

    Both records are written in one atomic commit: either both exist or
    neither does.

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

    def consent_for(video_uri, video_cid):
        return _consent_record(video_uri, video_cid, **terms)

    uri, cid, record, consent = _register_impl(client, video_path, validate=validate, consent_for=consent_for, **opts)
    click.secho("Created video record:", fg="green")
    click.echo(f"  uri:  {uri}\n  hash: {record['contentHash']}")
    c_uri, c_cid, c_record = consent
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
    videos = [] if what == "consents" else _list_records(client, did, VIDEO_NSIDS)
    consents = [] if what == "videos" else _list_records(client, did, CONSENT_NSIDS)
    if as_json:
        dump = lambda rs: [{"uri": r.uri, "cid": r.cid, "value": r.raw} for r in rs]  # noqa: E731
        click.echo(json.dumps({"videos": dump(videos), "consents": dump(consents)}, indent=2))
        return
    by_video: dict[str, list] = {}
    for r in consents:
        by_video.setdefault(r.view.get("videoUri"), []).append(r)
    for r in videos:
        v = r.view
        ref = v.get("storageRef", {})
        click.secho(_safe(repr(v.get("title"))), bold=True)
        click.echo(f"  video:   {_safe(r.uri)}\n  storage: {_safe(ref.get('kind'))}  {_safe(ref.get('value'))}")
        linked = by_video.pop(r.uri, [])
        if not linked:
            click.secho("  consent: none", fg="yellow")
        if r.nsid == VIDEO_NSID:
            click.echo("  format:  version 1 (org.consentvid.video)")
        for c in linked:
            cv = c.view
            state = " (EXPIRED)" if is_expired(cv) else ""
            if c.uri.rsplit("/", 1)[-1] != r.uri.rsplit("/", 1)[-1]:
                state += " (ignored: not keyed to its video)"
            click.echo(f"  consent: {_safe(c.uri)}{state}\n           {_safe(describe_terms(cv))}")
    for vuri, cs in by_video.items():
        for c in cs:
            click.secho(f"consent {_safe(c.uri)} -> missing video {_safe(vuri)}", fg="yellow")
    if not videos and not consents:
        click.echo("Nothing registered yet. Try: consentvid contribute --help")


_APPLY_WRITES_MAX = 200  # reference PDS limit per applyWrites call


def _delete_in_one_commit(
    client: Client, did: str, targets: list[tuple[str, str]], swap_commit: str | None = None
) -> None:
    """Delete (collection, rkey) pairs with com.atproto.repo.applyWrites:
    one atomic commit (several only past the 200-operation limit, in the
    order given). ``swap_commit`` guards the first commit: the PDS refuses it
    (InvalidSwap) if the repo head moved since it was read."""
    for i in range(0, len(targets), _APPLY_WRITES_MAX):
        chunk = targets[i : i + _APPLY_WRITES_MAX]
        params = {
            "repo": did,
            "writes": [
                {"$type": "com.atproto.repo.applyWrites#delete", "collection": c, "rkey": k} for c, k in chunk
            ],
        }
        if swap_commit and i == 0:
            params["swapCommit"] = swap_commit
        client.com.atproto.repo.apply_writes(params)


def _invalid_swap(e: BaseException) -> bool:
    return _xrpc_error_name(e) == "InvalidSwap" or "InvalidSwap" in str(e)


_REVOKE_ATTEMPTS = 5
_revoke_sleep = time.sleep  # tests replace this


def _revoke_fingerprint(video: dict, linked: list) -> tuple:
    """What a confirmed video revoke deletes: the consent keys and the video
    version (its storage pointer and content hash)."""
    return (
        tuple(sorted(k for _, k in linked)),
        json.dumps(video.get("storageRef"), sort_keys=True),
        video.get("contentHash"),
    )


def _show_revoke_plan(video: dict, linked: list, delete_bytes: bool) -> None:
    ref = video.get("storageRef", {})
    click.echo(f"Video {_safe(repr(video.get('title')))} ({_safe(ref.get('kind'))}: {_safe(ref.get('value'))})")
    click.echo(f"  {len(linked)} consent receipt(s) will be deleted with it, in one commit.")
    if delete_bytes:
        click.echo("  The stored bytes will be deleted too.")


def _video_revoke_plan(client: Client, did: str, uri: str, rkey: str, video_nsid: str = VIDEO_NSID):
    """(repo head CID, video view, [(consent uri, rkey)]) for revoking a
    video of either version (its consents are of the same version). The
    head is read first, so any write after it makes the guarded applyWrites
    fail instead of leaving a consent behind."""
    consent_nsid = CONSENT_FOR_VIDEO[video_nsid]
    head = str(client.com.atproto.sync.get_latest_commit({"did": did}).cid)
    raw = to_dict(client.com.atproto.repo.get_record({"repo": did, "collection": video_nsid, "rkey": rkey}).value)
    video = versions.video_view(video_nsid, raw) if isinstance(raw, dict) else {}
    linked: dict[str, str] = {}
    # The sidecar (same record key), whatever its videoUri says: readers
    # ignore a mismatched one, but it would stay public as an orphan.
    if _get_record_or_none(client, did, consent_nsid, rkey) is not None:
        linked[rkey] = f"at://{did}/{consent_nsid}/{rkey}"
    # Legacy consents keyed otherwise.
    for r in _list_records(client, did, (consent_nsid,)):
        try:
            cat = parse_at_uri(r.uri, consent_nsid)
        except ValueError:
            continue
        if cat.did == did and r.view.get("videoUri") == uri:
            linked.setdefault(cat.rkey, r.uri)
    return head, video, [(u, k) for k, u in linked.items()]


@main.command()
@click.argument("uri")
@click.option(
    "--delete-bytes", is_flag=True, help="Also delete the video bytes from R2/PeerTube or unpin from IPFS."
)
@click.option("--all", "all_for_video", is_flag=True, hidden=True, help="Deprecated: no effect.")
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def revoke(uri, delete_bytes, all_for_video, yes):
    """Withdraw consent: delete a consent receipt, or a video together with all
    its consents (one atomic commit).

    Datasets learn about it when they run `consentvid resync`, and the AppView
    sees the deletion on the firehose immediately.
    """
    from consentvid import storage

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    if at.collection not in RECORD_NSIDS:
        raise ConsentVidError(f"not a ConsentVid record: {at.collection}")
    if at.collection in CONSENT_NSIDS and delete_bytes:
        raise click.UsageError("--delete-bytes applies when revoking a video URI")
    if all_for_video:
        _warn("--all is deprecated and has no effect: a video has at most one consent")
    client = get_client()
    did = client.me.did
    if at.did != did:
        raise ConsentVidError(f"not your record (it belongs to {at.did})")

    if at.collection in CONSENT_NSIDS:
        if not yes:
            click.confirm(f"Withdraw consent {uri}?", abort=True)
        client.com.atproto.repo.delete_record({"repo": did, "collection": at.collection, "rkey": at.rkey})
        click.secho(f"Withdrawn: {uri}", fg="green")
        return

    # Plan, confirm, commit. swapCommit guards the WHOLE repo, which is the
    # contributor's everyday Bluesky repo: a like while the prompt waits must
    # not make withdrawal fail. So the confirmation is for WHAT is deleted
    # (the consents and the video version), and the commit re-reads the head
    # right before it is sent, retrying a few times without asking again
    # unless what would be deleted changed (R3-05).
    _, video, linked = _video_revoke_plan(client, did, uri, at.rkey, at.collection)
    _show_revoke_plan(video, linked, delete_bytes)
    if not yes:
        click.confirm("Proceed?", abort=True)
    confirmed = _revoke_fingerprint(video, linked)
    for attempt in range(1, _REVOKE_ATTEMPTS + 1):
        head, video, linked = _video_revoke_plan(client, did, uri, at.rkey, at.collection)
        if _revoke_fingerprint(video, linked) != confirmed:
            _warn("what this revoke deletes changed since you confirmed it")
            _show_revoke_plan(video, linked, delete_bytes)
            if not yes:
                click.confirm("Proceed?", abort=True)
            confirmed = _revoke_fingerprint(video, linked)
        targets = [(CONSENT_FOR_VIDEO[at.collection], rkey) for _, rkey in linked] + [(at.collection, at.rkey)]
        try:
            _delete_in_one_commit(client, did, targets, swap_commit=head)
            break
        except Exception as e:
            if not _invalid_swap(e):
                raise
            if attempt == _REVOKE_ATTEMPTS:
                raise ConsentVidError("your repo kept changing during revoke; re-run") from e
            _revoke_sleep(random.uniform(0.2, 1.0))
    ref = video.get("storageRef", {})  # the version that was actually deleted
    for c_uri, _ in linked:
        click.echo(f"Withdrawn: {c_uri}")
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


def _both_spellings(values) -> tuple:
    """The internal values plus their V2 (kebab-case) spellings."""
    return tuple(dict.fromkeys([*values, *(versions.camel_to_kebab(v) for v in values)]))


def _camel_values(ctx, param, value):
    """Filters take either spelling of a value (V2 'open-weights' or
    'openWeights'); records are compared in the internal (camelCase) one."""
    return tuple(versions.kebab_to_camel(v) for v in value)


def _filter_options(f):
    for opt in reversed(
        [
            click.option(
                "--purpose",
                "purposes",
                multiple=True,
                type=click.Choice(PURPOSES),
                callback=_camel_values,
                help="Require this purpose. Repeatable (all required).",
            ),
            click.option(
                "--model-class",
                "model_classes",
                multiple=True,
                type=click.Choice(_both_spellings(MODEL_CLASSES)),
                callback=_camel_values,
                help="Require this model class (open-weights or openWeights). Repeatable (all required).",
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


def _include_labeled_option(f):
    return click.option(
        "--include-labeled",
        is_flag=True,
        help="Keep clips the contributor self-labeled porn, sexual, nudity or graphic-media "
        "(left out by default).",
    )(f)


@main.command()
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--repo", default=None, help="Contributor handle or DID (default: you). No login needed for others."
)
@_filter_options
@click.option("--tag", "tags", multiple=True, help="Only videos with any of these tags.")
@_include_labeled_option
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def export(output_path, repo, purposes, model_classes, modalities, tags, include_labeled, name, description):
    """Write a Croissant-RAI manifest of one contributor's consented clips
    (both record versions)."""
    from consentvid import signals
    from consentvid.croissant import Clip, build_manifest
    from consentvid.identity import display_name, public_client, resolve_did
    from consentvid.policy import covers

    did = resolve_did(repo) if repo else _own_did()
    client = public_client(did)
    try:
        account = signals.account_signals(did)
    except Exception as e:  # noqa: BLE001 — fail closed
        raise ConsentVidError(
            f"could not check the contributor's bot label and AI-usage preferences ({type(e).__name__}: {_safe(e)})"
        ) from e
    if account.bot:
        raise ConsentVidError(f"{_safe(did)} labels itself a bot account: bots cannot give consent")
    videos = {r.uri: r for r in _list_records(client, did, VIDEO_NSIDS)}
    # Each video's consent is its sidecar: the consent with the video's own
    # record key, of the same record version. Any other consent is ignored.
    best: dict[str, tuple[str, dict]] = {}
    cids: dict[str, str] = {}
    for r in _list_records(client, did, CONSENT_NSIDS):
        c = r.view
        if not isinstance(c.get("videoUri"), str):
            _warn(f"skipping malformed consent {_safe(r.uri)}")
            continue
        try:
            cat = parse_at_uri(r.uri, r.nsid)
            vat = parse_at_uri(c["videoUri"], VIDEO_FOR_CONSENT[r.nsid])
        except ValueError as e:
            _warn(f"skipping consent {_safe(r.uri)}: {_safe(e)}")
            continue
        if cat.did != did or vat.rkey != cat.rkey:
            _warn(f"ignored consent {_safe(r.uri)}: record key doesn't match its video")
            continue
        errors = validate_in_repo(r.nsid, r.raw, did, rkey=cat.rkey)
        if errors:
            _warn(f"leaving out {_safe(c['videoUri'])}: its consent {_safe(r.uri)} is invalid ({_safe(errors[0])})")
            continue
        if is_expired(c):
            _warn(f"skipping expired consent {_safe(r.uri)}")
            continue
        conflict = account.conflicts(c)
        if conflict:
            _warn(f"leaving out {_safe(c['videoUri'])}: {_safe(conflict)}")
            continue
        best[c["videoUri"]] = (r.uri, c)
        cids[r.uri] = r.cid

    handle = display_name(did)
    clips = []
    for vuri, (curi, c) in best.items():
        if vuri not in videos:
            _warn(f"consent {_safe(curi)} references missing video {_safe(vuri)}")
            continue
        vr = videos[vuri]
        vcid, v = vr.cid, vr.view
        if validate_in_repo(vr.nsid, vr.raw, did, rkey=vr.rkey):
            _warn(f"skipping {_safe(curi)}: its video record is invalid")
            continue
        if c["videoCid"] != vcid:  # validate_in_repo requires videoCid
            _warn(f"skipping {_safe(curi)}: video record changed after consent was given")
            continue
        if not covers(c, purposes=purposes, model_classes=model_classes, modalities=modalities):
            continue
        if tags and not set(tags) & set(v.get("tags") or []):
            continue
        sensitive = set(v.get("labels") or []) & set(SENSITIVE_SELF_LABELS)
        if sensitive and not include_labeled:
            _warn(f"left out {_safe(vuri)}: self-labeled {', '.join(sorted(sensitive))} (--include-labeled keeps it)")
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
@click.option(
    "--no-relay-status",
    "no_relay_status",
    is_flag=True,
    help="Don't ask the relay (getRepoStatus) whether the contributor's account is active, e.g. "
    "offline. By default a relay-level takedown makes a clip unavailable.",
)
def verify(uri, as_json, no_relay_status):
    """Cryptographically verify a record against its author's repo (no login).

    Checks the commit signature against the DID's key and the Merkle proof of
    the record's presence or absence, then validates the record.
    """
    from consentvid.resync import stale_against, witness_rev_of
    from consentvid.verify import fetch_verified

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="URI") from None
    # The relay's witness first (see resync: STALE), then the PDS.
    witnessed = None if no_relay_status else witness_rev_of(at.did)
    rec = fetch_verified(at)
    errors = validate_in_repo(at.collection, rec.value, at.did, rkey=at.rkey) if rec.present else []
    stale = stale_against(witnessed, rec)
    relay = relay_lag = None
    if rec.present and not no_relay_status:
        from consentvid.appview import is_operational
        from consentvid.resync import relay_status_of

        answer = relay_status_of(at.did)
        if is_operational(answer):
            relay_lag = answer[1]  # the relay's own processing state, not the account's (R3-01)
        elif answer is not None and not answer[0]:
            relay = answer[1] or "inactive"
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
                    "relayStatus": relay,
                    "relayProcessingStatus": relay_lag,
                    "relayWitnessedRev": witnessed,
                    "stale": stale,
                    "value": rec.value,
                },
                indent=2,
            )
        )
    elif not rec.present:
        how = "proven absent by a signed repo commit" if rec.proven else "reported absent by the PDS"
        click.secho(f"ABSENT — {how} (rev {_safe(rec.rev or '?')})", fg="red")
        if stale:
            click.secho(f"  STALE: the relay has seen rev {_safe(witnessed)}; the PDS may be rolled back", fg="red")
    else:
        click.secho("PRESENT — signature and Merkle proof verified", fg="green")
        click.echo(f"  cid: {_safe(rec.cid)}\n  rev: {_safe(rec.rev)}")
        if errors:
            click.secho("  INVALID: " + "; ".join(_safe(e) for e in errors), fg="red")
        if stale:
            click.secho(
                f"  STALE: the relay has seen rev {_safe(witnessed)}, newer than this commit: "
                "the PDS served a rolled-back or stale repo",
                fg="red",
            )
        if relay:
            click.secho(f"  UNAVAILABLE: the relay reports the account {_safe(relay)}", fg="red")
        elif relay_lag:
            click.secho(f"  note: relay reports {_safe(relay_lag)}; live indexes may be stale", fg="yellow")
        click.echo(json.dumps(rec.value, indent=2, default=str))
    if not rec.present or errors or relay or stale:
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
    "bot-account": ("BOT", "red"),
    "conflicting-preference": ("PREF", "red"),
    "labeled": ("LABELED", "red"),
    "stale": ("STALE", "red"),
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
@_include_labeled_option
@click.option(
    "--no-relay-status",
    "no_relay_status",
    is_flag=True,
    help="Don't ask the relay (getRepoStatus) whether the contributor's account is active, e.g. "
    "offline. By default a relay-level takedown makes a clip unavailable.",
)
def resync(input_path, write_path, min_provenance, strict, check, as_json, workers, include_labeled, no_relay_status):
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

    results = rs.check_all(
        items,
        min_provenance=min_provenance,
        workers=workers,
        relay_status=not no_relay_status,
        include_labeled=include_labeled,
    )
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
    from consentvid.identity import verified_handle
    from consentvid.resync import parse_consent_uri
    from consentvid.verify import fetch_verified

    try:
        consent_at = parse_consent_uri(consent_uri)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--consent") from None
    consent = fetch_verified(consent_at)
    if not consent.present:
        raise ConsentVidError(f"{consent_uri} does not exist (withdrawn?)")
    errors = validate_in_repo(consent_at.collection, consent.value, consent_at.did, rkey=consent_at.rkey)
    if errors:
        raise ConsentVidError(f"consent is invalid: {errors[0]}")
    c = versions.consent_view(consent_at.collection, consent.value)
    video_uri = c["videoUri"]
    video_nsid = VIDEO_FOR_CONSENT[consent_at.collection]
    video_at = parse_at_uri(video_uri, video_nsid)
    video = fetch_verified(video_at)
    if not video.present:
        raise ConsentVidError(f"the consent's video {video_uri} no longer exists")
    errors = validate_in_repo(video_nsid, video.value, video_at.did, rkey=video_at.rkey)
    if errors:
        raise ConsentVidError(f"the consent's video record is invalid: {errors[0]}")
    v = versions.video_view(video_nsid, video.value)
    if c["videoCid"] != video.cid:  # validate_in_repo requires videoCid
        raise ConsentVidError("the video record changed after consent was given; consent no longer applies")
    actual = hash_file(video_path)
    if actual != v.get("contentHash"):
        raise ConsentVidError(
            f"{video_path.name} is not the registered video\n"
            f"  file:   {actual}\n  record: {v.get('contentHash')}"
        )
    output_path = output_path or video_path.with_suffix(f".c2pa{video_path.suffix}")
    out = ce.embed_consent(
        video_path=video_path,
        out_path=output_path,
        consent_uri=consent_uri,
        consent_cid=consent.cid,
        consent=c,
        video_uri=video_uri,
        video_cid=video.cid,
        video=v,
        contributor_handle=_verified_handle(consent_at.did, lambda d: verified_handle(d, raise_errors=True)),
        contributor_did=consent_at.did,
    )
    click.secho(f"Embedded C2PA consent manifest -> {out}", fg="green")
    _warn(
        "signed with a self-signed prototype certificate; C2PA readers will mark the "
        "signer untrusted. The stamped file's hash differs from the registered one, so "
        "`consentvid resync` reports it UNBOUND: the receipt points at the consent but does "
        "not prove this file is the consented bytes. Share the registered bytes for training."
    )


def _verified_handle(did: str, verify_handle) -> str | None:
    """The DID's bidirectionally verified handle (``verify_handle`` is
    identity.verified_handle), or None. Never the DID or an unverified
    claim: a handle field must not carry something that isn't a handle."""
    from consentvid.identity import HandleLookupError

    try:
        handle = verify_handle(did)
    except HandleLookupError as e:
        _warn(f"handle not included: lookup failed ({_safe(e)})")
        return None
    except Exception:  # noqa: BLE001 — no verified handle
        return None
    return handle if isinstance(handle, str) else None


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
@click.option(
    "--bootstrap",
    is_flag=True,
    help="Allow publishing although no _lexicon DNS record names an authority yet (first publication).",
)
@click.option(
    "--allow-breaking",
    is_flag=True,
    help="Skip the lexicon-evolution check. Only for a brand-new NSID: a published lexicon may "
    "only gain optional fields and descriptions.",
)
@click.option(
    "--skip-pds-validation",
    is_flag=True,
    help="Send validate=false (escape hatch if your PDS rejects the com.atproto.lexicon.schema record).",
)
def publish_lexicons(nsids, bootstrap, allow_breaking, skip_pds_validation):
    """Publish the lexicons as com.atproto.lexicon.schema records in your repo.

    Only the lexicon authority may publish: the account named by the
    _lexicon.<authority> DNS TXT record. Each change is checked against the
    published version with the lexicon evolution rules first (also with
    --bootstrap, against whatever your repo already holds), and written with
    compare-and-swap: a schema that changed meanwhile is never overwritten."""
    from consentvid import lexicons as lx
    from consentvid.identity import public_client

    client = get_client()
    me = client.me.did
    targets = list(nsids) or lx.discover_lexicons()
    plans = []
    for nsid in targets:
        try:
            auth = lx.resolve_did_via_dns(nsid, strict=True)
        except lx.ConflictingLexiconRecords as e:
            raise ConsentVidError(str(e)) from None
        if auth is None and not bootstrap:
            raise ConsentVidError(
                f"no {lx.dns_record_name(nsid)} TXT record: pass --bootstrap to publish the first version, "
                f'then add  {lx.dns_record_name(nsid)}  TXT  "did={me}"'
            )
        if auth is not None and auth != me:
            raise ConsentVidError(
                f"you are not the lexicon authority for {lx.nsid_authority(nsid)} "
                f"(DNS names {_safe(auth)}, you are {me}); refusing to publish unofficial schemas"
            )
        local = lx.load_lexicon(nsid)
        # Whatever is published already (with --bootstrap: in your own repo)
        # is checked against the evolution rules, and its CID guards the write.
        published = _get_record_or_none(public_client(auth or me), auth or me, lx.SCHEMA_COLLECTION, nsid)
        if published is not None and not allow_breaking:
            old = {k: v for k, v in to_dict(published.value).items() if k != "$type"}
            problems = lx.compat_errors(old, local)
            if problems:
                raise ConsentVidError(
                    f"{nsid}: the change breaks the lexicon evolution rules (use a new NSID instead):\n  - "
                    + "\n  - ".join(problems)
                )
        plans.append((nsid, lx.build_schema_record(local), str(published.cid) if published is not None else None))
    for nsid, record, existing_cid in plans:
        params = {"repo": me, "collection": lx.SCHEMA_COLLECTION, "rkey": nsid, "record": record}
        if skip_pds_validation:
            params["validate"] = False
        try:
            if existing_cid is None:
                # createRecord fails if the key appeared meanwhile.
                client.com.atproto.repo.create_record(params)
            else:
                client.com.atproto.repo.put_record({**params, "swapRecord": existing_cid})
        except Exception as e:
            text = f"{_xrpc_error_name(e)} {e}"
            if "InvalidSwap" in text or "already exists" in text.lower():
                raise ConsentVidError(
                    f"{nsid}: the published schema changed while you were publishing; re-run"
                ) from e
            if not isinstance(e, AtProtocolError):
                raise
            hint = "" if skip_pds_validation else " (if your PDS rejects valid lexicons, retry with --skip-pds-validation)"
            raise ConsentVidError(f"{nsid}: {_xrpc_message(e)}{hint}") from e
        click.echo(f"Published {nsid} -> at://{me}/{lx.SCHEMA_COLLECTION}/{nsid}")
    by_authority = {lx.nsid_authority(n): n for n in targets}
    for auth, nsid in sorted(by_authority.items()):
        try:
            points_at_me = lx.resolve_did_via_dns(nsid) == me
        except ConsentVidError as e:
            _warn(f"could not check _lexicon.{auth}: {_safe(e)}")
            continue
        if points_at_me:
            click.echo(f"DNS _lexicon.{auth} already points at you.")
        else:
            click.echo(f'Add DNS TXT:  _lexicon.{auth}  "did={me}"')


def _verified_lexicon(did: str, nsid: str) -> dict:
    """The schema record for ``nsid`` in ``did``'s repo, verified like any
    record (commit signature + MST proof), with the $type and id the lexicon
    spec requires. Raises ConsentVidError with a one-line reason."""
    from consentvid import lexicons as lx
    from consentvid.errors import RepoUnavailable, VerificationError
    from consentvid.verify import fetch_verified

    uri = f"at://{did}/{lx.SCHEMA_COLLECTION}/{nsid}"
    try:
        rec = fetch_verified(uri)
    except (VerificationError, RepoUnavailable) as e:
        raise ConsentVidError(f"{uri} could not be verified: {_safe(e)}") from e
    if not rec.present:
        raise ConsentVidError(f"{uri} does not exist")
    value = rec.value or {}
    if value.get("$type") != lx.SCHEMA_COLLECTION:
        raise ConsentVidError(f"{uri} has $type {_safe(value.get('$type'))!r}, not {lx.SCHEMA_COLLECTION}")
    if value.get("id") != nsid:
        raise ConsentVidError(f"{uri} declares id {_safe(value.get('id'))!r}, not {nsid}")
    return value


@main.command("resolve-lexicon")
@click.argument("nsid")
@click.option("--did", "did_override", default=None, help="Skip DNS; read from this DID.")
def resolve_lexicon(nsid, did_override):
    """Resolve a lexicon like a validator would (DNS -> DID -> schema record),
    verify the record against the authority's repo, and compare it with the
    local copy. No login needed."""
    from consentvid import lexicons as lx
    from consentvid.errors import LexiconDnsError

    try:
        did = did_override or lx.resolve_did_via_dns(nsid, strict=True)
    except lx.ConflictingLexiconRecords as e:
        raise ConsentVidError(f"{e}: resolution fails until only one remains") from None
    except LexiconDnsError as e:
        raise ConsentVidError(f"DNS error: {e}") from None
    if not did:
        raise ConsentVidError(f"no TXT record at {lx.dns_record_name(nsid)}")
    click.echo(f"{lx.dns_record_name(nsid)} -> {_safe(did)}")
    value = _verified_lexicon(did, nsid)
    click.echo(f"Resolved at://{_safe(did)}/{lx.SCHEMA_COLLECTION}/{nsid}: verified (commit signature + MST proof)")
    try:
        same = lx.schema_matches(lx.load_lexicon(nsid), value)
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
    from consentvid.errors import LexiconDnsError

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
        line(True, "logged in", f"{_safe(client.me.handle)} ({client.me.did})")
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
            try:
                did = lx.resolve_did_via_dns(nsid, strict=True)
            except lx.ConflictingLexiconRecords:
                line(False, f"lexicon {nsid}", f"conflicting _lexicon TXT records at {lx.dns_record_name(nsid)}")
                continue
            except LexiconDnsError as e:
                line(False, f"lexicon {nsid}", f"DNS error: {_safe(e)}")
                continue
            if not did:
                line(None, f"lexicon {nsid}", "no _lexicon DNS record")
                continue
            pub = _verified_lexicon(did, nsid)
            same = lx.schema_matches(lx.load_lexicon(nsid), pub)
            line(
                same,
                f"lexicon {nsid}",
                "published, verified, up to date" if same else "published copy is outdated: run publish-lexicons",
            )
        except Exception as e:  # noqa: BLE001
            line(False, f"lexicon {nsid}", _safe(str(e)[:100]))
    if av.db_path().exists():
        s = av.stats(av.connect())
        line(True, "appview index", f"{s['consents']} consents, {s['videos']} videos, cursor {s['cursor']}")
        if s.get("gap_since"):
            line(
                False,
                "appview gap",
                f"events after {s['gap_since']} may be missing; repair: consentvid appview backfill --all",
            )
        if s.get("pending_backfill"):
            line(
                None,
                "appview pending",
                f"{s['pending_backfill']} repo(s) queued for a re-read ({s.get('pending_retrying', 0)} retrying); "
                "`appview index` re-reads them, `consentvid appview backfill --pending` forces it",
            )
        if not s["consents"] and not s["videos"]:
            line(None, "appview empty", "fill it with: consentvid appview backfill --all")
    else:
        line(None, "appview index", "not created yet")


# -------------------------------------------------------------- appview ------


@main.group()
def appview():
    """Index ConsentVid records from the whole network (local SQLite)."""


def _relay_url(value: str | None) -> str:
    from consentvid import appview as av
    from consentvid.storage import require_https

    return require_https("--relay", value or os.environ.get("CONSENTVID_RELAY") or av.DEFAULT_RELAY).rstrip("/")


@appview.command("backfill")
@click.argument("repos", nargs=-1)
@click.option(
    "--all",
    "all_repos",
    is_flag=True,
    help="Every contributor: repos the relay lists (com.atproto.sync.listReposByCollection) plus every "
    "repo already in the index. Repairs a gap.",
)
@click.option("--pending", is_flag=True, help="Repos queued for a re-read (#sync, reactivated accounts).")
@click.option(
    "--relay",
    default=None,
    help="Relay for discovery and repo status. Default: $CONSENTVID_RELAY, else relay1.us-east.bsky.network.",
)
def appview_backfill(repos, all_repos, pending, relay):
    """Index existing records from contributors (handles or DIDs, --all, or --pending)."""
    from consentvid import appview as av
    from consentvid.records import is_did

    if not (repos or all_repos or pending):
        raise click.UsageError("name contributors (handles or DIDs), or pass --all or --pending")
    relay = _relay_url(relay)
    conn = av.connect()
    # A gap recorded while this run lists repos covers events after what it
    # already read: only the gap seen now may be cleared at the end (R2-07).
    gap_at_start = av.gap_token(conn)
    targets: list[str] = list(repos)
    if all_repos:
        listed: list[str] = []
        for nsid in RECORD_NSIDS:
            listed += av.list_repos_by_collection(relay, nsid)
        known = av.known_dids(conn)
        click.echo(f"Relay lists {len(set(listed))} repo(s); the index knows {len(known)}.")
        targets += sorted(set(listed) | known)
    if pending or all_repos:
        targets += av.pending_dids(conn)
    seen, failures, done = set(), 0, 0
    for repo in targets:
        key = repo if is_did(repo) else repo.lower().removeprefix("@")
        if key in seen:
            continue
        seen.add(key)
        try:
            # _backfill_repo settles the repo's pending row itself (cleared,
            # or kept while the relay lags: R3-01).
            did, stored, invalid = _backfill_repo(conn, repo, relay=relay)
        except Exception as e:  # noqa: BLE001 — one bad repo must not stop the others
            conn.rollback()
            failures += 1
            _warn(f"{_safe(repo)}: backfill failed ({type(e).__name__}: {_safe(e)}); continuing")
            continue
        done += 1
        click.echo(f"{_safe(repo)}: {stored} record(s) indexed, {invalid} skipped")
    if all_repos and not failures:
        if av.clear_gap_if(conn, gap_at_start):
            if gap_at_start[0] is not None:
                click.echo("Gap repaired.")
        else:
            _warn("a new gap was recorded during this run; run `consentvid appview backfill --all` again")
    click.echo(f"{done} repo(s) backfilled, {failures} failed.")
    if failures and av.get_gap(conn):
        _warn("the gap stays recorded until every repo backfills; re-run `consentvid appview backfill --all`")


def _repo_statuses(did: str, relay: str) -> list[tuple[bool, str | None] | None]:
    """getRepoStatus from the contributor's PDS and from the relay; None for
    a source that couldn't answer. RepoNotFound counts as "deleted" only when
    the other source doesn't vouch for the repo: a relay only knows the repos
    it crawls, and the PDS only those it hosts."""
    from consentvid import appview as av
    from consentvid.errors import RepoUnavailable
    from consentvid.identity import public_client, with_retry

    pds: tuple[bool, str | None] | str | None
    try:
        st = with_retry(lambda: public_client(did).com.atproto.sync.get_repo_status({"did": did}))
        pds = (bool(st.active), getattr(st, "status", None))
    except RepoUnavailable as e:
        # A tombstoned DID (plc.directory, a trust root) is deleted, whatever
        # a relay still remembers. Other identity failures say nothing.
        pds = (False, "deleted") if e.status == "deleted" else None
    except Exception as e:  # noqa: BLE001
        pds = "not-found" if _xrpc_error_name(e) == "RepoNotFound" else None
    rel: tuple[bool, str | None] | str | None
    try:
        rel = av.relay_repo_status(relay, did)
    except RepoUnavailable:
        rel = "not-found"
    except Exception:  # noqa: BLE001
        rel = None

    def resolve(mine, other):
        if mine != "not-found":
            return mine
        # A relay that is throttling a repo still knows it exists (R3-01).
        vouched = isinstance(other, tuple) and (other[0] or av.is_operational(other))
        return None if vouched or other is None else (False, "deleted")

    return [resolve(pds, rel), resolve(rel, pds)]


_STATUS_SEVERITY = ("deleted", "takendown", "suspended", "deactivated")


def _combine_status(answers) -> tuple[bool, str | None] | None:
    """The most restrictive answer: inactive if any source says so; active
    only if every source answered active; None if that can't be decided.
    A relay processing status (throttled, host-throttled, desynchronized) is
    not an account status and casts no vote (R3-01; see _relay_lag)."""
    from consentvid.appview import is_operational

    answers = [a for a in answers if not is_operational(a)]
    inactive = [a for a in answers if a is not None and not a[0]]
    if inactive:
        statuses = [a[1] for a in inactive]
        for s in _STATUS_SEVERITY:
            if s in statuses:
                return False, s
        return False, statuses[0]
    if answers and all(a is not None and a[0] for a in answers):
        return True, None
    return None


def _backfill_repo(conn, repo: str, relay: str | None = None) -> tuple[str, int, int]:
    """Re-read one repo into the index. Returns (did, stored, invalid).

    The repo status (PDS + relay getRepoStatus, or a tombstoned DID) is
    decided first: a deactivated, taken-down, suspended or deleted repo is
    not listed at all (its PDS refuses listRecords, and the sync spec says to
    treat it as unavailable). Its status is recorded and the backfill counts
    as done, so `--all` can clear a gap that hid its #account event (R2-01)."""
    from consentvid import appview as av
    from consentvid.identity import public_client, resolve_did

    did = resolve_did(repo)
    # Fence: anything the live indexer writes for this repo from now on is
    # newer than our listing, and must win over it (R3-07).
    # Timestamps have millisecond precision: start the fence on a fresh
    # millisecond, so writes made before this call are strictly older.
    before = now_iso()
    while (started := now_iso()) == before:
        time.sleep(0.0002)
    # Read everything BEFORE writing: the listing and the status checks are
    # network I/O against untrusted hosts and must not run while we hold the
    # database's write lock (the live indexer would time out behind us).
    answers = _repo_statuses(did, _relay_url(relay))
    status = _combine_status(answers)
    if status is not None and not status[0]:
        return _record_inactive_repo(conn, did, status[1], started)
    relay_lag = _relay_lag(answers)
    client = public_client(did)
    listings = {collection: _list_all(client, did, collection) for collection in RECORD_NSIDS}
    # Account signals (bot self-label, AI-usage preferences), read with
    # proofs; a failure fails the backfill, which is retried (fail closed:
    # the account's consents stay hidden until it succeeds).
    from consentvid import signals

    account = signals.account_signals(did, fresh=True)
    stored = invalid = skipped = 0
    # Take the write lock, then see what changed while we were listing.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    touched_consents = {
        row["uri"]
        for row in conn.execute(
            "SELECT uri FROM consents WHERE did = ? AND "
            "max(indexed_at, COALESCE(revoked_at, ''), COALESCE(invalid_at, '')) >= ? "
            # A delete seen live for a consent the index never had (R1-07).
            "UNION SELECT uri FROM consent_tombstones WHERE did = ? AND deleted_at >= ?",
            (did, started, did, started),
        )
    }
    touched_videos = {
        row["uri"]
        for row in conn.execute(
            "SELECT uri FROM videos WHERE did = ? AND indexed_at >= ? "
            "UNION SELECT uri FROM video_tombstones WHERE did = ? AND deleted_at >= ?",
            (did, started, did, started),
        )
    }
    touched = {c: (touched_consents if c in CONSENT_NSIDS else touched_videos) for c in RECORD_NSIDS}
    for collection in RECORD_NSIDS:
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
        table = "consents" if collection in CONSENT_NSIDS else "videos"
        prefix = f"at://{did}/{collection}/"
        gone = [
            row["uri"]
            for row in conn.execute(f"SELECT uri FROM {table} WHERE did = ?", (did,)).fetchall()
            if row["uri"].startswith(prefix) and row["uri"] not in live and row["uri"] not in touched[collection]
        ]
        for uri in gone:
            if collection in VIDEO_NSIDS:
                av.delete_video(conn, uri)
            else:
                av.delete_consent(conn, uri)
    av.set_account_signals(conn, did, account.bot, account.training_denied, started)
    # Hosting status from the PDS and the relay (getRepoStatus), never a
    # blind "active": a takedown seen live must not be undone (R1-10).
    acct = conn.execute("SELECT updated_at FROM accounts WHERE did = ?", (did,)).fetchone()
    if acct is None or acct["updated_at"] < started:
        if status is None:
            _warn(f"{did}: could not confirm the repo status with both the PDS and the relay; account status unchanged")
        else:
            av.set_account_status(conn, did, *status)
    if relay_lag:
        # The PDS is the authority and was just read, but the relay is not
        # (fully) forwarding this repo's events, so the live index can't keep
        # it current: keep it hidden and re-check it later (R3-01).
        lag_until = format_dt(datetime.now(UTC) + timedelta(seconds=av.RELAY_LAG_RECHECK_S))
        av.mark_relay_lag(conn, did, relay_lag, next_try=lag_until)
    else:
        # The listing started after any #sync seen so far: the repo is current.
        av.clear_desync(conn, did, started)
        av.clear_pending(conn, did, started)
    conn.commit()
    if skipped:
        click.echo(f"  {skipped} record(s) skipped (changed during backfill; the live index is newer)")
    if relay_lag:
        click.echo(f"{did}: relay reports {_safe(relay_lag)}; listed from the PDS but hidden until the relay catches up")
    return did, stored, invalid


def _relay_lag(answers) -> str | None:
    """The relay processing status among ``answers`` (throttled,
    host-throttled, desynchronized), if any."""
    from consentvid.appview import is_operational

    return next((a[1] for a in answers if is_operational(a)), None)


def _record_inactive_repo(conn, did: str, status: str | None, started: str) -> tuple[str, int, int]:
    """Record that ``did`` is not active, without listing it. Rows of a
    deactivated/taken-down/suspended account are kept (hidden by active=0;
    reactivation queues a backfill); a deleted account is purged."""
    from consentvid import appview as av

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        acct = conn.execute("SELECT updated_at FROM accounts WHERE did = ?", (did,)).fetchone()
        if acct is None or acct["updated_at"] < started:  # a live #account event since then wins
            av.set_account_status(conn, did, False, status)
        av.clear_pending(conn, did, started)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    click.echo(f"{did}: account {status or 'inactive'}; not listed")
    return did, 0, 0


def _drain_repo(did: str) -> None:
    """Re-read one pending repo for the indexer's drain task (R3-02). Runs in
    a worker thread, so it opens its own database connection. _backfill_repo
    clears the pending row on success, or keeps it (due again in an hour)
    while the relay reports a processing status (R3-01)."""
    from consentvid import appview as av

    conn = av.connect()
    try:
        _backfill_repo(conn, did)
    finally:
        conn.close()


@appview.command("index")
@click.option("--stop-after", type=int, default=None, help="Exit after N relevant events.")
@click.option(
    "--no-drain",
    is_flag=True,
    help="Don't re-read pending repos (#sync, reactivations, relay lag) in the background; "
    "leave them to `appview backfill --pending`.",
)
def appview_index(stop_after, no_drain):
    """Follow the firehose (Jetstream) and keep the index current. Resumes from
    where it stopped. Repos queued for a re-read (a #sync, a reactivated
    account, a relay that reports throttled/desynchronized) are re-read in the
    background, one at a time, with backoff on failure."""
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
                conn,
                on_event=lambda op, x: click.echo(f"  [{_safe(op)}] {_safe(x)}"),
                stop_after=stop_after,
                drain=None if no_drain else _drain_repo,
            )
        )
        click.echo(f"Processed {n} events.")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@appview.command("stats")
def appview_stats():
    """Show index counts and freshness."""
    from consentvid import appview as av

    conn = av.connect()
    s = av.stats(conn)
    for k, v in s.items():
        click.echo(f"{k + ':':<19}{v}")
    if s["gap_since"]:
        _warn(f"events after {s['gap_since']} may be missing; repair: consentvid appview backfill --all")
    elif not s["consents"] and not s["videos"]:
        click.echo("The index is empty; fill it with: consentvid appview backfill --all")
    if s["pending_backfill"]:
        click.echo(
            f"{s['pending_backfill']} repo(s) queued for a re-read (`appview index` re-reads them; "
            "`consentvid appview backfill --pending` forces it):"
        )
        for row in av.pending_rows(conn):
            due = f", next try {row['next_try']}" if row["next_try"] else ""
            click.echo(f"  {row['did']}: {_safe(row['reason'])} ({row['attempts']} failed attempt(s){due})")


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
@_include_labeled_option
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def appview_query(
    purposes, model_classes, modalities, provenance, repo, include_revoked, include_expired, include_labeled,
    limit, as_json,
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
        include_labeled=include_labeled,
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
@_include_labeled_option
@click.option("--name", default=None, help="Dataset name.")
@click.option("--description", default=None)
def appview_export(output_path, purposes, model_classes, modalities, provenance, include_labeled, name, description):
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
        include_labeled=include_labeled,
        limit=None,
    )
    clips, missing = [], 0
    for r in rows:
        if not r["record"] or not r["video_record"]:
            missing += 1
            continue
        try:
            raw_consent, raw_video = json.loads(r["record"]), json.loads(r["video_record"])
            consent_nsid = parse_at_uri(r["uri"]).collection
            video_at = parse_at_uri(r["video_uri"], VIDEO_FOR_CONSENT[consent_nsid])
            # Re-validate: the index may predate today's rules.
            if validate_in_repo(consent_nsid, raw_consent, r["did"], rkey=r["rkey"]) or validate_in_repo(
                video_at.collection, raw_video, r["did"], rkey=video_at.rkey
            ):
                raise ValueError("no longer valid")
            consent = versions.consent_view(consent_nsid, raw_consent)
            video = versions.video_view(video_at.collection, raw_video)
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
