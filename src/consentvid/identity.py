"""Identity resolution: handle <-> DID -> PDS endpoint + signing key.

Reads go straight to the contributor's own PDS, found from their DID document.
That's what makes read-only commands (resync, verify, export of someone else's
records, AppView backfill) work for accounts on ANY PDS, and without logging in.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from atproto import Client, IdResolver

from consentvid.errors import ConsentVidError, RepoUnavailable
from consentvid.records import is_did, is_handle

_TIMEOUT = 15.0


@dataclass(frozen=True)
class Identity:
    did: str
    pds: str
    signing_key: str  # did:key multibase form
    handle: str | None  # bidirectionally verified, else None


@lru_cache(maxsize=1)
def _resolver() -> IdResolver:
    return IdResolver(timeout=_TIMEOUT)


@lru_cache(maxsize=1024)
def resolve_did(handle_or_did: str) -> str:
    """Accept a handle (with or without leading @) or a DID; return the DID."""
    s = handle_or_did.strip().removeprefix("@")
    if is_did(s):
        return s
    if not is_handle(s):
        raise ConsentVidError(f"{handle_or_did!r} is neither a DID nor a valid handle")
    did = _resolver().handle.resolve(s.lower())
    if not did:
        raise ConsentVidError(f"could not resolve handle {s!r}")
    return did


@lru_cache(maxsize=1024)
def resolve_identity(did: str) -> Identity:
    data = _resolver().did.resolve_atproto_data(did)
    if data is None:
        # No DID document: the DID was tombstoned (account deleted) or never existed.
        raise RepoUnavailable(did, "deleted", f"no DID document for {did} (account deleted?)")
    if not data.pds or not data.signing_key:
        raise RepoUnavailable(did, "invalid", f"DID document for {did} lacks a PDS or signing key")
    handle = None
    if data.handle:
        try:
            # A DID doc's handle claim only counts if the handle points back.
            if _resolver().handle.resolve(data.handle) == did:
                handle = data.handle
        except Exception:  # noqa: BLE001 — handle verification is best-effort
            pass
    return Identity(did=did, pds=data.pds.rstrip("/"), signing_key=data.signing_key, handle=handle)


@lru_cache(maxsize=256)
def public_client(did: str) -> Client:
    """Unauthenticated client pointed at the PDS that hosts ``did``'s repo."""
    return Client(base_url=resolve_identity(did).pds)


def display_name(did: str) -> str:
    try:
        return resolve_identity(did).handle or did
    except Exception:  # noqa: BLE001
        return did
