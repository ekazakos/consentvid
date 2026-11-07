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

So "valid" means "the holder of the DID's current #atproto signing key vouches
for exactly these bytes", and "revoked" means "the holder of that key vouches
that the record is gone". That removes trust in transport, mirrors, relays and
caches — but NOT in the hosting PDS for typical accounts: on a standard PDS
(e.g. bsky.social) the PDS operator holds the repo signing key, so a malicious
or compromised PDS can sign commits that re-add a deleted consent or forge a
new one. Consumers should use manifests that pin the consent and video CIDs
(the default export), which limits a forging PDS to replaying the exact
records the manifest already references. A PDS can also serve a stale commit
(a rollback): resync compares the commit's rev with the newest rev a relay
has seen (resync.STALE), and commits dated more than five minutes in the
future are refused here, so a forged far-future rev can't outrank them.

Robustness, as the sync spec asks:

* a signature that doesn't match is retried once per DID and process with a
  freshly resolved DID document (the key may have rotated, or the repo moved);
* a PDS whose ``sync.getRecord`` is broken (5xx / not implemented) is read
  with ``sync.getLatestCommit`` + ``sync.getBlocks`` instead, walking the
  same MST path one block at a time; every block is still checked against
  its CID, so the fallback trusts nothing more;
* reads honour 429 rate limits and retry transient errors
  (:func:`consentvid.identity.with_retry`), at most a few at a time per host.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

import libipld
from atproto import verify_signature
from atproto_client.exceptions import BadRequestError

from consentvid.errors import RepoUnavailable, SignatureMismatch, VerificationError
from consentvid.identity import (
    client_for,
    host_slot,
    invalidate_identity,
    resolve_identity,
    status_of,
    with_retry,
)
from consentvid.records import AtUri, decode_tid, is_nsid, is_record_key, is_tid, parse_at_uri

_CID_PREFIX = b"\x01\x71\x12\x20"  # CIDv1, dag-cbor, sha2-256, 32-byte digest
_REPO_STATUS_ERRORS = {
    "RepoNotFound": "deleted",
    "RepoDeactivated": "deactivated",
    "RepoTakendown": "takendown",
    "RepoSuspended": "suspended",
}
# getBlocks fallback: the commit, at most 64 MST nodes (the depth cap), and
# the record block.
MAX_BLOCK_FETCHES = 66
# Entries per MST node. With fanout 4 a real node holds a handful; the cap
# only bounds what a hostile PDS can make us decode.
MAX_NODE_ENTRIES = 1024
# Blocks per CAR. A getRecord proof holds the commit, one MST path and the
# record; the cap (with identity.MAX_BODY on the response) bounds the rest.
MAX_CAR_BLOCKS = 4096
_refreshed: set[str] = set()  # DIDs whose identity was re-resolved after a mismatch
_moved: set[str] = set()  # DIDs re-resolved after a repo-status error (R3-04)
_refreshed_lock = threading.Lock()


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
    try:
        return _fetch_verified(at)
    except SignatureMismatch:
        # Sync spec: on a signature failure, refresh the identity in case it
        # recently changed. Once per DID and process, so a hostile PDS can't
        # make us hammer the DID directory.
        with _refreshed_lock:
            if at.did in _refreshed:
                raise
            _refreshed.add(at.did)
        invalidate_identity(at.did)
        return _fetch_verified(at)
    except RepoUnavailable as e:
        if e.pds is None:
            raise  # the DID document itself said so (tombstone, invalid)
        # A PDS the account has left answers RepoDeactivated, or RepoNotFound
        # once the old copy is deleted. Before calling that a deactivation
        # or a deletion, re-resolve the DID (the sync spec's advice when
        # hosting may have changed) and ask the PDS it names now, if that is
        # another one. Once per DID and process.
        with _refreshed_lock:
            if at.did in _moved:
                raise
            _moved.add(at.did)
        invalidate_identity(at.did)
        if resolve_identity(at.did).pds == e.pds:
            raise
        return _fetch_verified(at)


def _fetch_verified(at: AtUri) -> VerifiedRecord:
    ident = resolve_identity(at.did)
    # The client comes from the same identity as the host slot and the key:
    # never another PDS than the one the DID document named (R3-04).
    client = client_for(ident.pds)
    key = f"{at.collection}/{at.rkey}"
    fallback = False
    with host_slot(ident.pds):
        try:
            car = with_retry(
                lambda: client.com.atproto.sync.get_record(
                    {"did": at.did, "collection": at.collection, "rkey": at.rkey}
                ),
                what="sync.getRecord",
                retry_server_errors=False,  # a broken getRecord goes to the fallback
            )
        except BadRequestError as e:
            err, _ = _xrpc_error(e)
            _raise_repo_status(at, e, ident.pds)
            if err == "RecordNotFound":
                # Older PDS versions answer with an error instead of a proof of
                # absence. Authoritative, but not cryptographically proven.
                return VerifiedRecord(str(at), False, None, None, "", proven=False)
            if err != "MethodNotImplemented":
                raise
            fallback = True
        except Exception as e:
            if not _get_record_broken(e):
                raise
            fallback = True
    if fallback:  # outside the host slot: the fallback takes its own
        return _fetch_via_blocks(client, at, ident, key)
    present, cid, value, rev = verify_car(car, did=at.did, signing_key=ident.signing_key, key=key)
    return VerifiedRecord(str(at), present, cid, value, rev, proven=True)


def _raise_repo_status(at: AtUri, e: BadRequestError, pds: str) -> None:
    """Raise RepoUnavailable (naming the ``pds`` that said so) if ``e`` says
    the repo itself is unavailable (RepoNotFound / RepoDeactivated /
    RepoTakendown / RepoSuspended, or the reference PDS's 'Could not find
    repo'); otherwise return."""
    err, msg = _xrpc_error(e)
    if err in _REPO_STATUS_ERRORS:
        raise RepoUnavailable(at.did, _REPO_STATUS_ERRORS[err], msg, pds=pds) from e
    if "could not find repo" in msg.lower():
        raise RepoUnavailable(at.did, "deleted", msg, pds=pds) from e


def _get_record_broken(e: BaseException) -> bool:
    """sync.getRecord failed in a way that getLatestCommit + getBlocks may
    not (some PDS implementations answer 500 / 501 to it)."""
    code = status_of(e)
    if code is not None and code >= 500:
        return True
    return _xrpc_error(e)[0] == "MethodNotImplemented"


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise VerificationError("malformed CAR: bad varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def _car_blocks(car) -> tuple[list, dict[bytes, bytes]]:
    """(header roots, {CID bytes: raw block bytes}) of a CAR v1. Blocks are
    kept as received: a CID binds the raw bytes (_block hashes them before
    decoding), so a non-canonical encoding can't make a valid block look
    tampered. Accepts an empty roots list, which is what sync.getBlocks
    returns. Only CIDv1 dag-cbor sha-256 blocks are kept."""
    if not isinstance(car, (bytes, bytearray)):
        raise VerificationError("the PDS did not return a CAR")
    car = bytes(car)
    n, pos = _varint(car, 0)
    if pos + n > len(car):
        raise VerificationError("malformed CAR: truncated header")
    try:
        header = libipld.decode_dag_cbor(car[pos : pos + n])
    except Exception as e:  # noqa: BLE001
        raise VerificationError(f"malformed CAR header: {e}") from e
    if not isinstance(header, dict) or header.get("version") != 1 or not isinstance(header.get("roots"), list):
        raise VerificationError("malformed CAR header (want version 1 with a roots list)")
    pos += n
    blocks: dict[bytes, bytes] = {}
    count = 0
    while pos < len(car):
        count += 1
        if count > MAX_CAR_BLOCKS:
            raise VerificationError(f"CAR has more than {MAX_CAR_BLOCKS} blocks")
        n, pos = _varint(car, pos)
        end = pos + n
        if end > len(car) or n < 36:
            raise VerificationError("malformed CAR: truncated block")
        cid, data = car[pos : pos + 36], car[pos + 36 : end]
        pos = end
        if not cid.startswith(_CID_PREFIX):
            continue
        if blocks.get(cid, data) != data:
            raise VerificationError("CAR has two different blocks under one CID")
        blocks[cid] = data
    return header["roots"], blocks


def _cid_bytes(cid: str) -> bytes:
    info = libipld.decode_cid(cid)
    h = info["hash"]
    if info["version"] != 1 or info["codec"] != 0x71 or h["code"] != 0x12 or len(h["digest"]) != 32:
        raise VerificationError("unexpected commit CID type (want CIDv1 dag-cbor sha256)")
    return _CID_PREFIX + h["digest"]


def _fetch_via_blocks(client, at: AtUri, ident, key: str) -> VerifiedRecord:
    """Verify ``key`` from the repo head (sync.getLatestCommit) by fetching
    the commit and each MST node on the path with sync.getBlocks."""
    with host_slot(ident.pds):
        try:
            latest = with_retry(
                lambda: client.com.atproto.sync.get_latest_commit({"did": at.did}), what="sync.getLatestCommit"
            )
        except BadRequestError as e:
            _raise_repo_status(at, e, ident.pds)
            raise
    try:
        commit_cid = _cid_bytes(str(latest.cid))
    except VerificationError:
        raise
    except Exception as e:  # noqa: BLE001 — not a CID at all
        raise VerificationError(f"malformed commit CID from getLatestCommit: {e}") from e
    cache: dict[bytes, bytes] = {}  # raw bytes; _block checks each hash
    fetches = 0

    def get_block(cid: bytes) -> bytes:
        nonlocal fetches
        if cid in cache:
            return cache[cid]
        fetches += 1
        if fetches > MAX_BLOCK_FETCHES:
            raise VerificationError("MST walk needs too many getBlocks calls")
        cid_str = libipld.encode_cid(cid)
        with host_slot(ident.pds):
            try:
                car = with_retry(
                    lambda: client.com.atproto.sync.get_blocks({"did": at.did, "cids": [cid_str]}),
                    what="sync.getBlocks",
                )
            except BadRequestError as e:
                _raise_repo_status(at, e, ident.pds)
                raise
        _, blocks = _car_blocks(car)
        if cid not in blocks:
            raise VerificationError("proof is incomplete: getBlocks did not return the requested block")
        cache[cid] = blocks[cid]
        return blocks[cid]

    try:
        commit = _block(get_block, commit_cid)
        rev = _check_commit(commit, did=at.did, signing_key=ident.signing_key)
        if rev != getattr(latest, "rev", None):
            # getLatestCommit names the head by CID and rev: a commit block
            # whose rev disagrees is not the head it claimed.
            raise VerificationError(f"commit rev {rev} differs from getLatestCommit's {getattr(latest, 'rev', None)!r}")
        record_cid = _mst_lookup(get_block, commit["data"], key.encode())
        if record_cid is None:
            return VerifiedRecord(str(at), False, None, None, rev, proven=True)
        value = _block(get_block, record_cid)
    except VerificationError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
        raise VerificationError(f"malformed repo data: {type(e).__name__}: {e}") from e
    if not isinstance(value, dict):
        raise VerificationError("record block is not an object")
    return VerifiedRecord(str(at), True, libipld.encode_cid(record_cid), value, rev, proven=True)


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
    roots, blocks = _car_blocks(car_bytes)
    if not roots:
        raise VerificationError("CAR has no root (the commit)")
    # The repository spec fixes only the first root: the commit.
    get_block = _dict_getter(blocks)
    commit = _block(get_block, roots[0])
    rev = _check_commit(commit, did=did, signing_key=signing_key)
    record_cid = _mst_lookup(get_block, commit["data"], key.encode())
    if record_cid is None:
        return False, None, None, rev
    value = _block(get_block, record_cid)
    if not isinstance(value, dict):
        raise VerificationError("record block is not an object")
    return True, libipld.encode_cid(record_cid), value, rev


def _check_commit(commit, *, did: str, signing_key: str) -> str:
    """Check a v3 commit's shape and signature. Returns its rev."""
    if not isinstance(commit, dict):
        raise VerificationError("commit block is not an object")
    if commit.get("did") != did:
        raise VerificationError(f"commit is for {commit.get('did')}, expected {did}")
    if commit.get("version") != 3:
        raise VerificationError(f"unsupported repo commit version {commit.get('version')}")
    # atproto.com/specs/repository: rev (a TID) and prev (nullable CID link)
    # are required fields of a v3 commit.
    if not is_tid(commit.get("rev")):
        raise VerificationError("commit rev is not a TID")
    if "prev" not in commit or not (commit["prev"] is None or isinstance(commit["prev"], bytes)):
        raise VerificationError("commit has no prev field (null or a CID)")
    sig = commit.get("sig")
    if not isinstance(sig, bytes):
        raise VerificationError("commit is unsigned")
    unsigned = {k: v for k, v in commit.items() if k != "sig"}
    if not isinstance(commit.get("data"), bytes):
        raise VerificationError("commit has no data root")
    if not verify_signature(signing_key, libipld.encode_dag_cbor(unsigned), sig):
        raise SignatureMismatch("commit signature does not match the DID's signing key")
    if rev_is_far_future(commit["rev"]):
        # A rev is a TID (a timestamp): a signed one from the future would
        # sort after every honest commit and defeat rollback detection.
        raise VerificationError(f"commit rev {commit['rev']} is more than five minutes in the future")
    return commit["rev"]


MAX_REV_SKEW_US = 5 * 60 * 1_000_000


def rev_is_far_future(rev: str, now_us: int | None = None) -> bool:
    """Is the TID ``rev``'s timestamp more than MAX_REV_SKEW_US ahead?"""
    import time

    now_us = time.time_ns() // 1000 if now_us is None else now_us
    return (decode_tid(rev) >> 10) > now_us + MAX_REV_SKEW_US


def _dict_getter(blocks: dict[bytes, bytes]) -> Callable[[bytes], bytes]:
    def get(cid: bytes) -> bytes:
        if cid not in blocks:
            raise VerificationError("proof is incomplete: referenced block missing from CAR")
        return blocks[cid]

    return get


def _block(get_block: Callable[[bytes], bytes], cid: bytes):
    """The decoded block ``cid``. The hash is checked over the bytes as
    received, BEFORE decoding (a CID binds the raw bytes; a decoder may
    accept non-canonical encodings that re-encode differently)."""
    if not isinstance(cid, bytes) or not cid.startswith(_CID_PREFIX) or len(cid) != 36:
        raise VerificationError("unexpected CID type (want CIDv1 dag-cbor sha256)")
    raw = get_block(cid)
    if not isinstance(raw, (bytes, bytearray)) or hashlib.sha256(raw).digest() != cid[4:]:
        raise VerificationError("block content does not match its CID")
    try:
        return libipld.decode_dag_cbor(bytes(raw))
    except Exception as e:  # noqa: BLE001
        raise VerificationError(f"malformed block: {e}") from e


def mst_key_layer(key: bytes) -> int:
    """The MST layer of ``key``: leading zero bits of SHA-256(key), counted
    in 2-bit chunks (fanout 4; atproto.com/specs/repository)."""
    zeros = 0
    for byte in hashlib.sha256(key).digest():
        if byte:
            zeros += 8 - byte.bit_length()
            break
        zeros += 8
    return zeros // 2


def _check_mst_key(key: bytes) -> None:
    """A repo path is '<valid NSID>/<valid record key>' (atproto.com/specs/repository)."""
    collection, sep, rkey = key.partition(b"/")
    try:
        c, r = collection.decode("ascii"), rkey.decode("ascii")
    except UnicodeDecodeError:
        c = r = ""
    if not sep or not is_nsid(c) or not is_record_key(r):
        raise VerificationError(f"malformed MST key {key[:80]!r} (want <NSID>/<record key>)")


def _mst_node(node, parent_layer: int | None) -> tuple[list, list[bytes], int | None]:
    """Decode and check one MST node. Returns (entries, full keys, layer).
    The repository spec says the depth and sort order of keys should be
    verified: keys strictly increasing and well-formed, every entry on the
    node's layer, and the layer exactly one below the parent's (empty
    intermediate nodes are kept, so sub-tree links never skip a layer). The
    root is never an empty node that only points to a sub-tree, and no
    other node is entirely empty."""
    if not isinstance(node, dict):
        raise VerificationError("MST node is not an object")
    entries = node.get("e")
    if not isinstance(entries, list):
        raise VerificationError("MST node has no entry list")
    if len(entries) > MAX_NODE_ENTRIES:
        raise VerificationError(f"MST node has more than {MAX_NODE_ENTRIES} entries")
    keys: list[bytes] = []
    prev, layer = b"", None
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise VerificationError("MST entry is not an object")
        p, k = entry.get("p"), entry.get("k")
        if isinstance(p, bool) or not isinstance(p, int) or not isinstance(k, bytes):
            raise VerificationError("MST entry has a malformed prefix length or key")
        if i == 0 and p != 0:
            raise VerificationError("first MST entry of a node must have prefix length 0")
        if not 0 <= p <= len(prev):
            raise VerificationError("MST entry prefix length out of range")
        full = prev[:p] + k
        if keys and full <= prev:
            raise VerificationError("MST keys are not strictly sorted")
        _check_mst_key(full)
        entry_layer = mst_key_layer(full)
        if layer is None:
            layer = entry_layer
        elif entry_layer != layer:
            raise VerificationError("MST node mixes keys of different layers")
        keys.append(full)
        prev = full
    if layer is None:
        # No entries: the root of an empty repo, or an intermediate node
        # one layer below its parent that leads to a sub-tree.
        if parent_layer is None:
            if node.get("l") is not None:
                raise VerificationError("MST root is an empty node that only points to a sub-tree")
            return entries, keys, None
        if node.get("l") is None:
            raise VerificationError("MST has an empty non-root node")
        return entries, keys, parent_layer - 1
    if parent_layer is not None and layer != parent_layer - 1:
        raise VerificationError("MST node is not exactly one layer below its parent's (a skipped layer)")
    return entries, keys, layer


def _mst_lookup(get_block: Callable[[bytes], bytes], root: bytes, key: bytes) -> bytes | None:
    """Walk the MST toward ``key``; return the record CID, or None if the walk
    proves the key absent. Every node on the path must be available and
    structurally valid (_mst_node), and every key in a sub-tree must lie
    strictly between the keys on either side of the link that leads to it:
    a malformed tree could otherwise make a present record look absent."""
    node_cid = root
    depth = 0
    parent_layer: int | None = None
    lo: bytes | None = None  # exclusive bounds of the current sub-tree
    hi: bytes | None = None
    while node_cid is not None:
        depth += 1
        if depth > 64:
            raise VerificationError("MST too deep")
        node = _block(get_block, node_cid)
        entries, keys, layer = _mst_node(node, parent_layer)
        if layer is not None and layer < 0:
            raise VerificationError("MST descends below layer 0")
        if keys and ((lo is not None and keys[0] <= lo) or (hi is not None and keys[-1] >= hi)):
            raise VerificationError("MST sub-tree holds a key outside its parent's range")
        subtree, next_lo, next_hi = node.get("l"), lo, (keys[0] if keys else hi)
        for i, (full, entry) in enumerate(zip(keys, entries, strict=True)):
            if full == key:
                return entry["v"]
            if key < full:
                break
            subtree, next_lo = entry.get("t"), full
            next_hi = keys[i + 1] if i + 1 < len(keys) else hi
        parent_layer, lo, hi = layer, next_lo, next_hi
        node_cid = subtree
    return None


def _xrpc_error(e: Exception) -> tuple[str, str]:
    content = getattr(getattr(e, "response", None), "content", None)
    return (getattr(content, "error", "") or "", getattr(content, "message", "") or str(e))
