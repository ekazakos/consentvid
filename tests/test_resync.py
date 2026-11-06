import pytest

from conftest import ALICE, BOB, HASH, VIDEO_CID, consent_record, video_record
from consentvid import resync as rs
from consentvid.errors import RepoUnavailable, VerificationError
from consentvid.verify import VerifiedRecord

CURI = f"at://{ALICE}/org.consentvid.consent/c1"
VURI = f"at://{ALICE}/org.consentvid.video/v1"
CCID = "bafyconsent"


def fetcher(consent=None, video=None, consent_cid=CCID, video_cid=VIDEO_CID, raise_for=None):
    consent = consent_record() if consent is None else consent
    video = video_record() if video is None else video

    def fetch(at):
        uri = str(at)
        if raise_for and uri in raise_for:
            raise raise_for[uri]
        if uri == CURI:
            return VerifiedRecord(uri, consent is not False, consent_cid, consent or None, "r", True)
        if uri == VURI:
            return VerifiedRecord(uri, video is not False, video_cid, video or None, "r", True)
        raise AssertionError(uri)

    return fetch


def item(consent=None, **kw):
    """A manifest item exactly as a fresh export of ``consent`` would produce it."""
    c = consent if isinstance(consent, dict) else consent_record()
    base = dict(
        id="x",
        name="clip",
        consent_uri=CURI,
        consent_cid=CCID,
        sha256=HASH[7:],
        terms=rs._terms(c),
        video_uri=VURI,
        video_cid=VIDEO_CID,
        content_url="https://example.org/videos/x",
    )
    base.update(kw)
    return rs.Item(**base)


@pytest.mark.parametrize(
    "fetch_kw,item_kw,status",
    [
        ({}, {}, rs.VALID),
        ({"consent": False}, {}, rs.REVOKED),
        ({"video": False}, {}, rs.REVOKED),
        ({"consent_cid": "bafyother"}, {}, rs.MODIFIED),
        ({"video_cid": "bafyother"}, {}, rs.MODIFIED),
        ({}, {"sha256": "cd" * 32}, rs.MODIFIED),
        ({"consent": consent_record(authorshipAttested=False)}, {}, rs.INVALID),
        ({"consent": consent_record(did=BOB)}, {}, rs.INVALID),  # someone else's video
        ({"consent": consent_record(expiresAt="2026-01-02T00:00:00Z")}, {}, rs.EXPIRED),
        ({}, {"expires_at": "2000-01-01T00:00:00Z"}, rs.EXPIRED),
        ({}, {"consent_uri": "at://alice.test/org.consentvid.consent/c1"}, rs.INVALID),
        ({"raise_for": {CURI: RepoUnavailable(ALICE, "deleted")}}, {}, rs.REVOKED),
        ({"raise_for": {CURI: RepoUnavailable(ALICE, "deactivated")}}, {}, rs.UNAVAILABLE),
        ({"raise_for": {CURI: VerificationError("bad sig")}}, {}, rs.INVALID),
        ({"raise_for": {CURI: OSError("down")}}, {}, rs.UNKNOWN),
        ({"raise_for": {VURI: OSError("down")}}, {}, rs.UNKNOWN),
    ],
)
def test_statuses(fetch_kw, item_kw, status):
    it = item(fetch_kw.get("consent"), **item_kw)
    assert rs.check(it, fetch=fetcher(**fetch_kw)).status == status


def test_min_provenance():
    f = fetcher()
    assert rs.check(item(), fetch=f, min_provenance="selfAttested").status == rs.VALID
    assert rs.check(item(), fetch=f, min_provenance="attested").status == rs.UNATTESTED


def test_legacy_manifest_without_cids_still_checked():
    legacy = consent_record()
    del legacy["videoCid"]
    r = rs.check(
        item(legacy, consent_cid=None, video_cid=None),
        fetch=fetcher(consent=legacy, video_cid="bafyanything"),
    )
    assert r.status == rs.VALID and "legacy" in r.reason


def test_filter_manifest_and_legacy_keys():
    manifest = {
        "distribution": [
            {"@type": "cr:FileObject", "@id": "a", "cr:consentReceipt": {"atUri": CURI}},  # old key
            {"@type": "cr:FileObject", "@id": "b", "cv:consentReceipt": {"atUri": CURI, "cid": "x"}},
        ]
    }
    items = rs.items_from_manifest(manifest)
    assert [i.consent_uri for i in items] == [CURI, CURI]
    results = [rs.Result("a", "a", CURI, rs.UNKNOWN), rs.Result("b", "b", CURI, rs.MODIFIED)]
    # strict is the default: unverifiable consent is dropped; lenient keeps it
    assert rs.filter_manifest(manifest, results, "now")["distribution"] == []
    assert [f["@id"] for f in rs.filter_manifest(manifest, results, "now", strict=False)["distribution"]] == ["a"]


@pytest.mark.parametrize(
    "edit,status",
    [
        (
            dict(terms={**rs._terms(consent_record()), "modelClasses": ["academic", "commercial"]}),
            rs.MODIFIED,
        ),
        (dict(terms={**rs._terms(consent_record()), "purposes": ["training", "evaluation"]}), rs.MODIFIED),
        (dict(terms={**rs._terms(consent_record()), "expiresAt": "2999-01-01T00:00:00Z"}), rs.MODIFIED),
        (dict(consent_cid=None, terms={**rs._terms(consent_record()), "modalities": ["face"]}), rs.MODIFIED),
        (dict(sha256=None), rs.INVALID),
        (dict(sha256=""), rs.INVALID),
        (dict(content_url="https://evil.example/other.mp4"), rs.MODIFIED),
        (dict(video_uri=f"at://{ALICE}/org.consentvid.video/other"), rs.MODIFIED),
    ],
)
def test_edited_manifest_never_passes(edit, status):
    """An edited manifest must not widen what a clip permits (review finding 1)."""
    assert rs.check(item(**edit), fetch=fetcher()).status == status


def test_superseded_by_newer_consent():
    newer = consent_record(createdAt="2026-06-01T00:00:00Z", purposes=["evaluation"])
    listing = lambda did: [(CURI, consent_record()), (CURI + "x", newer)]  # noqa: E731
    r = rs.check(item(), fetch=fetcher(), list_consents=listing)
    assert r.status == rs.MODIFIED and "superseded" in r.reason
    invalid_newer = lambda did: [(CURI + "x", {**newer, "authorshipAttested": False})]  # noqa: E731
    assert rs.check(item(), fetch=fetcher(), list_consents=invalid_newer).status == rs.VALID


def test_malformed_video_record_is_invalid_not_a_crash():
    bad = video_record(contentHash=123)
    assert rs.check(item(), fetch=fetcher(video=bad)).status == rs.INVALID


def test_one_bad_clip_does_not_abort_the_run(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "check", explode)
    results = rs.check_all([item(), item(id="y")], list_consents=lambda d: [])
    assert [r.status for r in results] == [rs.UNKNOWN, rs.UNKNOWN]


def test_c2pa_states():
    assert rs.check(item(c2pa_state="Invalid"), fetch=fetcher()).status == rs.INVALID
    r = rs.check(item(c2pa_state="Valid"), fetch=fetcher())
    assert r.status == rs.VALID and "untrusted" in r.reason
