"""Identity resolution: handle <-> DID -> PDS endpoint + signing key.

Reads go straight to the contributor's own PDS, found from their DID document.
That's what makes read-only commands (resync, verify, export of someone else's
records, AppView backfill) work for accounts on ANY PDS, and without logging in.

DID documents, handles and PDS endpoints are chosen by contributors, so every
host we are told to contact must be a public https host: no IP literals (in
any spelling), no localhost / .local / .internal names, no names that resolve
to private addresses, no non-default ports and no redirects. Otherwise a
manifest could make a researcher's machine send requests into its private
network. (Residual: DNS rebinding between our lookup and the connection.)

Every answer from those hosts is also bounded: at most MAX_BODY decoded bytes
and DEADLINE seconds per response, and at most ``max_pages`` pages per
listing, so one hostile PDS cannot exhaust memory or stall a whole resync.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urljoin, urlsplit

import httpcore
import httpx
from atproto import Client, IdResolver
from atproto_client.request import Request, RequestBase, _handle_request_errors, _handle_response

from consentvid.errors import ConsentVidError, RepoUnavailable
from consentvid.records import (
    check_public_host,
    handle_tld_error,
    https_url_error,
    is_did,
    is_handle,
    is_ldh_host,
    url_host_port_error,
)

_TIMEOUT = 15.0  # per network read
DEADLINE = 60.0  # per response, overall
MAX_BODY = 8 * 1024 * 1024  # decoded bytes per response
MAX_PAGES = 100  # listRecords pages per collection
PAGE_LIMIT = 100  # records per listRecords page
# A single content coding, and only ones whose expansion per network chunk is
# bounded (deflate: ~1000x). No brotli/zstd, and no stacked codings
# ('gzip, gzip' would expand ~10^6x inside one read).
_ACCEPT_ENCODING = "gzip, deflate"
_DECODABLE = {"", "identity", "gzip", "x-gzip", "deflate"}
IDENTITY_TTL = 3600.0  # seconds a resolved DID document / PDS client is reused
MAX_HANDLE_REDIRECTS = 3  # https://<handle>/.well-known/atproto-did hops
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class HandleLookupError(ConsentVidError):
    """A handle lookup failed without an answer (DNS timeout or SERVFAIL, a
    network error, an HTTP 5xx). The handle spec treats these as retryable,
    not as "this handle has no DID": never cached as unverified."""


class ResponseTooLarge(ConsentVidError):
    """An untrusted server's answer exceeded the size or time budget. Not a
    transient network error: resync treats it as INVALID, never UNKNOWN."""


@dataclass(frozen=True)
class Identity:
    """What reading a repo needs: the PDS and the signing key. Handle
    verification is separate (verified_handle), as in @atproto/identity's
    DidResolver / HandleResolver split: resync, verify and backfill never
    pay for it."""

    did: str
    pds: str
    signing_key: str  # did:key multibase form
    claimed_handle: str | None = None  # first valid alsoKnownAs entry: UNVERIFIED

    @property
    def handle(self) -> str | None:
        """The bidirectionally verified handle, else None (network I/O on
        first use; cached)."""
        return verified_handle(self.did)


def _check_public_host(host: str | None, *, resolve: bool = True) -> None:
    """Raise ValueError unless ``host`` is a public DNS name (resolving it by
    default: every host checked here is about to be contacted)."""
    check_public_host(host, resolve=resolve)


def _check_url_host(url: str, expected: str) -> None:
    """Defence in depth: the host httpx will actually contact for ``url``
    must be the (already checked) ``expected`` host, and public."""
    try:
        actual = httpx.URL(url).host
    except Exception as e:  # noqa: BLE001 — httpx.InvalidURL and friends
        raise ValueError(f"malformed URL {url!r}: {e}") from None
    if actual != expected.lower().rstrip("."):
        raise ValueError(f"URL {url!r} would contact {actual!r}, not {expected!r}")
    check_public_host(actual, resolve=False)


def _insecure_allowed() -> bool:
    return os.environ.get("CONSENTVID_ALLOW_INSECURE") == "1"


# ----------------------------------------------- absolute per-request deadline
#
# httpx's timeouts are per socket operation, so a server dripping one header
# byte every few seconds (or a slow TLS handshake) is never cut off. The
# network backend below enforces an absolute deadline, taken from a context
# variable that capped_send sets, on every connect, TLS handshake, read and
# write of the connection.

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("consentvid_deadline", default=None)


def _remaining(timeout: float | None, exc: type[Exception]) -> float | None:
    d = _deadline.get()
    if d is None:
        return timeout
    left = d - time.monotonic()
    if left <= 0:
        raise exc("absolute deadline exceeded")
    return min(math.inf if timeout is None else timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _remaining(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        # The inner write loops sock.send() with the timeout per call; bound
        # each chunk so a peer that never drains its window is cut off too.
        view = memoryview(buffer)
        while view:
            n = min(len(view), 65536)
            self._inner.write(bytes(view[:n]), _remaining(timeout, httpcore.WriteTimeout))
            view = view[n:]

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None) -> httpcore.NetworkStream:
        inner = self._inner.start_tls(ssl_context, server_hostname, _remaining(timeout, httpcore.ConnectTimeout))
        return _DeadlineStream(inner)

    def get_extra_info(self, info: str):
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        timeout = _remaining(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(super().connect_tcp(host, port, timeout, local_address, socket_options))

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        timeout = _remaining(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(super().connect_unix_socket(path, timeout, socket_options))


def _install_deadline(client: httpx.Client) -> httpx.Client:
    """Put the deadline-aware backend under every real (httpcore) transport
    of ``client``. Test doubles (MockTransport) have none and are left alone.
    Relies on httpx.HTTPTransport._pool._network_backend (httpx 0.28 /
    httpcore 1.x); tests/test_identity.py fails loudly if that changes."""
    transports = [client._transport, *(t for t in client._mounts.values() if t is not None)]
    for t in transports:
        if isinstance(t, httpx.HTTPTransport):
            pool = t._pool
            if not hasattr(pool, "_network_backend"):
                raise RuntimeError("httpx/httpcore internals changed: no _pool._network_backend")
            pool._network_backend = _DeadlineBackend()
    return client


def capped_send(client: httpx.Client, request: httpx.Request) -> httpx.Response:
    """Send ``request`` and read the (decoded) body within MAX_BODY bytes and
    DEADLINE seconds (connect, TLS, headers and body together, when the client
    went through _install_deadline). Returns a fully read Response."""
    deadline = time.monotonic() + DEADLINE
    host = request.url.host
    request.headers["Accept-Encoding"] = _ACCEPT_ENCODING
    token = _deadline.set(deadline)
    try:
        try:
            resp = client.send(request, stream=True)
        except httpx.TimeoutException:
            if time.monotonic() >= deadline:
                raise ResponseTooLarge(f"{host} took longer than {DEADLINE:.0f}s to answer") from None
            raise
        try:
            coding = resp.headers.get("content-encoding", "").strip().lower()
            if coding not in _DECODABLE:
                raise ResponseTooLarge(f"{host} sent an unsupported content encoding {coding!r}")
            buf = bytearray()
            try:
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BODY:
                        raise ResponseTooLarge(f"{host} sent more than {MAX_BODY} bytes")
                    if time.monotonic() > deadline:
                        raise ResponseTooLarge(f"{host} took longer than {DEADLINE:.0f}s to answer")
            except httpx.TimeoutException:
                if time.monotonic() >= deadline:
                    raise ResponseTooLarge(f"{host} took longer than {DEADLINE:.0f}s to answer") from None
                raise
        finally:
            resp.close()
    finally:
        _deadline.reset(token)
    headers = [
        (k, v)
        for k, v in resp.headers.multi_items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    ]
    return httpx.Response(resp.status_code, headers=headers, content=bytes(buf), request=request)


class _NoRedirectRequest(Request):
    """atproto's Request hard-codes follow_redirects=True and reads bodies
    without limit; a PDS (untrusted) must not be able to bounce us to another
    host, nor answer with gigabytes or a byte every 14 seconds."""

    def __init__(self, **kwargs) -> None:
        RequestBase.__init__(self)
        self._client = _install_deadline(httpx.Client(follow_redirects=False, timeout=_TIMEOUT, **kwargs))

    def _send_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = self.get_headers(kwargs.pop("headers", None))
        kwargs.pop("follow_redirects", None)
        try:
            req = self._client.build_request(method=method, url=url, headers=headers, **kwargs)
            return _handle_response(capped_send(self._client, req))
        except ResponseTooLarge:
            raise
        except Exception as e:
            _handle_request_errors(e)
            raise


@lru_cache(maxsize=1)
def _http() -> httpx.Client:
    return _install_deadline(httpx.Client(follow_redirects=False, timeout=_TIMEOUT))


def capped_get(url: str) -> httpx.Response:
    """GET from an untrusted host with the same bounds as PDS reads. Raises
    httpx.HTTPStatusError for non-2xx answers."""
    client = _http()
    resp = capped_send(client, client.build_request("GET", url))
    resp.raise_for_status()
    return resp


@lru_cache(maxsize=1)
def _resolver() -> IdResolver:
    return IdResolver(timeout=_TIMEOUT)


def _txt_records(name: str) -> list[str]:
    """TXT strings at ``name`` (each record's character strings joined).
    Empty on a definite "none" (NXDOMAIN, no TXT answer); HandleLookupError
    when DNS gave no answer at all (timeout, SERVFAIL, no nameservers)."""
    import dns.exception
    import dns.resolver

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError) as e:
        raise HandleLookupError(f"DNS lookup of {name} failed ({type(e).__name__})") from e
    return [b"".join(rdata.strings).decode("utf-8", "replace") for rdata in answers]


def _resolve_handle_dns(handle: str) -> str | None:
    """_atproto.<handle> TXT "did=...". Several records naming different DIDs
    make resolution fail (atproto.com/specs/handle)."""
    dids = set()
    for text in _txt_records(f"_atproto.{handle}"):
        if text.startswith("did="):
            did = text[len("did=") :].strip()
            if is_did(did):
                dids.add(did)
    return next(iter(dids)) if len(dids) == 1 else None


def _resolve_handle_https(handle: str) -> str | None:
    """https://<handle>/.well-known/atproto-did, following up to
    MAX_HANDLE_REDIRECTS redirects by hand (the spec allows them); every hop
    must be a public https host on port 443, checked before it is contacted.
    None is a definite answer (a 2xx without a DID, a 4xx, an unsafe host);
    a transport error or a 5xx raises HandleLookupError."""
    url = f"https://{handle}/.well-known/atproto-did"
    client = _http()
    try:
        for hop in range(MAX_HANDLE_REDIRECTS + 1):
            if https_url_error(url) or url_host_port_error(url):
                return None
            host = urlsplit(url).hostname
            check_public_host(host, resolve=True)
            _check_url_host(url, host)
            resp = capped_send(client, client.build_request("GET", url))
            if resp.status_code in _REDIRECT_CODES:
                location = resp.headers.get("location")
                if not location or hop == MAX_HANDLE_REDIRECTS:
                    return None
                url = urljoin(url, location)
                continue
            if resp.status_code >= 500:
                raise HandleLookupError(f"{urlsplit(url).hostname} answered HTTP {resp.status_code}")
            if not 200 <= resp.status_code < 300:
                return None
            lines = resp.text.strip().splitlines()
            text = lines[0].strip() if lines else ""
            return text if is_did(text) else None
    except HandleLookupError:
        raise
    except httpx.TransportError as e:
        raise HandleLookupError(f"https://{handle}{urlsplit(url).path} unreachable ({type(e).__name__})") from e
    except (ValueError, httpx.HTTPError, ConsentVidError):
        return None
    return None


def _resolve_handle(handle: str) -> str | None:
    """Handle -> DID via DNS TXT, then HTTPS well-known (capped, public hosts
    only). ``handle`` must already be lowercase."""
    r = _resolver().handle
    if not hasattr(r, "resolve_dns"):  # test doubles
        return r.resolve(handle)
    error: HandleLookupError | None = None
    for method in (_resolve_handle_dns, _resolve_handle_https):
        try:
            did = method(handle)
        except HandleLookupError as e:
            did, error = None, error or e
        if did:
            return did
    if error is not None:
        # Neither method answered with a DID, and one could not answer at
        # all: that is "unknown", not "this handle has no DID".
        raise error
    return None


@lru_cache(maxsize=1024)
def resolve_did(handle_or_did: str) -> str:
    """Accept a handle (with or without leading @) or a DID; return the DID."""
    s = handle_or_did.strip().removeprefix("@")
    if is_did(s):
        return s
    if not is_handle(s):
        raise ConsentVidError(f"{handle_or_did!r} is neither a DID nor a valid handle")
    why = handle_tld_error(s)
    if why:
        raise ConsentVidError(f"{handle_or_did!r}: {why}")
    did = _resolve_handle(s.lower())
    if not did:
        raise ConsentVidError(f"could not resolve handle {s!r}")
    return did


def _did_web_host(did: str) -> str:
    parts = did.split(":")
    host = unquote(parts[2]) if len(parts) == 3 else ""
    if len(parts) != 3 or ":" in host or "/" in host:
        raise RepoUnavailable(did, "invalid", f"{did}: did:web with a port or path is not supported")
    if not is_ldh_host(host):
        # '%3F' / '%23' / '%40' would end the host early once put in a URL.
        raise RepoUnavailable(did, "invalid", f"{did}: did:web host is not a plain ASCII host name")
    # atproto.com/specs/did: the handle TLD restrictions (.arpa, .test, ...)
    # apply to did:web domains too.
    why = handle_tld_error(host)
    if why:
        raise RepoUnavailable(did, "invalid", f"{did}: {why}")
    try:
        _check_public_host(host)
    except ValueError as e:
        raise RepoUnavailable(did, "invalid", f"{did}: {e}") from None
    return host


def _check_did_web(did: str) -> None:
    _did_web_host(did)


def _resolve_did_web(did: str):
    """did:web document, fetched with the capped client (the host is
    untrusted) and validated like atproto's resolver does."""
    from atproto_identity.did.resolvers.base_resolver import _BaseResolver
    from atproto_identity.exceptions import DidWebResolverError

    host = _did_web_host(did)
    url = f"https://{host}/.well-known/did.json"
    try:
        _check_url_host(url, host)
    except ValueError as e:
        raise RepoUnavailable(did, "invalid", f"{did}: {e}") from None
    try:
        body = capped_get(url).content
    except httpx.HTTPError as e:
        raise DidWebResolverError(f"Error resolving DID {did}") from e
    try:
        value = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise RepoUnavailable(did, "invalid", f"{did}: did.json is not JSON") from e
    try:
        return _BaseResolver.validate_did_doc(did, value)
    except Exception as e:  # noqa: BLE001 — malformed document
        raise RepoUnavailable(did, "invalid", f"{did}: invalid DID document ({e})") from e


def _resolve_doc(did: str):
    """The DID document, or RepoUnavailable(deleted) for a tombstoned DID."""
    from atproto_identity.exceptions import DidPlcResolverError, DidWebResolverError

    def fetch():
        # did:plc goes to plc.directory (a trust root); did:web to the
        # contributor's own host, so it gets the capped client.
        return _resolve_did_web(did) if did.startswith("did:web:") else _resolver().did.resolve(did)

    try:
        # A directory 5xx or a network blip is retried like every other
        # read; a 404/410 (tombstone) is not transient and is mapped below.
        doc = with_retry(fetch, what="DID resolution")
    except (DidPlcResolverError, DidWebResolverError) as e:
        cause = e.__cause__
        if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (404, 410):
            raise RepoUnavailable(did, "deleted", f"{did} is deleted/tombstoned") from e
        raise
    if doc is None:
        raise RepoUnavailable(did, "deleted", f"no DID document for {did} (account deleted?)")
    return doc


def _check_pds(pds: str) -> None:
    u = urlsplit(pds)
    if u.scheme != "https" or u.username is not None or u.password is not None:
        raise ValueError(f"PDS endpoint must be a plain https:// URL: {pds}")
    # atproto.com/specs/did: the serviceEndpoint is scheme, host and optional
    # port only (a path would be prefixed to /xrpc by some clients only).
    if u.path not in ("", "/") or u.query or u.fragment or "?" in pds or "#" in pds:
        raise ValueError(f"PDS endpoint must be an origin with no path, query or fragment: {pds}")
    if u.port not in (None, 443) and not _insecure_allowed():
        raise ValueError(f"PDS endpoint must use the default https port: {pds}")
    _check_public_host(u.hostname)
    _check_url_host(pds, u.hostname)


class _TTLCache:
    """A small thread-safe cache whose entries expire after IDENTITY_TTL, so
    a key rotation or PDS migration is picked up by long-lived processes."""

    _MISS = object()

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
        if hit is None or time.monotonic() - hit[1] > IDENTITY_TTL:
            return self._MISS
        return hit[0]

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic())

    def pop(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_identities = _TTLCache()
_clients = _TTLCache()
_handles = _TTLCache()


def _claimed_handle(doc) -> str | None:
    """The first syntactically valid at:// handle in alsoKnownAs, lowercased
    (atproto.com/specs/did: the first valid entry is the claim)."""
    aka = getattr(doc, "also_known_as", None)
    if aka is None:
        h = doc.get_handle()
        aka = [f"at://{h}"] if h else []
    for entry in aka:
        if isinstance(entry, str) and entry.startswith("at://") and is_handle(entry[len("at://") :]):
            return entry[len("at://") :].lower()
    return None


def _resolve_identity(did: str) -> Identity:
    if not is_did(did):
        raise RepoUnavailable(str(did), "invalid", f"not a DID: {did!r}")
    if did.startswith("did:web:"):
        _check_did_web(did)
    doc = _resolve_doc(did)
    try:
        pds, signing_key, claimed_handle = doc.get_pds_endpoint(), doc.get_did_key(), _claimed_handle(doc)
    except Exception as e:  # noqa: BLE001 — malformed document
        raise RepoUnavailable(did, "invalid", f"DID document for {did} is malformed: {e}") from e
    if not pds or not signing_key:
        raise RepoUnavailable(did, "invalid", f"DID document for {did} lacks a PDS or signing key")
    try:
        _check_pds(pds)
    except ValueError as e:
        raise RepoUnavailable(did, "invalid", f"DID document for {did}: {e}") from None
    return Identity(did=did, pds=pds.rstrip("/"), signing_key=signing_key, claimed_handle=claimed_handle)


def resolve_identity(did: str) -> Identity:
    """DID -> PDS and signing key (plus the unverified handle claim). No
    handle lookups. Cached for IDENTITY_TTL; failures are not cached."""
    hit = _identities.get(did)
    if hit is not _TTLCache._MISS:
        return hit
    ident = _resolve_identity(did)
    _identities.set(did, ident)
    return ident


def verified_handle(did: str, *, raise_errors: bool = False) -> str | None:
    """The DID document's handle claim if the handle points back to ``did``
    (bidirectional verification, atproto.com/specs/handle), else None.
    Only definite answers are cached (for IDENTITY_TTL): a lookup that
    failed (HandleLookupError: DNS timeout, network error, 5xx) returns None
    uncached, or raises with ``raise_errors``.

    The handle host itself is never contacted for the DNS TXT method; the
    HTTPS well-known method checks every host it contacts (public, port
    443) in _resolve_handle_https."""
    hit = _handles.get(did)
    if hit is not _TTLCache._MISS:
        return hit
    try:
        claimed = resolve_identity(did).claimed_handle
    except Exception:  # noqa: BLE001 — no identity, no verified handle (not cached)
        return None
    handle = None
    if claimed and not handle_tld_error(claimed):
        try:
            if _resolve_handle(claimed) == did:
                handle = claimed
        except HandleLookupError:
            if raise_errors:
                raise
            return None  # unknown, not unverified: don't cache it
        except Exception:  # noqa: BLE001 — handle verification is best-effort
            pass
    _handles.set(did, handle)
    return handle


def client_for(pds: str) -> Client:
    """Unauthenticated client for the PDS at ``pds`` (an already checked
    endpoint from a DID document). Redirects are not followed and answers
    are size/time bounded. Cached per PDS URL, so a client can never point
    at a different host than the identity it was looked up for."""
    hit = _clients.get(pds)
    if hit is not _TTLCache._MISS:
        return hit
    client = Client(base_url=pds, request=_NoRedirectRequest())
    _clients.set(pds, client)
    return client


def public_client(did: str) -> Client:
    """Unauthenticated client pointed at the PDS that hosts ``did``'s repo
    (per the cached DID document: the two never disagree)."""
    return client_for(resolve_identity(did).pds)


def invalidate_identity(did: str) -> None:
    """Forget the cached DID document and handle for ``did`` (after a
    signature mismatch or a repo-status error: the key may have rotated or
    the repo moved). Clients are per PDS URL and stay valid."""
    _identities.pop(did)
    _handles.pop(did)


def _clear_identities() -> None:
    _identities.clear()
    _handles.clear()


resolve_identity.cache_clear = _clear_identities  # type: ignore[attr-defined]
verified_handle.cache_clear = _handles.clear  # type: ignore[attr-defined]
public_client.cache_clear = _clients.clear  # type: ignore[attr-defined]


# ------------------------------------------------- rate limits and retries
#
# Bluesky's PDSes allow about 3000 reads per 5 minutes per client IP
# ("ratelimit-policy: 3000;w=300") and answer 429 beyond that. Reads honour
# Retry-After / RateLimit-Reset (bounded per call and per process), retry
# transient failures twice with jitter, and run at most HOST_CONCURRENCY at
# a time against one PDS host.

HOST_CONCURRENCY = 4
MAX_RATE_LIMIT_WAITS = 3  # per call
MAX_RATE_LIMIT_WAIT_S = 60.0  # per wait
RETRY_BUDGET_S = 300.0  # total sleeping per process
MAX_TRANSIENT_RETRIES = 2
_sleep = time.sleep  # tests replace this
_budget_lock = threading.Lock()
_budget_used = 0.0
_host_slots: dict[str, threading.Semaphore] = {}
_host_slots_lock = threading.Lock()


def reset_retry_budget() -> None:
    global _budget_used
    with _budget_lock:
        _budget_used = 0.0


def _take_budget(seconds: float) -> bool:
    global _budget_used
    with _budget_lock:
        if _budget_used + seconds > RETRY_BUDGET_S:
            return False
        _budget_used += seconds
        return True


def _chain(e: BaseException | None, limit: int = 16) -> list[BaseException]:
    """``e`` and its causes/contexts (bounded, cycle-safe): resolver and SDK
    errors wrap the httpx error that carries the real status."""
    out, seen = [], set()
    while e is not None and id(e) not in seen and len(out) < limit:
        seen.add(id(e))
        out.append(e)
        e = e.__cause__ or e.__context__
    return out


def status_of(e: BaseException) -> int | None:
    """HTTP status of an SDK or httpx error (or of the error it wraps, e.g.
    a DidPlcResolverError around an httpx.HTTPStatusError), if any."""
    for x in _chain(e):
        code = getattr(getattr(x, "response", None), "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def _header(e: BaseException, name: str) -> str | None:
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        for k, v in dict(headers).items():
            if str(k).lower() == name:
                return str(v)
    except (TypeError, ValueError):
        return None
    return None


def rate_limit_reset(e: BaseException) -> float | None:
    """Epoch seconds at which a 429'd request may be retried, from
    RateLimit-Reset (epoch seconds) or Retry-After (delta seconds)."""
    now = time.time()
    reset = _header(e, "ratelimit-reset")
    try:
        if reset is not None:
            value = float(reset)
            # Some servers send a delta here; treat small values as one.
            return value if value > 1e9 else now + value
    except ValueError:
        pass
    after = _header(e, "retry-after")
    try:
        if after is not None:
            return now + float(after)
    except ValueError:
        pass
    return None


def _is_transient_error(e: BaseException) -> bool:
    import ssl

    from atproto_client.exceptions import NetworkError

    chain = _chain(e)
    if any(isinstance(x, (ResponseTooLarge, ssl.SSLError)) for x in chain):
        return False
    code = status_of(e)
    if code is not None:
        return code >= 500
    return any(isinstance(x, (NetworkError, httpx.TransportError, OSError, TimeoutError)) for x in chain)


def with_retry(fn, *, what: str = "request", retry_server_errors: bool = True):
    """Call ``fn()``; on 429 wait for the server's reset (at most
    MAX_RATE_LIMIT_WAITS times, MAX_RATE_LIMIT_WAIT_S each, within the
    process-wide RETRY_BUDGET_S), and retry network errors (and 5xx, unless
    ``retry_server_errors`` is False) twice with 1-4 s jitter. Re-raises the
    last error once the attempts or the budget are spent."""
    rate_waits = transient = 0
    while True:
        try:
            return fn()
        except ResponseTooLarge:
            raise
        except Exception as e:
            code = status_of(e)
            if code == 429 and rate_waits < MAX_RATE_LIMIT_WAITS:
                reset = rate_limit_reset(e)
                wait = min(max((reset - time.time()) if reset else 1.0, 0.0), MAX_RATE_LIMIT_WAIT_S)
                if not _take_budget(wait):
                    raise
                rate_waits += 1
                _sleep(wait)
                continue
            if (
                code != 429
                and transient < MAX_TRANSIENT_RETRIES
                and _is_transient_error(e)
                and (retry_server_errors or code is None)
            ):
                wait = random.uniform(1.0, 4.0)
                if not _take_budget(wait):
                    raise
                transient += 1
                _sleep(wait)
                continue
            raise


@contextlib.contextmanager
def host_slot(pds: str):
    """At most HOST_CONCURRENCY concurrent reads against one PDS host
    (``pds`` is its URL); a resync's workers would otherwise all hit it."""
    host = urlsplit(pds).hostname or pds
    with _host_slots_lock:
        sem = _host_slots.setdefault(host, threading.Semaphore(HOST_CONCURRENCY))
    with sem:
        yield


def list_all(client, did: str, collection: str, *, max_pages: int = MAX_PAGES) -> list:
    """Every record of ``collection`` in ``did``'s repo, via listRecords.

    The PDS is untrusted: a repeated cursor, more than ``max_pages`` pages or a
    page longer than requested raises ConsentVidError instead of looping or
    growing without bound."""
    out, cursor, seen = [], None, set()
    for _ in range(max_pages):
        params = {"repo": did, "collection": collection, "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        resp = with_retry(lambda params=params: client.com.atproto.repo.list_records(params), what="listRecords")
        records = list(resp.records or [])
        if len(records) > PAGE_LIMIT:
            raise ConsentVidError(f"PDS for {did} returned {len(records)} records for a page of {PAGE_LIMIT}")
        out.extend(records)
        cursor = resp.cursor
        if not cursor or not records:
            return out
        if cursor in seen:
            raise ConsentVidError(f"PDS for {did} repeated listRecords cursor {cursor!r}")
        seen.add(cursor)
    raise ConsentVidError(f"PDS for {did} returned more than {max_pages} pages of {collection}")


def display_name(did: str) -> str:
    """The verified handle, else the DID (never an unverified claim)."""
    return verified_handle(did) or did
