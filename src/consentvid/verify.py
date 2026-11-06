"""Cryptographically verified record reads.

Reading a record with ``com.atproto.repo.getRecord`` trusts the server. Instead
we call ``com.atproto.sync.getRecord``, which returns a CAR file containing the
repo's latest *signed commit* plus the Merkle Search Tree (MST) path from the
commit to the record — or, if the record doesn't exist, the path proving its
absence. We then check, entirely locally:

1. every block we use hashes to the CID that references it;
2. the commit is for the expected DID and is signed by the signing key in the
   DID document (resolved independently of the PDS);
3. walking the MST from the commit's root reaches the record (presence) or a
   gap where it would be (absence).

So "valid" means "the contributor's key currently vouches for exactly these
bytes", and "revoked" means "the contributor's key vouches that the record is
gone". Neither depends on trusting the PDS. (A PDS could still serve a stale
commit — a rollback — which would need a relay/witness to detect.)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import libipld
from atproto import verify_signature
from atproto_client.exceptions import BadRequestError

from consentvid.errors import RepoUnavailable, VerificationError
from consentvid.identity import public_client, resolve_identity
from consentvid.records import AtUri, parse_at_uri

_CID_PREFIX = b"\x01\x71\x12\x20"  # CIDv1, dag-cbor, sha2-256, 32-byte digest
_REPO_STATUS_ERRORS = {
    "RepoNotFound": "deleted",
    "RepoDeactivated": "deactivated",
    "RepoTakendown": "takendown",
    "RepoSuspended": "suspended",
}


@dataclass(frozen=True)
class VerifiedRecord:
    uri: str
    present: bool
    cid: str | None  # record CID (base32) when present
    value: dict | None
    rev: str  # repo revision of the commit that vouches for this
    proven: bool  # False only for absence reported by a PDS without a proof


def fetch_verified(uri: str | AtUri) -> VerifiedRecord:
    """Fetch and verify a record. Raises RepoUnavailable, VerificationError, or
    network errors (atproto_client NetworkError / httpx errors) — callers decide
    whether those are fatal."""
    at = uri if isinstance(uri, AtUri) else parse_at_uri(uri)
    ident = resolve_identity(at.did)
    try:
        car = public_client(at.did).com.atproto.sync.get_record(
            {"did": at.did, "collection": at.collection, "rkey": at.rkey}
        )
    except BadRequestError as e:
        err, msg = _xrpc_error(e)
        if err in _REPO_STATUS_ERRORS:
            raise RepoUnavailable(at.did, _REPO_STATUS_ERRORS[err], msg) from e
        if "could not find repo" in msg.lower():
            raise RepoUnavailable(at.did, "deleted", msg) from e
        if err == "RecordNotFound":
            # Older PDS versions answer with an error instead of a proof of
            # absence. Authoritative, but not cryptographically proven.
            return VerifiedRecord(str(at), False, None, None, "", proven=False)
        raise
    present, cid, value, rev = verify_car(
        car, did=at.did, signing_key=ident.signing_key, key=f"{at.collection}/{at.rkey}"
    )
    return VerifiedRecord(str(at), present, cid, value, rev, proven=True)


def verify_car(car_bytes: bytes, *, did: str, signing_key: str, key: str):
    """Verify a sync.getRecord CAR. Returns (present, cid, value, rev).
    Any malformed structure raises VerificationError, never a bare KeyError."""
    try:
        return _verify_car(car_bytes, did=did, signing_key=signing_key, key=key)
    except VerificationError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise VerificationError(f"malformed repo data: {type(e).__name__}: {e}") from e


def _verify_car(car_bytes: bytes, *, did: str, signing_key: str, key: str):
    try:
        header, blocks = libipld.decode_car(car_bytes)
    except Exception as e:  # noqa: BLE001
        raise VerificationError(f"malformed CAR: {e}") from e
    roots = header.get("roots") or []
    if len(roots) != 1:
        raise VerificationError("CAR must have exactly one root (the commit)")
    commit = _block(blocks, roots[0])
    if commit.get("did") != did:
        raise VerificationError(f"commit is for {commit.get('did')}, expected {did}")
    if commit.get("version") != 3:
        raise VerificationError(f"unsupported repo commit version {commit.get('version')}")
    sig = commit.get("sig")
    if not isinstance(sig, bytes):
        raise VerificationError("commit is unsigned")
    unsigned = {k: v for k, v in commit.items() if k != "sig"}
    if not isinstance(commit.get("data"), bytes):
        raise VerificationError("commit has no data root")
    if not verify_signature(signing_key, libipld.encode_dag_cbor(unsigned), sig):
        raise VerificationError("commit signature does not match the DID's signing key")
    record_cid = _mst_lookup(blocks, commit["data"], key.encode())
    if record_cid is None:
        return False, None, None, commit.get("rev", "")
    value = _block(blocks, record_cid)
    if not isinstance(value, dict):
        raise VerificationError("record block is not an object")
    return True, libipld.encode_cid(record_cid), value, commit.get("rev", "")


def _block(blocks: dict, cid: bytes):
    if not isinstance(cid, bytes) or not cid.startswith(_CID_PREFIX) or len(cid) != 36:
        raise VerificationError("unexpected CID type (want CIDv1 dag-cbor sha256)")
    if cid not in blocks:
        raise VerificationError("proof is incomplete: referenced block missing from CAR")
    node = blocks[cid]
    # DAG-CBOR is canonical, so re-encoding reproduces the original bytes.
    if hashlib.sha256(libipld.encode_dag_cbor(node)).digest() != cid[4:]:
        raise VerificationError("block content does not match its CID")
    return node


def _mst_lookup(blocks: dict, root: bytes, key: bytes) -> bytes | None:
    """Walk the MST toward ``key``; return the record CID, or None if the walk
    proves the key absent. Every node on the path must be in the CAR."""
    node_cid = root
    depth = 0
    while node_cid is not None:
        depth += 1
        if depth > 64:
            raise VerificationError("MST too deep")
        node = _block(blocks, node_cid)
        entries = node.get("e") or []
        subtree = node.get("l")
        prev = b""
        for entry in entries:
            full = prev[: entry["p"]] + entry["k"]
            if full == key:
                return entry["v"]
            if key < full:
                break
            subtree = entry.get("t")
            prev = full
        node_cid = subtree
    return None


def _xrpc_error(e: Exception) -> tuple[str, str]:
    content = getattr(getattr(e, "response", None), "content", None)
    return (getattr(content, "error", "") or "", getattr(content, "message", "") or str(e))
