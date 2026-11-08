"""ConsentVid's contributor core: sessions, settings, and every write to a
contributor's repo (register, attach, contribute, revoke).

This is the single implementation behind both the public Python API
(``consentvid.api``) and the CLI (``consentvid.cli``): every gate lives here
(local validation, the bot-label and AI-preference checks, metadata
stripping, the atomic applyWrites with the consent as the video's sidecar).
It never prompts. Progress and warnings go to a reporter: the CLI installs
one that prints; otherwise they go to the ``consentvid`` logger.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from atproto import Client
from atproto_client.exceptions import AtProtocolError, NetworkError
from dotenv import dotenv_values

from consentvid import versions
from consentvid.errors import (
    AlreadyRegistered,
    Cancelled,
    ConfigError,
    ConsentVidError,
    RateLimited,
    UsageError,
)
from consentvid.paths import secure_dir, write_private
from consentvid.records import (
    CONSENT_FOR_VIDEO,
    CONSENT_NSIDS,
    CONSENT_V2_NSID,
    RECORD_NSIDS,
    VIDEO_MIME_TYPES,
    VIDEO_NSID,
    VIDEO_NSIDS,
    VIDEO_V2_NSID,
    format_dt,
    hash_file,
    mime_for,
    now_iso,
    parse_at_uri,
    parse_dt,
    safe_text,
    to_dict,
)
from consentvid.validate import validate_in_repo, write_policy_errors

# ------------------------------------------------------------ reporting ------

Reporter = Callable[[str, str], None]
"""``reporter(level, message)``; level is "info", "success" or "warning"."""

_log = logging.getLogger("consentvid")
_reporter: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar("consentvid_reporter", default=None)


def set_reporter(reporter: Reporter | None) -> contextvars.Token:
    """Route progress messages and warnings to ``reporter`` (None: the
    ``consentvid`` logger). Returns a token for ``reset_reporter``."""
    return _reporter.set(reporter)


def reset_reporter(token: contextvars.Token) -> None:
    _reporter.reset(token)


def _emit(level: str, message: str) -> None:
    reporter = _reporter.get()
    if reporter is not None:
        reporter(level, message)
    else:
        _log.log(logging.WARNING if level == "warning" else logging.INFO, message)


def _info(message: str) -> None:
    _emit("info", message)


def _success(message: str) -> None:
    _emit("success", message)


def _note_strict_validation(validate) -> None:
    """Strict PDS validation is refused by every reference PDS today, and
    only after the bytes are uploaded: say so (the CLI also asks first)."""
    if _validate_flag(validate) is True:
        _warn(
            "strict PDS validation (validate=True / CONSENTVID_VALIDATE) will likely be rejected by your PDS "
            "('Unknown lexicon type': no PDS resolves third-party lexicons yet); uploaded bytes would then be "
            "cleaned up"
        )


# ------------------------------------------------------------ plumbing -------


CONFIG_DIR = Path(os.environ.get("CONSENTVID_HOME", Path.home() / ".consentvid"))


SESSION_PATH = CONFIG_DIR / "session.txt"


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


def _safe_lines(msg: str) -> str:
    """Like _safe, but keeps the message's own line breaks."""
    return "\n".join(_safe(line) for line in str(msg).split("\n"))


def _warn(msg: str) -> None:
    # Callers wrap untrusted values in _safe(); this is the backstop.
    _emit("warning", _safe_lines(msg))


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
        _info(f"Signing in to {_service_url()} (CONSENTVID_SERVICE) ...")
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
        _info(f"Signing in to {ident.pds} ...")
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


def _rate_limit_error(e: BaseException) -> RateLimited:
    from consentvid.identity import rate_limit_reset

    reset = rate_limit_reset(e)
    until = (
        f"until {datetime.fromtimestamp(reset).astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        if reset
        else "for now"
    )
    return RateLimited(
        f"rate limited by your PDS {until}; re-run the same command (the uploaded bytes are kept)",
        reset=reset,
    )


def _note_validation(*results) -> None:
    """Show the PDS's validationStatus ('valid' / 'unknown') when it gives one."""
    for r in results:
        status = getattr(r, "validation_status", None)
        if isinstance(status, str):
            _info(f"  PDS validation: {_safe(status)}")


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
                raise UsageError(
                    "use an ISO datetime with timezone (2027-01-01T00:00:00Z), a date "
                    "(2027-01-01) or a duration (90d, 12w, 2y)",
                    param_hint="--expires-at",
                ) from None
    if dt <= now:
        raise UsageError("expiry must be in the future", param_hint="--expires-at")
    return format_dt(dt)


def _require_attestation(attest: bool) -> None:
    if not attest:
        raise UsageError(
            "consent requires --attest-authorship: you affirm (signed by your key) that "
            "you recorded this video yourself or otherwise hold the right to consent. "
            "Consenting on behalf of someone else's video (e.g. a YouTube re-upload) is "
            "unlawful. If you hold a licence rather than having filmed it, also pass "
            "--rights-basis licensed."
        )


def _default_storage() -> str:
    from consentvid import storage

    env = os.environ.get("CONSENTVID_STORAGE")
    if env:
        return env
    return "r2" if storage.configured("r2") else "local"


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
    _info("Stripping identifying metadata (lossless, no re-encode):")
    for k, v in found:
        _info(f"  - {_safe(k)} = {_safe(v)}")
    for extra in probe.extra_streams:
        _info(f"  - extra stream {_safe(extra)}")
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
    _info(f"  -> clean copy: {out}  (the record commits to this file's hash)")
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
        raise UsageError("--storage-kind url requires --storage-value https://...")
    if kind == "url" and not storage_value.startswith("https://"):
        raise UsageError("--storage-value for url must be an https:// URL")
    if storage_value is not None:
        from consentvid.validate import storage_ref_errors

        errs = storage_ref_errors({"kind": kind, "value": storage_value})
        if errs:
            raise UsageError(errs[0], param_hint="--storage-value")

    _note_strict_validation(validate)
    _secure_config_dir()
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as tmp:
        path, probe, content_hash = _prepare_bytes(video_path, kind, keep_metadata, Path(tmp), bool(storage_value))

        if not allow_duplicate:
            for r in _list_records(client, did, VIDEO_NSIDS):
                if r.view.get("contentHash") == content_hash:
                    raise AlreadyRegistered(
                        f"these bytes are already registered as {r.uri} "
                        "(pass --allow-duplicate to register again)",
                        uri=r.uri,
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
            _info(
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
                _info(f"Uploading to {kind} ...")
                result = storage.upload(kind, path, content_hash=content_hash, title=title)
                record["storageRef"]["value"] = result.value
                uploaded = result.created
                _info(f"  -> {record['storageRef']['value']}")
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
                    _warn("the PDS committed the write although the response was lost")
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


def _register_post(
    client: Client,
    post_ref: str,
    *,
    title,
    description,
    language,
    tags,
    keep_metadata,
    allow_duplicate,
    validate,
    consent_for,
    self_labels=(),
    **_ignored,
):
    """Register the video of one of the user's own Bluesky posts (videoV2
    #bskyPostRef) and its consent in one applyWrites commit. The bytes are
    downloaded from the PDS only to check them (hash, metadata, size); they
    stay where they are. Returns (uri, cid, record, consent) like
    _register_impl."""
    from consentvid import bsky, media
    from consentvid.records import compute_cid, new_tid
    from consentvid.validate import is_language
    from consentvid.verify import fetch_verified
    from consentvid.versions import content_hash_of_cid

    did = client.me.did
    try:
        post_at = bsky.resolve_post_uri(post_ref)
    except ValueError as e:
        raise UsageError(str(e), param_hint="--from-post") from None
    if post_at.did != did:
        raise ConsentVidError(
            f"that post belongs to {_safe(post_at.did)}, not to you ({_safe(did)}): "
            "you can only contribute your own videos"
        )
    post = fetch_verified(post_at)
    if not post.present:
        raise ConsentVidError(f"post {_safe(post_at)} doesn't exist (deleted?)")
    blob = bsky.video_blob_of(post.value)
    if blob is None:
        raise ConsentVidError("that post has no video (app.bsky.embed.video)")
    content_hash = content_hash_of_cid(blob.cid)
    if content_hash is None:
        raise ConsentVidError(f"the post's video blob {_safe(blob.cid)} is not a raw sha-256 CID")
    if not allow_duplicate:
        for r in _list_records(client, did, VIDEO_NSIDS):
            if r.view.get("contentHash") == content_hash:
                raise AlreadyRegistered(
                    f"this video is already registered as {r.uri} (pass --allow-duplicate to register again)",
                    uri=r.uri,
                )

    _note_strict_validation(validate)
    _secure_config_dir()
    with tempfile.TemporaryDirectory(dir=CONFIG_DIR) as tmp:
        _info("Downloading the post's video from your PDS to check it ...")
        path = bsky.download_blob(did, blob.cid, Path(tmp))
        size = path.stat().st_size
        probe = media.probe(path)
        if probe is None:
            if not keep_metadata:
                raise ConfigError(
                    "ffprobe is needed to check the post's video for identifying metadata "
                    "(brew install ffmpeg), or pass --keep-metadata"
                )
            _warn("ffprobe not found: can't read duration/size or check for metadata")
        elif probe.needs_sanitising:
            found = list({**probe.sensitive_tags, **probe.unknown_tags}.items())[:6]
            listed = ", ".join(f"{_safe(k)}={_safe(v)}" for k, v in found) or ", ".join(probe.extra_streams)
            if not keep_metadata:
                raise ConsentVidError(
                    f"the post's video carries identifying metadata ({listed}). It can't be stripped without "
                    "changing the bytes the post holds: delete the post and post a stripped copy "
                    "(consentvid register-video strips files), or pass --keep-metadata to include it anyway."
                )
            _warn(f"including the post's video WITH identifying metadata: {listed}")

    record = {
        "title": title or bsky.default_title(post.value),
        "contentHash": content_hash,
        "storageRef": {"kind": "bsky", "value": blob.cid, "post": {"uri": str(post_at), "cid": post.cid}},
        "createdAt": now_iso(),
        "size": size,
    }
    if blob.mime_type in VIDEO_MIME_TYPES:
        record["mimeType"] = blob.mime_type
    if description:
        record["description"] = description
    langs = [language] if language else [
        x for x in (post.value.get("langs") or []) if isinstance(x, str) and is_language(x)
    ][:3]
    if langs:
        record["langs"] = list(dict.fromkeys(langs))
    if tags:
        record["tags"] = list(dict.fromkeys(tags))
    if self_labels:
        record["labels"] = list(dict.fromkeys(self_labels))
    if probe:
        record.update(probe.record_fields())
    rkey = new_tid()
    video_uri = f"at://{did}/{VIDEO_V2_NSID}/{rkey}"
    rec_v2 = versions.video_record_v2(record)
    _check_before_write(VIDEO_V2_NSID, rec_v2, did, rkey)
    first = consent_for(video_uri, compute_cid(rec_v2))
    _check_before_write(CONSENT_V2_NSID, versions.consent_record_v2(first), did, rkey)
    _check_account(did, first)
    try:
        uri, cid, consent = _write_video_and_consent(client, did, rkey, record, consent_for, validate)
    except Exception as e:
        if _rate_limited(e):
            raise _rate_limit_error(e) from e
        if not _definite_refusal(e):
            outcome = _write_outcome(client, did, rkey, record, True)
            if outcome is not None:
                _warn("the PDS committed the write although the response was lost")
                return (*outcome[:2], record, outcome[2])
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
        raise UsageError(str(e), param_hint="VIDEO_URI") from None
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


class RevokePlan:
    """What a revoke is about to delete, handed to a ``confirm`` callback.
    ``changed``: what would be deleted changed since an earlier confirmation."""

    __slots__ = ("uri", "kind", "video", "consents", "delete_bytes", "changed")

    def __init__(self, uri, kind, video, consents, delete_bytes, changed=False):
        self.uri, self.kind, self.video = uri, kind, video
        self.consents, self.delete_bytes, self.changed = consents, delete_bytes, changed


class RevokeOutcome:
    """What a revoke did: the consent URIs withdrawn, the video URI deleted
    (None when only a consent was withdrawn), and the bytes' fate."""

    __slots__ = ("withdrawn", "video_deleted", "bytes_deleted", "bytes_note")

    def __init__(self, withdrawn, video_deleted=None, bytes_deleted=False, bytes_note=None):
        self.withdrawn, self.video_deleted = withdrawn, video_deleted
        self.bytes_deleted, self.bytes_note = bytes_deleted, bytes_note


def _revoke(
    client: Client,
    uri: str,
    *,
    delete_bytes: bool = False,
    confirm: Callable[[RevokePlan], bool] | None = None,
) -> RevokeOutcome:
    """Withdraw consent: delete a consent receipt, or a video together with
    all its consents in one atomic commit. ``confirm(plan)`` is asked before
    anything is deleted (and again only if what would be deleted changes);
    returning False raises Cancelled. Without it nothing is asked."""
    import random

    from consentvid import storage

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise UsageError(str(e), param_hint="URI") from None
    if at.collection not in RECORD_NSIDS:
        raise ConsentVidError(f"not a ConsentVid record: {at.collection}")
    if at.collection in CONSENT_NSIDS and delete_bytes:
        raise UsageError("--delete-bytes applies when revoking a video URI")
    did = client.me.did
    if at.did != did:
        raise ConsentVidError(f"not your record (it belongs to {at.did})")

    def ask(plan: RevokePlan) -> None:
        if confirm is not None and not confirm(plan):
            raise Cancelled("revoke cancelled; nothing was deleted")

    if at.collection in CONSENT_NSIDS:
        ask(RevokePlan(uri, "consent", None, [uri], False))
        client.com.atproto.repo.delete_record({"repo": did, "collection": at.collection, "rkey": at.rkey})
        _success(f"Withdrawn: {uri}")
        return RevokeOutcome([uri])

    # Plan, confirm, commit. swapCommit guards the WHOLE repo, which is the
    # contributor's everyday Bluesky repo: a like while a confirmation waits
    # must not make withdrawal fail. So the confirmation is for WHAT is
    # deleted (the consents and the video version), and the commit re-reads
    # the head right before it is sent, retrying a few times without asking
    # again unless what would be deleted changed (R3-05).
    _, video, linked = _video_revoke_plan(client, did, uri, at.rkey, at.collection)
    ask(RevokePlan(uri, "video", video, [u for u, _ in linked], delete_bytes))
    confirmed = _revoke_fingerprint(video, linked)
    for attempt in range(1, _REVOKE_ATTEMPTS + 1):
        head, video, linked = _video_revoke_plan(client, did, uri, at.rkey, at.collection)
        if _revoke_fingerprint(video, linked) != confirmed:
            _warn("what this revoke deletes changed since you confirmed it")
            ask(RevokePlan(uri, "video", video, [u for u, _ in linked], delete_bytes, changed=True))
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
        _info(f"Withdrawn: {c_uri}")
    _info(f"Deleted video record: {uri}")
    outcome = RevokeOutcome([u for u, _ in linked], uri)
    if delete_bytes:
        other = _bytes_shared(client, did, ref, video.get("contentHash"), exclude_uri=uri)
        if other:
            outcome.bytes_note = f"not deleting the bytes: another of your videos uses them ({other})"
            _warn(outcome.bytes_note)
        else:
            try:
                outcome.bytes_note = storage.delete_bytes(ref, video.get("contentHash") or "")
                outcome.bytes_deleted = True
                _info(outcome.bytes_note)
            except Exception as e:  # noqa: BLE001 — the records are gone; say the bytes may not be
                where = storage.public_url(ref) or ref.get("value")
                raise ConsentVidError(
                    f"the records were deleted, but deleting the bytes failed ({_safe(e)}). "
                    f"They may still be public at {_safe(where)}: delete them manually."
                ) from e
    elif storage.public_url(ref):
        outcome.bytes_note = (
            f"the bytes are still publicly reachable at {storage.public_url(ref)}; "
            "pass --delete-bytes to remove them"
        )
        _warn(outcome.bytes_note)
    return outcome
