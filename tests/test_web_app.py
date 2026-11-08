"""The consent web app (app/ -> web/app/) writes records that the Python
readers accept: the JS builder's outputs (tests/fixtures/web_app_vectors.json,
regenerated with `npm run vectors` in app/ and checked current by the JS
tests) are validated here with the Python validators and CID code."""

import json
from pathlib import Path

import pytest

from consentvid import versions
from consentvid.records import CONSENT_V2_NSID, VIDEO_V2_NSID, compute_cid
from consentvid.validate import validate_in_repo
from consentvid.vocab import SENSITIVE_SELF_LABELS

ROOT = Path(__file__).parent.parent
VECTORS = json.loads((ROOT / "tests/fixtures/web_app_vectors.json").read_text())


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_web_app_records_are_valid(vec):
    did, rkey = vec["did"], vec["rkey"]
    video, consent = vec["video"], vec["consent"]
    assert validate_in_repo(VIDEO_V2_NSID, video, did, rkey=rkey) == []
    assert validate_in_repo(CONSENT_V2_NSID, consent, did, rkey=rkey) == []
    # The consent pins the CID the PDS will compute for the video record.
    assert compute_cid(video) == vec["videoCid"] == consent["subject"]["cid"]
    assert consent["subject"]["uri"] == vec["videoUri"] == f"at://{did}/{VIDEO_V2_NSID}/{rkey}"


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_web_app_records_match_the_python_writer(vec):
    """Round trip through the internal view and the Python V2 writer gives
    the same records: the two writers agree field for field."""
    video, consent = vec["video"], vec["consent"]
    assert versions.video_record_v2(versions.video_view(VIDEO_V2_NSID, video)) == video
    assert versions.consent_record_v2(versions.consent_view(CONSENT_V2_NSID, consent)) == consent


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_web_app_apply_writes_body(vec):
    body = vec["body"]
    assert body["repo"] == vec["did"]
    (v, c) = body["writes"]
    assert v == {"$type": "com.atproto.repo.applyWrites#create", "collection": VIDEO_V2_NSID,
                 "rkey": vec["rkey"], "value": vec["video"]}
    assert c == {"$type": "com.atproto.repo.applyWrites#create", "collection": CONSENT_V2_NSID,
                 "rkey": vec["rkey"], "value": vec["consent"]}
    assert [w["collection"] for w in vec["withdraw"]["writes"]] == [CONSENT_V2_NSID, VIDEO_V2_NSID]
    assert all(w["$type"].endswith("#delete") and w["rkey"] == vec["rkey"] for w in vec["withdraw"]["writes"])


def test_web_app_vocabulary_comes_from_the_lexicons():
    """The app reads its vocabulary from the same lexicon files (records.js
    imports them); its sensitive-label list must match vocab.py."""
    src = (ROOT / "app/src/records.js").read_text()
    assert "src/consentvid/schemas/org.consentvid.consentV2.json" in src
    listed = json.dumps(list(SENSITIVE_SELF_LABELS)).replace('"', "'")
    assert f"export const SENSITIVE_SELF_LABELS = {listed}" in src


def test_everything_vector_carries_the_optional_fields():
    vec = next(v for v in VECTORS if v["name"] == "everything")
    video = vec["video"]
    assert video["title"] == "Pasta night"  # control/format characters dropped
    assert video["tags"] == ["cooking", "food"] and video["labels"]["values"] == [{"val": "nudity"}]
    assert video["langs"] == ["en", "cs"] and video["size"] == 12262169
    assert vec["consent"]["expiresAt"] == "2028-11-08T06:20:00.000Z"


def test_client_metadata_is_served_with_the_site():
    meta = json.loads((ROOT / "web/app/client-metadata.json").read_text())
    assert meta["client_id"] == "https://consentvid.org/app/client-metadata.json"
    assert "atproto" in meta["scope"].split()
    for nsid in (VIDEO_V2_NSID, CONSENT_V2_NSID):
        assert f"repo:{nsid}?action=create&action=delete" in meta["scope"].split()
