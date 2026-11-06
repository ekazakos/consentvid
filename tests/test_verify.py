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
