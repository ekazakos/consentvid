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

import contextvars
import json
import math
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urlsplit

import httpcore
import httpx
from atproto import Client, IdResolver
from atproto_client.request import Request, RequestBase, _handle_request_errors, _handle_response

from consentvid.errors import ConsentVidError, RepoUnavailable
from consentvid.records import check_public_host, is_did, is_handle, is_ldh_host

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


class ResponseTooLarge(ConsentVidError):
    """An untrusted server's answer exceeded the size or time budget. Not a
    transient network error: resync treats it as INVALID, never UNKNOWN."""


@dataclass(frozen=True)
class Identity:
    did: str
    pds: str
    signing_key: str  # did:key multibase form
    handle: str | None  # bidirectionally verified, else None


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


def _resolve_handle(handle: str) -> str | None:
    """Handle -> DID via DNS TXT, then https://<handle>/.well-known/atproto-did
    (capped, public hosts only)."""
    r = _resolver().handle
    if not hasattr(r, "resolve_dns"):  # test doubles
        return r.resolve(handle)
    did = r.resolve_dns(handle)
    if did:
        return did
    try:
        check_public_host(handle, resolve=True)
        url = f"https://{handle}/.well-known/atproto-did"
        _check_url_host(url, handle)
        text = capped_get(url).text.strip()
    except (ValueError, httpx.HTTPError, ConsentVidError):
        return None
    return text if is_did(text) else None


@lru_cache(maxsize=1024)
def resolve_did(handle_or_did: str) -> str:
    """Accept a handle (with or without leading @) or a DID; return the DID."""
    s = handle_or_did.strip().removeprefix("@")
    if is_did(s):
        return s
    if not is_handle(s):
        raise ConsentVidError(f"{handle_or_did!r} is neither a DID nor a valid handle")
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

    try:
        # did:plc goes to plc.directory (a trust root); did:web to the
        # contributor's own host, so it gets the capped client.
        doc = _resolve_did_web(did) if did.startswith("did:web:") else _resolver().did.resolve(did)
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
    if u.port not in (None, 443) and not _insecure_allowed():
        raise ValueError(f"PDS endpoint must use the default https port: {pds}")
    _check_public_host(u.hostname)
    _check_url_host(pds, u.hostname)


@lru_cache(maxsize=1024)
def resolve_identity(did: str) -> Identity:
    if not is_did(did):
        raise RepoUnavailable(str(did), "invalid", f"not a DID: {did!r}")
    if did.startswith("did:web:"):
        _check_did_web(did)
    doc = _resolve_doc(did)
    try:
        pds, signing_key, claimed_handle = doc.get_pds_endpoint(), doc.get_did_key(), doc.get_handle()
    except Exception as e:  # noqa: BLE001 — malformed document
        raise RepoUnavailable(did, "invalid", f"DID document for {did} is malformed: {e}") from e
    if not pds or not signing_key:
        raise RepoUnavailable(did, "invalid", f"DID document for {did} lacks a PDS or signing key")
    try:
        _check_pds(pds)
    except ValueError as e:
        raise RepoUnavailable(did, "invalid", f"DID document for {did}: {e}") from None
    handle = None
    if claimed_handle and is_handle(claimed_handle):
        try:
            _check_public_host(claimed_handle)
            # A DID doc's handle claim only counts if the handle points back.
            if _resolve_handle(claimed_handle) == did:
                handle = claimed_handle
        except Exception:  # noqa: BLE001 — handle verification is best-effort
            pass
    return Identity(did=did, pds=pds.rstrip("/"), signing_key=signing_key, handle=handle)


@lru_cache(maxsize=256)
def public_client(did: str) -> Client:
    """Unauthenticated client pointed at the PDS that hosts ``did``'s repo.
    Redirects are not followed and answers are size/time bounded."""
    return Client(base_url=resolve_identity(did).pds, request=_NoRedirectRequest())


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
        resp = client.com.atproto.repo.list_records(params)
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
    try:
        return resolve_identity(did).handle or did
    except Exception:  # noqa: BLE001
        return did
