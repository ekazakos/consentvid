import json
import logging

import pytest

from conftest import ALICE, VIDEO_CID, consent_record, video_record
from consentvid.croissant import LICENSE_URL, Clip, build_manifest
from consentvid.policy import covers, latest_per_video
from consentvid.storage import public_url


def clip(storage=None, **ckw):
    v = video_record(mimeType="video/mp4", **({"storageRef": storage} if storage else {}))
    return Clip(
        f"at://{ALICE}/org.consentvid.consent/c1",
        "bafyc",
        consent_record(**ckw),
        f"at://{ALICE}/org.consentvid.video/v1",
        VIDEO_CID,
        v,
        ALICE,
        "alice.test",
    )


def test_manifest_shape():
    m, skipped = build_manifest([clip(), clip({"kind": "local", "value": "x.mov"})], name="t")
    assert len(m["distribution"]) == 1 and len(skipped) == 1
    fo = m["distribution"][0]
    assert m["license"] == LICENSE_URL and m["isLiveDataset"] is True
    assert fo["encodingFormat"] == "video/mp4" and fo["sha256"] == "ab" * 32
    assert fo["cv:consentReceipt"]["cid"] == "bafyc" and fo["cv:videoCid"] == VIDEO_CID
    assert not any(k.startswith("cr:consent") for k in fo)


def test_manifest_validates_as_croissant(tmp_path):
    mlc = pytest.importorskip("mlcroissant")
    logging.disable(logging.WARNING)
    m, _ = build_manifest([clip()], name="test-dataset")
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    mlc.Dataset(jsonld=str(p))


def test_policy_all_of_and_latest():
    c = consent_record(purposes=["training", "evaluation"])
    assert covers(c, purposes=["training"]) and not covers(c, purposes=["training", "benchmarking"])
    best, superseded = latest_per_video(
        [
            ("old", consent_record(createdAt="2026-01-01T00:00:00Z")),
            ("new", consent_record(createdAt="2026-02-01T00:00:00Z")),
        ]
    )
    assert list(best.values())[0][0] == "new" and superseded == ["old"]


def test_public_url(monkeypatch):
    monkeypatch.setenv("CONSENTVID_IPFS_GATEWAY", "https://gw.example/")
    assert public_url({"kind": "ipfs", "value": "ipfs://bafyX"}) == "https://gw.example/ipfs/bafyX"
    assert public_url({"kind": "local", "value": "x.mov"}) is None
    assert public_url({"kind": "r2", "value": "https://r2/x"}) == "https://r2/x"
