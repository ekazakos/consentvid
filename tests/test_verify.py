import hashlib

import libipld
import pytest

from consentvid.errors import VerificationError
from consentvid.verify import verify_car

KEY = "org.consentvid.consent/{}"


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_car(header, blocks):
    h = libipld.encode_dag_cbor(header)
    out = _varint(len(h)) + h
    for cid, node in blocks.items():
        data = libipld.encode_dag_cbor(node)
        out += _varint(len(cid) + len(data)) + cid + data
    return out


def cid_of(node):
    return b"\x01\x71\x12\x20" + hashlib.sha256(libipld.encode_dag_cbor(node)).digest()


def test_present_record_verifies(cars):
    present, cid, value, rev = verify_car(
        cars["present"],
        did=cars["did"],
        signing_key=cars["signing_key"],
        key=KEY.format(cars["present_rkey"]),
    )
    assert present and cid == cars["present_cid"] and rev
    assert value["$type"] == "org.consentvid.consent" and value["authorshipAttested"] is True


def test_absent_record_is_proven(cars):
    present, cid, value, _ = verify_car(
        cars["absent"], did=cars["did"], signing_key=cars["signing_key"], key=KEY.format(cars["absent_rkey"])
    )
    assert not present and cid is None and value is None


def test_absence_proof_does_not_hide_other_keys(cars):
    # The present-record CAR proves the path to its own key; asking it about a
    # key elsewhere must not silently claim absence without a full path.
    try:
        present, *_ = verify_car(
            cars["present"],
            did=cars["did"],
            signing_key=cars["signing_key"],
            key=KEY.format(cars["absent_rkey"]),
        )
        assert not present
    except VerificationError as e:
        assert "incomplete" in str(e)


def test_wrong_did_rejected(cars):
    with pytest.raises(VerificationError, match="commit is for"):
        verify_car(
            cars["present"],
            did="did:plc:someoneelse",
            signing_key=cars["signing_key"],
            key=KEY.format(cars["present_rkey"]),
        )


def test_tampered_record_rejected(cars):
    header, blocks = libipld.decode_car(cars["present"])
    for cid, node in blocks.items():
        if isinstance(node, dict) and node.get("$type") == "org.consentvid.consent":
            blocks[cid] = {**node, "modelClasses": ["commercial"]}  # same CID, new content
    with pytest.raises(VerificationError, match="does not match its CID"):
        verify_car(
            encode_car(header, blocks),
            did=cars["did"],
            signing_key=cars["signing_key"],
            key=KEY.format(cars["present_rkey"]),
        )


def test_forged_commit_rejected(cars):
    header, blocks = libipld.decode_car(cars["present"])
    root = header["roots"][0]
    commit = dict(blocks.pop(root))
    commit["rev"] = "3zzzzzzzzzzzz"  # any change invalidates the signature
    new_root = cid_of(commit)
    blocks[new_root] = commit
    with pytest.raises(VerificationError, match="signature"):
        verify_car(
            encode_car({"version": 1, "roots": [new_root]}, blocks),
            did=cars["did"],
            signing_key=cars["signing_key"],
            key=KEY.format(cars["present_rkey"]),
        )


def test_wrong_key_rejected(cars):
    other = "did:key:zQ3shokFTS3brHcDQrn82RUDfCZESWL1ZdCEJwekUDPQiYBme"
    with pytest.raises(VerificationError, match="signature"):
        verify_car(cars["present"], did=cars["did"], signing_key=other, key=KEY.format(cars["present_rkey"]))


def test_garbage_rejected(cars):
    with pytest.raises(VerificationError):
        verify_car(b"not a car", did=cars["did"], signing_key=cars["signing_key"], key="a/b")


# --------------------------------------------- fetch_verified with fakes

from types import SimpleNamespace  # noqa: E402

from consentvid import identity  # noqa: E402
from consentvid import verify as vf  # noqa: E402
from consentvid.identity import Identity  # noqa: E402

OTHER_KEY = "did:key:zQ3shXjHeiBuRCKmM36cuYnm7YEMzhGnCmCyW92sRJ9pribSF"


def _http_error(status, error=None, headers=None):
    from atproto_client.exceptions import BadRequestError, RequestException
    from atproto_client.models.common import XrpcError
    from atproto_client.request import Response

    content = XrpcError(error=error, message=error or "x") if error else None
    resp = Response(success=False, status_code=status, content=content, headers=headers or {})
    return (BadRequestError if status == 400 else RequestException)(resp)


class _Sync:
    def __init__(self, car=None, get_record_error=None, blocks=None, root=None):
        self.car, self.get_record_error, self.blocks, self.root = car, get_record_error, blocks or {}, root
        self.calls = []

    def get_record(self, p):
        self.calls.append("getRecord")
        if self.get_record_error:
            raise self.get_record_error
        return self.car

    def get_latest_commit(self, p):
        self.calls.append("getLatestCommit")
        if getattr(self, "latest_error", None):
            raise self.latest_error
        root = self.blocks.get(self.root)
        rev = root.get("rev") if isinstance(root, dict) else "3maaaaaaaaaz2"
        return SimpleNamespace(cid=libipld.encode_cid(self.root), rev=getattr(self, "latest_rev", None) or rev)

    def get_blocks(self, p):
        self.calls.append("getBlocks")
        by_str = {libipld.encode_cid(k): k for k in self.blocks}
        found = {by_str[c]: self.blocks[by_str[c]] for c in p["cids"] if c in by_str}
        return encode_car({"version": 1, "roots": []}, found)  # real PDSes send no roots


@pytest.fixture
def pds(monkeypatch, cars):
    """resolve_identity/public_client fakes for the fixture repo."""
    state = SimpleNamespace(keys=[cars["signing_key"]], resolutions=0, sync=_Sync(cars["present"]))

    def resolve(did):
        key = state.keys[min(state.resolutions, len(state.keys) - 1)]
        state.resolutions += 1
        return Identity(did=did, pds="https://pds.example.com", signing_key=key)

    monkeypatch.setattr(vf, "resolve_identity", resolve)
    monkeypatch.setattr(vf, "client_for", lambda pds: SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(sync=state.sync))))
    monkeypatch.setattr(vf, "invalidate_identity", lambda did: None)
    monkeypatch.setattr(identity, "_sleep", lambda s: None)
    identity.reset_retry_budget()
    vf._refreshed.clear()
    vf._moved.clear()
    yield state
    vf._refreshed.clear()
    vf._moved.clear()


def _uri(cars, rkey=None):
    return f"at://{cars['did']}/org.consentvid.consent/{rkey or cars['present_rkey']}"


# R1-04: a signature mismatch re-resolves the identity once


def test_signature_mismatch_retries_once_with_a_fresh_identity(pds, cars):
    pds.keys = [OTHER_KEY, cars["signing_key"]]  # stale key first, rotated key second
    rec = vf.fetch_verified(_uri(cars))
    assert rec.present and rec.cid == cars["present_cid"] and pds.resolutions == 2


def test_second_mismatch_for_the_same_did_does_not_refresh_again(pds, cars):
    from consentvid.errors import SignatureMismatch

    pds.keys = [OTHER_KEY]
    with pytest.raises(SignatureMismatch):
        vf.fetch_verified(_uri(cars))
    n = pds.resolutions
    with pytest.raises(SignatureMismatch):
        vf.fetch_verified(_uri(cars))
    assert pds.resolutions == n + 1  # resolved for the read, not refreshed


# R1-13: getLatestCommit + getBlocks when sync.getRecord is broken


def _fallback_sync(cars, tamper=False):
    header, blocks = libipld.decode_car(cars["present"])
    root = header["roots"][0]
    if tamper:
        # Same CIDs, different content: every block must still hash to its CID.
        blocks = {k: ({**v, "rev": "tampered"} if k == root else v) for k, v in blocks.items()}
    return _Sync(get_record_error=_http_error(500, "InternalServerError"), blocks=blocks, root=root)


def test_broken_get_record_falls_back_to_get_blocks(pds, cars):
    pds.sync = _fallback_sync(cars)
    rec = vf.fetch_verified(_uri(cars))
    assert rec.present and rec.proven and rec.cid == cars["present_cid"]
    assert rec.value["$type"] == "org.consentvid.consent"
    assert pds.sync.calls[:2] == ["getRecord", "getLatestCommit"] and "getBlocks" in pds.sync.calls


def test_fallback_rejects_tampered_blocks(pds, cars):
    pds.sync = _fallback_sync(cars, tamper=True)
    with pytest.raises(VerificationError):
        vf.fetch_verified(_uri(cars))


def test_record_not_found_does_not_trigger_the_fallback(pds, cars):
    pds.sync = _Sync(get_record_error=_http_error(400, "RecordNotFound"))
    rec = vf.fetch_verified(_uri(cars))
    assert not rec.present and not rec.proven and pds.sync.calls == ["getRecord"]


# R1-14: rate limits and transient errors


def test_rate_limit_waits_for_the_reset_then_succeeds(monkeypatch):
    import time

    slept = []
    monkeypatch.setattr(identity, "_sleep", slept.append)
    identity.reset_retry_budget()
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, headers={"ratelimit-reset": str(time.time() + 2)})
        return "ok"

    assert identity.with_retry(fn) == "ok"
    assert len(slept) == 1 and 1.5 <= slept[0] <= 2.5


def test_rate_limit_budget_exhaustion_raises(monkeypatch):
    monkeypatch.setattr(identity, "_sleep", lambda s: None)
    monkeypatch.setattr(identity, "RETRY_BUDGET_S", 5.0)
    identity.reset_retry_budget()

    def fn():
        raise _http_error(429, headers={"retry-after": "30"})

    from atproto_client.exceptions import RequestException

    with pytest.raises(RequestException) as e:
        identity.with_retry(fn)
    assert identity.status_of(e.value) == 429


def test_too_large_is_never_retried(monkeypatch):
    slept = []
    monkeypatch.setattr(identity, "_sleep", slept.append)
    calls = []

    def fn():
        calls.append(1)
        raise identity.ResponseTooLarge("big")

    with pytest.raises(identity.ResponseTooLarge):
        identity.with_retry(fn)
    assert calls == [1] and slept == []


def test_server_errors_are_retried_twice_unless_told_not_to(monkeypatch):
    monkeypatch.setattr(identity, "_sleep", lambda s: None)
    identity.reset_retry_budget()
    calls = []

    def fn():
        calls.append(1)
        raise _http_error(503)

    from atproto_client.exceptions import RequestException

    with pytest.raises(RequestException):
        identity.with_retry(fn)
    assert len(calls) == 3
    calls.clear()
    with pytest.raises(RequestException):
        identity.with_retry(fn, retry_server_errors=False)
    assert len(calls) == 1


# R2-14: the getBlocks fallback maps repo-status errors like getRecord does


@pytest.mark.parametrize(
    "error, status, verdict",
    [("RepoDeactivated", "deactivated", "unavailable"), ("RepoTakendown", "takendown", "unavailable"),
     ("RepoNotFound", "deleted", "revoked")],
)
def test_fallback_maps_repo_status_errors(pds, cars, error, status, verdict):
    from consentvid import resync as rs
    from consentvid.errors import RepoUnavailable

    sync = _Sync(get_record_error=_http_error(500, "InternalServerError"))
    sync.latest_error = _http_error(400, error)
    pds.sync = sync
    with pytest.raises(RepoUnavailable) as ei:
        vf.fetch_verified(_uri(cars))
    assert ei.value.status == status
    item = rs.Item(id="x", name="x", consent_uri=_uri(cars), sha256="ab" * 32,
                   content_url="https://example.org/v", receipt={})
    assert rs.check(item, fetch=vf.fetch_verified, relay_status=False).status == verdict


# R2-13: MST structure and commit fields are checked


def _keys_by_layer(n_per_layer=3):
    from consentvid.records import _encode_tid

    out: dict[int, list[bytes]] = {}
    i = 0
    while len(out.get(0, [])) < n_per_layer or len(out.get(1, [])) < n_per_layer:
        k = f"org.consentvid.consent/{_encode_tid(1 << 40 | i)}".encode()
        out.setdefault(vf.mst_key_layer(k), []).append(k)
        i += 1
    return {layer: sorted(ks) for layer, ks in out.items()}


def _tree(*nodes):
    blocks = {cid_of(n): libipld.encode_dag_cbor(n) for n in nodes}
    return vf._dict_getter(blocks), cid_of(nodes[0])


REC = cid_of({"$type": "x"})


def test_unsorted_mst_node_is_rejected_not_taken_as_absence():
    """The auditor's PoC: unsorted keys made a present record look absent."""
    k1, k2 = _keys_by_layer()[0][:2]
    node = {"l": None, "e": [{"p": 0, "k": k2, "v": REC, "t": None}, {"p": 0, "k": k1, "v": REC, "t": None}]}
    get, root = _tree(node)
    with pytest.raises(VerificationError, match="sorted"):
        vf._mst_lookup(get, root, k1)
    good = {"l": None, "e": [{"p": 0, "k": k1, "v": REC, "t": None}, {"p": 0, "k": k2, "v": REC, "t": None}]}
    get, root = _tree(good)
    assert vf._mst_lookup(get, root, k1) == REC


def test_mst_layers_are_checked():
    layers = _keys_by_layer()
    a0, b1 = layers[0][0], layers[1][0]
    mixed = {"l": None, "e": [{"p": 0, "k": min(a0, b1), "v": REC, "t": None},
                              {"p": 0, "k": max(a0, b1), "v": REC, "t": None}]}
    get, root = _tree(mixed)
    with pytest.raises(VerificationError, match="different layers"):
        vf._mst_lookup(get, root, a0)
    child = {"l": None, "e": [{"p": 0, "k": layers[1][1], "v": REC, "t": None}]}  # layer 1 under layer 1
    parent = {"l": cid_of(child), "e": [{"p": 0, "k": max(layers[1][1:]) + b"~", "v": REC, "t": None}]}
    parent["e"][0]["k"] = layers[1][2]
    get, root = _tree(parent, child)
    with pytest.raises(VerificationError, match="below its parent"):
        vf._mst_lookup(get, root, layers[1][1])


@pytest.mark.parametrize("bad", [{"p": 1}, {"k": b"no-slash"}, {"p": True}])
def test_malformed_mst_entries_are_rejected(bad):
    k = _keys_by_layer()[0][0]
    entry = {"p": 0, "k": k, "v": REC, "t": None, **bad}
    get, root = _tree({"l": None, "e": [entry]})
    with pytest.raises(VerificationError):
        vf._mst_lookup(get, root, k)


@pytest.mark.parametrize("change", [{"rev": "not-a-tid"}, {"prev": "bafy-not-bytes"}, "no-prev"])
def test_commit_needs_a_tid_rev_and_a_prev_field(change):
    commit = {"did": "did:plc:x", "version": 3, "data": REC, "rev": "3maaaaaaaaaa2", "prev": None, "sig": b"s"}
    if change == "no-prev":
        del commit["prev"]
    else:
        commit.update(change)
    with pytest.raises(VerificationError, match="rev|prev"):
        vf._check_commit(commit, did="did:plc:x", signing_key=OTHER_KEY)


def test_car_with_extra_roots_uses_the_first(cars):
    header, blocks = libipld.decode_car(cars["present"])
    car = encode_car({"version": 1, "roots": [header["roots"][0], header["roots"][0]]}, blocks)
    present, *_ = verify_car(car, did=cars["did"], signing_key=cars["signing_key"],
                             key=KEY.format(cars["present_rkey"]))
    assert present


def _key_heights():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures/interop/mst_key_heights.json").read_text())


@pytest.mark.parametrize("vector", _key_heights(), ids=lambda v: v["key"] or "empty")
def test_mst_key_layer_matches_interop_vectors(vector):
    assert vf.mst_key_layer(vector["key"].encode()) == vector["height"]


# R3-10: MST shape rules from the repository spec


def _keys_at(layer, n=2, start=0):
    from consentvid.records import _encode_tid

    out, i = [], start
    while len(out) < n:
        k = f"org.consentvid.consent/{_encode_tid(1 << 40 | i)}".encode()
        if vf.mst_key_layer(k) == layer:
            out.append(k)
        i += 1
    return sorted(out)


def _leaf(*keys):
    return {"l": None, "e": [{"p": 0, "k": k, "v": REC, "t": None} for k in keys]}


def test_mst_skipped_layer_is_rejected_and_empty_intermediates_are_walked():
    (top,) = _keys_at(2, 1, start=500)
    low = [k for k in _keys_at(0, 6) if k < top][:1]
    assert low
    child = _leaf(*low)
    skipping = {"l": cid_of(child), "e": [{"p": 0, "k": top, "v": REC, "t": None}]}
    get, root = _tree(skipping, child)
    with pytest.raises(VerificationError, match="skipped layer"):
        vf._mst_lookup(get, root, low[0])
    # The same tree with the empty layer-1 node kept is fine.
    middle = {"l": cid_of(child), "e": []}
    proper = {"l": cid_of(middle), "e": [{"p": 0, "k": top, "v": REC, "t": None}]}
    get, root = _tree(proper, middle, child)
    assert vf._mst_lookup(get, root, low[0]) == REC


def test_mst_root_must_not_be_an_empty_pointer_node():
    child = _leaf(*_keys_at(0, 1))
    get, root = _tree({"l": cid_of(child), "e": []}, child)
    with pytest.raises(VerificationError, match="root is an empty node"):
        vf._mst_lookup(get, root, _keys_at(0, 1)[0])
    get, root = _tree({"l": None, "e": []})  # an empty repo is fine
    assert vf._mst_lookup(get, root, _keys_at(0, 1)[0]) is None


def test_mst_empty_non_root_leaf_is_rejected():
    (top,) = _keys_at(1, 1)
    empty = {"l": None, "e": []}
    get, root = _tree({"l": cid_of(empty), "e": [{"p": 0, "k": top, "v": REC, "t": None}]}, empty)
    with pytest.raises(VerificationError, match="empty non-root"):
        vf._mst_lookup(get, root, b"org.consentvid.consent/2222222222222")


def test_mst_child_keys_must_lie_between_their_parent_keys():
    (top,) = _keys_at(1, 1, start=200)
    zeros = _keys_at(0, 400)
    below, above = [k for k in zeros if k < top][0], [k for k in zeros if k > top][0]
    child = _leaf(below, above)  # 'above' does not belong left of 'top'
    get, root = _tree({"l": cid_of(child), "e": [{"p": 0, "k": top, "v": REC, "t": None}]}, child)
    with pytest.raises(VerificationError, match="outside its parent's range"):
        vf._mst_lookup(get, root, below)
    child = _leaf(below)
    get, root = _tree({"l": None, "e": [{"p": 0, "k": top, "v": REC, "t": cid_of(child)}]}, child)
    with pytest.raises(VerificationError, match="outside its parent's range"):
        vf._mst_lookup(get, root, b"org.consentvid.consent/zzzzzzzzzzzzz")


@pytest.mark.parametrize(
    "key", [b"Not An NSID/3maaaaaaaaaa2", b"consent/3maaaaaaaaaa2", b"org.consentvid.consent/..",
            b"org.consentvid.consent/bad key", b"org.consentvid.consent/" + b"a" * 513, b"org.consentvid.consent/\xff"],
)
def test_mst_paths_must_be_nsid_and_record_key(key):
    get, root = _tree(_leaf(key))
    with pytest.raises(VerificationError, match="malformed MST key"):
        vf._mst_lookup(get, root, key)


def test_record_key_syntax():
    from consentvid.records import is_record_key

    assert is_record_key("3maaaaaaaaaa2") and is_record_key("self") and is_record_key("a:b~c-d_e.f")
    for bad in ("", ".", "..", "a/b", "a b", "a" * 513, None):
        assert not is_record_key(bad), bad


def test_fallback_commit_must_match_get_latest_commit_rev(pds, cars):
    pds.sync = _fallback_sync(cars)
    pds.sync.latest_rev = "3zzzzzzzzzzzz"
    with pytest.raises(VerificationError, match="differs from getLatestCommit"):
        vf.fetch_verified(_uri(cars))


# R3-11: a CID binds the raw block bytes


def test_block_hash_is_checked_over_the_raw_bytes():
    raw = bytes([0xA1, 0x61, 0x61, 0x18, 0x01])  # {"a": 1} with a non-minimal integer
    assert libipld.encode_dag_cbor(libipld.decode_dag_cbor(raw)) != raw
    cid = b"\x01\x71\x12\x20" + hashlib.sha256(raw).digest()
    assert vf._block(vf._dict_getter({cid: raw}), cid) == {"a": 1}
    tampered = bytes([0xA1, 0x61, 0x61, 0x18, 0x02])
    with pytest.raises(VerificationError, match="does not match its CID"):
        vf._block(vf._dict_getter({cid: tampered}), cid)


def test_car_blocks_keep_raw_bytes_and_roots(cars):
    roots, blocks = vf._car_blocks(cars["present"])
    assert roots and all(isinstance(v, bytes) for v in blocks.values())
    for cid, raw in blocks.items():
        assert hashlib.sha256(raw).digest() == cid[4:]
    with pytest.raises(VerificationError, match="header"):
        vf._car_blocks(encode_car({"version": 2, "roots": []}, {}))


# R3-04: a repo-status error from a PDS the account may have left


def _migrated(monkeypatch, cars, old_sync, new_sync, pds_sequence):
    """resolve_identity answers pds_sequence[i] on the i-th call (then the
    last one); client_for(pds) talks to old_sync or new_sync."""
    state = SimpleNamespace(resolutions=0, invalidated=[])

    def resolve(did):
        pds = pds_sequence[min(state.resolutions, len(pds_sequence) - 1)]
        state.resolutions += 1
        return Identity(did=did, pds=pds, signing_key=cars["signing_key"])

    syncs = {"https://old.example": old_sync, "https://new.example": new_sync}
    monkeypatch.setattr(vf, "resolve_identity", resolve)
    monkeypatch.setattr(vf, "client_for", lambda pds: SimpleNamespace(com=SimpleNamespace(atproto=SimpleNamespace(sync=syncs[pds]))))
    monkeypatch.setattr(vf, "invalidate_identity", state.invalidated.append)
    return state


@pytest.mark.parametrize("error", ["RepoNotFound", "RepoDeactivated"])
def test_old_pds_after_a_migration_is_not_a_withdrawal(monkeypatch, cars, error):
    old = _Sync(get_record_error=_http_error(400, error))
    new = _Sync(cars["present"])
    state = _migrated(monkeypatch, cars, old, new, ["https://old.example", "https://new.example"])
    rec = vf.fetch_verified(_uri(cars))
    assert rec.present and rec.proven and rec.cid == cars["present_cid"]
    assert state.invalidated == [cars["did"]] and old.calls == ["getRecord"] and new.calls == ["getRecord"]


def test_migration_refresh_also_covers_the_get_blocks_fallback(monkeypatch, cars):
    old = _Sync(get_record_error=_http_error(500, "InternalServerError"))
    old.latest_error = _http_error(400, "RepoNotFound")
    new = _fallback_sync(cars)
    _migrated(monkeypatch, cars, old, new, ["https://old.example", "https://new.example"])
    rec = vf.fetch_verified(_uri(cars))
    assert rec.present and "getBlocks" in new.calls


def test_unchanged_pds_keeps_the_repo_status_after_one_reresolution(monkeypatch, cars):
    from consentvid import resync as rs
    from consentvid.errors import RepoUnavailable

    old = _Sync(get_record_error=_http_error(400, "RepoDeactivated"))
    state = _migrated(monkeypatch, cars, old, None, ["https://old.example"])
    with pytest.raises(RepoUnavailable) as ei:
        vf.fetch_verified(_uri(cars))
    assert ei.value.status == "deactivated" and ei.value.pds == "https://old.example"
    assert state.resolutions == 2 and state.invalidated == [cars["did"]]  # exactly one re-resolution
    # Once per DID and process: a second read does not re-resolve again.
    item = rs.Item(id="x", name="x", consent_uri=_uri(cars), sha256="ab" * 32,
                   content_url="https://example.org/v", receipt={})
    assert rs.check(item, fetch=vf.fetch_verified, relay_status=False).status == rs.UNAVAILABLE
    assert state.resolutions == 3 and state.invalidated == [cars["did"]]


def test_tombstoned_did_is_not_re_resolved(monkeypatch, cars):
    from consentvid.errors import RepoUnavailable

    calls = []

    def resolve(did):
        calls.append(did)
        raise RepoUnavailable(did, "deleted", "tombstoned")

    monkeypatch.setattr(vf, "resolve_identity", resolve)
    monkeypatch.setattr(vf, "invalidate_identity", lambda did: pytest.fail("re-resolved a tombstone"))
    with pytest.raises(RepoUnavailable):
        vf.fetch_verified(_uri(cars))
    assert len(calls) == 1
