import copy
import json

import pytest
from atproto_client.exceptions import BadRequestError
from click.testing import CliRunner

from conftest import ALICE, BOB, HASH, K1, K2, VIDEO_CID, consent_record, video_record
from consentvid import resync as rs
from consentvid.croissant import CROISSANT_CONTEXT, LEGACY_CONTEXTS, Clip, _file_object, build_manifest
from consentvid.errors import ConsentVidError, RepoUnavailable, VerificationError
from consentvid.verify import VerifiedRecord

CURI = f"at://{ALICE}/org.consentvid.consent/{K1}"
VURI = f"at://{ALICE}/org.consentvid.video/{K1}"
CCID = "bafyconsent"
URL = "https://example.org/videos/x"


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


def clip(consent=None, video=None):
    return Clip(
        CURI,
        CCID,
        consent if isinstance(consent, dict) else consent_record(),
        VURI,
        VIDEO_CID,
        video if isinstance(video, dict) else video_record(),
        ALICE,
    )


def file_object(consent=None, video=None):
    """A distribution entry exactly as a fresh export produces it."""
    return _file_object(clip(consent, video))


def manifest(*entries):
    m, _ = build_manifest([clip()], name="t")
    m["distribution"] = list(entries) if entries else m["distribution"]
    return m


def item(consent=None, **kw):
    """The Item a fresh export of ``consent`` yields, with fields overridden."""
    (it,) = rs.items_from_manifest(manifest(file_object(consent)))
    receipt_edits = kw.pop("receipt_edits", None)
    if receipt_edits:
        it.receipt = {**it.receipt, **receipt_edits}
        it.expires_at = it.receipt.get("expiresAt")
    for k, v in kw.items():
        setattr(it, k, v)
    return it


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
        # R1-04: a PDS 400 or a malformed answer is never "couldn't check"
        ({"raise_for": {CURI: BadRequestError()}}, {}, rs.INVALID),
        ({"raise_for": {VURI: BadRequestError()}}, {}, rs.INVALID),
        ({"raise_for": {CURI: ValueError("bad")}}, {}, rs.INVALID),
        ({"raise_for": {CURI: TypeError("bad")}}, {}, rs.INVALID),
    ],
)
def test_statuses(fetch_kw, item_kw, status):
    consent = fetch_kw.get("consent")
    it = item(consent if isinstance(consent, dict) else None, **item_kw)
    assert rs.check(it, fetch=fetcher(**fetch_kw)).status == status


def test_min_provenance():
    f = fetcher()
    assert rs.check(item(), fetch=f, min_provenance="selfAttested").status == rs.VALID
    assert rs.check(item(), fetch=f, min_provenance="attested").status == rs.UNATTESTED


def test_attested_provenance_is_not_trusted():
    """R3-05: a signed 'attested' is self-declared; nothing verifies it."""
    attested = consent_record(captureProvenance="attested")
    f = fetcher(consent=attested)
    r = rs.check(item(attested), fetch=f, min_provenance="attested")
    assert r.status == rs.UNATTESTED and "self-declared" in r.reason
    assert rs.check(item(attested), fetch=f, min_provenance="selfAttested").status == rs.VALID
    assert rs.check(item(attested), fetch=f).status == rs.VALID


def test_legacy_manifest_without_cids_still_checked():
    # R3-03: a consent that does not pin its video version is invalid ...
    legacy = consent_record()
    del legacy["videoCid"]
    r = rs.check(
        item(legacy, consent_cid=None, video_cid=None),
        fetch=fetcher(consent=legacy, video_cid="bafyanything"),
    )
    assert r.status == rs.INVALID and "videoCid is required" in r.reason
    # ... while a manifest that does not pin the consent CID is still checked.
    r = rs.check(item(consent_cid=None, video_cid=None), fetch=fetcher())
    assert r.status == rs.VALID and "legacy" not in r.reason
    assert "does not pin" in r.reason  # R1-17


def test_filter_manifest_and_legacy_keys():
    m = manifest(
        {"@type": "cr:FileObject", "@id": "a", "cr:atUri": VURI, "cr:consentReceipt": {"atUri": CURI}},  # old keys
        {"@type": "cr:FileObject", "@id": "b", "cr:atUri": VURI, "cr:consentReceipt": {"atUri": CURI, "cid": "x"}},
    )
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    items = rs.items_from_manifest(m)
    assert [i.consent_uri for i in items] == [CURI, CURI]
    results = [rs.Result("a", "a", CURI, rs.UNKNOWN), rs.Result("b", "b", CURI, rs.MODIFIED)]
    # strict is the default: unverifiable consent is dropped; lenient keeps it
    assert rs.filter_manifest(m, results, "now")["distribution"] == []
    assert [f["@id"] for f in rs.filter_manifest(m, results, "now", strict=False)["distribution"]] == ["a"]


def test_legacy_context_accepted():
    assert len(rs.items_from_manifest(legacy_manifest())) == 1


# ------------------------------------------------- R1-01: every entry counts


def test_every_distribution_entry_is_checked_and_only_exact_fileobjects_kept():
    good = file_object()
    variants = []
    for t in ("http://mlcommons.org/croissant/FileObject", ["cr:FileObject"], "cr:FileSet"):
        fo = {**file_object(), "@type": t, "@id": f"{ALICE}/other-{len(variants)}"}
        variants.append(fo)
    m = manifest(good, *variants)
    items = rs.items_from_manifest(m)
    results = rs.check_all(items, fetch=fetcher())
    assert len(results) == 4
    assert results[0].status == rs.VALID
    assert [r.status for r in results[1:]] == [rs.INVALID] * 3
    out = rs.filter_manifest(m, results, "now")
    assert [f["@id"] for f in out["distribution"]] == [good["@id"]]
    assert "3 clip(s) dropped, 1 remain" in out["rai:dataReleaseMaintenancePlan"]


@pytest.mark.parametrize("entry", ["a string", 3, None, ["cr:FileObject"]])
def test_non_dict_entries_are_invalid_results(entry):
    items = rs.items_from_manifest(manifest(file_object(), entry))
    assert len(items) == 2
    assert rs.check(items[1], fetch=fetcher()).status == rs.INVALID


def test_duplicate_ids_drop_both():
    valid, revoked = file_object(), file_object()
    m = manifest(valid, revoked)
    items = rs.items_from_manifest(m)
    assert all(i.problem and "duplicate" in i.problem for i in items)
    # Even if a checker had produced VALID for one and REVOKED for the other:
    results = [rs.Result(valid["@id"], "", CURI, rs.VALID), rs.Result(valid["@id"], "", CURI, rs.REVOKED)]
    assert rs.filter_manifest(m, results, "now")["distribution"] == []


@pytest.mark.parametrize(
    "edit",
    [
        {"sc:distribution": [{"@type": "cr:FileObject"}]},
        {"https://schema.org/distribution": []},
        {"@graph": []},
        {"@included": []},
        {"recordSet": [{"@type": "cr:RecordSet"}]},
        {"rai:dataCollection": {"@type": "cr:FileObject"}},
        {"distribution": {"@type": "cr:FileObject"}},
    ],
)
def test_side_channels_for_extra_fileobjects_rejected(edit):
    m = {**manifest(file_object()), **edit}
    with pytest.raises(ConsentVidError):
        rs.items_from_manifest(m)


def test_modified_context_rejected():
    m = manifest(file_object())
    m["@context"] = {**CROISSANT_CONTEXT, "sha256": "cr:md5"}
    with pytest.raises(ConsentVidError, match="@context"):
        rs.items_from_manifest(m)
    del m["@context"]
    with pytest.raises(ConsentVidError):
        rs.items_from_manifest(m)


# --------------------------------------- R1-02: aliases, contentUrl, rebuild


@pytest.mark.parametrize(
    "extra",
    [
        {"md5": "0" * 32},
        {"sc:contentUrl": "https://evil.example/u.mp4"},
        {"https://schema.org/contentUrl": "https://evil.example/u.mp4"},
        {"cr:md5": "0" * 32},
        {"sameAs": "https://evil.example/u.mp4"},
        {"containedIn": {"@id": "x"}},
        {"cr:consentReceipt": {"atUri": CURI}},  # both receipt keys
        {"cr:atUri": VURI},  # both video keys
    ],
)
def test_unexpected_fileobject_fields_never_pass(extra):
    m = manifest({**file_object(), **extra})
    if set(extra) & {"cr:atUri", "cr:consentReceipt"}:
        # R3-03: legacy keys in a current-context manifest refuse the whole manifest
        with pytest.raises(ConsentVidError, match="legacy-format"):
            rs.items_from_manifest(m)
        return
    (it,) = rs.items_from_manifest(m)
    assert rs.check(it, fetch=fetcher()).status in (rs.MODIFIED, rs.INVALID)


@pytest.mark.parametrize("key", ["contentUrl", "sha256", "@id", "name", "cv:videoCid"])
def test_fileobject_fields_must_be_strings(key):
    (it,) = rs.items_from_manifest(manifest({**file_object(), key: {"@id": "x"}}))
    assert rs.check(it, fetch=fetcher()).status == rs.INVALID


def test_missing_content_url_is_invalid():
    fo = file_object()
    del fo["contentUrl"]
    (it,) = rs.items_from_manifest(manifest(fo))
    r = rs.check(it, fetch=fetcher())
    assert r.status == rs.INVALID and "contentUrl" in r.reason


CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"


@pytest.mark.parametrize(
    "url,ok",
    [
        (f"https://ipfs.io/ipfs/{CID}", True),
        (f"https://evil.example/x/ipfs/{CID}", False),
        (f"https://evil.example/u.mp4?a=/ipfs/{CID}", False),
        (f"https://evil.example/u.mp4#/ipfs/{CID}", False),
        (f"file:///ipfs/{CID}", False),
        (f"http://ipfs.io/ipfs/{CID}", False),
        (f"https://user@ipfs.io/ipfs/{CID}", False),
        (f"https://ipfs.io/ipfs/{CID}/", False),
    ],
)
def test_ipfs_url_matching(url, ok):
    assert rs._url_matches(url, {"kind": "ipfs", "value": f"ipfs://{CID}"}) is ok


def test_ipfs_url_matching_needs_a_real_cid():
    assert not rs._url_matches("https://ipfs.io/ipfs/../x", {"kind": "ipfs", "value": "../x"})


def test_write_rebuilds_entries_from_verified_records():
    fo = {**file_object(), "name": "a nicer title", "description": "use freely for anything"}
    fo["cr:atUri"] = fo.pop("cv:atUri")
    fo["cr:consentReceipt"] = fo.pop("cv:consentReceipt")
    m = manifest(fo)
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    items = rs.items_from_manifest(m)
    results = rs.check_all(items, fetch=fetcher())
    assert results[0].status == rs.VALID
    out = rs.filter_manifest(m, results, "now")
    assert out["distribution"] == [file_object()]
    assert out["@context"] == CROISSANT_CONTEXT
    assert "_verified" not in results[0].as_dict()
    json.dumps([r.as_dict() for r in results])  # serialisable


# -------------------------------------------- R1-03: every receipt field


_RECEIPT_EDITS = [
    {"peoplePresent": "noIdentifiablePeople"},
    {"captureProvenance": "attested"},
    {"compensation": "paid"},
    {"rightsBasis": "x"},
    {"adultAttested": False},
    {"videoCid": "bafyother"},
    {"foo": "bar"},
    {"createdAt": "2020-01-01T00:00:00Z"},
    {"createdAt": "not a date"},
    {"expiresAt": "2999-01-01T00:00:00Z"},
    {"expiresAt": 12345},
    {"modelClasses": ["academic", "commercial"]},
    {"purposes": ["training", "evaluation"]},
    {"modalities": ["face"]},
    {"purposes": "training"},
    {"revocable": "yes"},
]


@pytest.mark.parametrize("pin", [True, False])
@pytest.mark.parametrize("receipt_edits", _RECEIPT_EDITS)
def test_edited_receipt_never_passes(receipt_edits, pin):
    kw = {} if pin else {"consent_cid": None}
    r = rs.check(item(receipt_edits=receipt_edits, **kw), fetch=fetcher())
    assert r.status in (rs.MODIFIED, rs.INVALID), (receipt_edits, r)


@pytest.mark.parametrize(
    "edit,status",
    [
        (dict(sha256=None), rs.INVALID),
        (dict(sha256=""), rs.INVALID),
        (dict(content_url="https://evil.example/other.mp4"), rs.MODIFIED),
        (dict(video_uri=f"at://{ALICE}/org.consentvid.video/other"), rs.MODIFIED),
        (dict(contributor=BOB), rs.MODIFIED),
        (dict(storage_kind="ipfs"), rs.MODIFIED),
    ],
)
def test_edited_manifest_never_passes(edit, status):
    """An edited manifest must not widen what a clip permits (review finding 1)."""
    assert rs.check(item(**edit), fetch=fetcher()).status == status


def test_list_item_in_terms_is_invalid_not_unknown():
    it = item(receipt_edits={"purposes": ["training", ["x"]]})
    (r,) = rs.check_all([it], fetch=fetcher())
    assert r.status == rs.INVALID


def test_legacy_receipt_without_new_fields_still_valid():
    fo = file_object()
    receipt = fo.pop("cv:consentReceipt")
    for k in ("peoplePresent", "adultAttested", "videoCid", "cid"):
        receipt.pop(k)
    fo["cr:consentReceipt"] = receipt
    fo["cr:atUri"] = fo.pop("cv:atUri")
    m = manifest(fo)
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    (it,) = rs.items_from_manifest(m)
    assert rs.check(it, fetch=fetcher()).status == rs.VALID


# ------------------------------------------- R1-01: the sidecar consent


def test_consent_not_keyed_to_its_video_is_invalid():
    """A consent under any key but its video's is ignored: a manifest that
    names one is INVALID (proven from the signed record, no listing)."""
    other = f"at://{ALICE}/org.consentvid.consent/{K2}"

    def fetch(at):
        if str(at) == other:
            return VerifiedRecord(other, True, CCID, consent_record(), "r", True)
        return fetcher()(at)

    r = rs.check(item(consent_uri=other), fetch=fetch)
    assert r.status == rs.INVALID and "sidecar" in r.reason


def test_rewritten_consent_is_modified_via_its_cid():
    r = rs.check(item(), fetch=fetcher(consent_cid="bafyrewritten"))
    assert r.status == rs.MODIFIED and "changed since export" in r.reason


def test_check_never_lists_the_repo():
    """Supersession is gone: the verdict rests only on MST-proven reads."""
    import inspect

    assert "list_consents" not in inspect.signature(rs.check).parameters
    assert "list_consents" not in inspect.signature(rs.check_all).parameters


# ------------------------------------------------------------------ errors


def test_malformed_video_record_is_invalid_not_a_crash():
    bad = video_record(contentHash=123)
    assert rs.check(item(), fetch=fetcher(video=bad)).status == rs.INVALID


def test_bad_storage_ref_in_signed_video_is_invalid():
    bad = video_record(storageRef={"kind": "url", "value": "file:///etc/passwd"})
    assert rs.check(item(), fetch=fetcher(video=bad)).status == rs.INVALID


def test_tls_failure_is_invalid_not_unknown():
    import ssl

    import httpx
    from atproto_client.exceptions import NetworkError

    def wrapped(inner):
        try:
            try:
                raise inner
            except Exception as e:
                raise httpx.ConnectError("connect failed") from e
        except httpx.ConnectError as e:
            try:
                raise NetworkError() from e
            except NetworkError as outer:
                return outer

    tls = wrapped(ssl.SSLCertVerificationError("certificate verify failed"))
    down = wrapped(ConnectionRefusedError("refused"))
    assert rs.check(item(), fetch=fetcher(raise_for={CURI: tls})).status == rs.INVALID
    assert rs.check(item(), fetch=fetcher(raise_for={CURI: down})).status == rs.UNKNOWN


def test_one_bad_clip_does_not_abort_the_run(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "check", explode)
    results = rs.check_all([item(), item(id="y")])
    # Fail closed: an internal error is never "network, try again" (R1-04)
    assert [r.status for r in results] == [rs.INVALID, rs.INVALID]


# -------------------------------------------------------------------- C2PA


def c2pa_receipt(consent=None, **kw):
    c = consent or consent_record()
    r = {
        "consentUri": CURI,
        "consentCid": CCID,
        "videoUri": VURI,
        "videoCid": VIDEO_CID,
        "contentHash": HASH,
        "contributor": {"handle": "alice.test", "did": ALICE},
        "purposes": c["purposes"],
        "modelClasses": c["modelClasses"],
        "modalities": c["modalities"],
        "compensation": c["compensation"],
        "revocable": c["revocable"],
        "authorshipAttested": c["authorshipAttested"],
        "rightsBasis": c["rightsBasis"],
        "captureProvenance": c["captureProvenance"],
        "peoplePresent": c["peoplePresent"],
        "adultAttested": c["adultAttested"],
        "expiresAt": c.get("expiresAt"),
        "createdAt": c["createdAt"],
        "revocationNote": "...",
    }
    r.update(kw)
    return r


@pytest.mark.parametrize("state", ["Valid", "Trusted"])
def test_c2pa_file_is_never_valid(state):
    """R1-05: a C2PA signer is not the DID key and the stamped bytes differ."""
    r = rs.check(rs.item_from_c2pa(c2pa_receipt(), "f.mp4", state), fetch=fetcher())
    assert r.status == rs.UNBOUND and r.verified is False
    assert rs.UNBOUND in rs.DROP_STATUSES
    assert "sha256-" + HASH[7:] in r.reason


def test_c2pa_states():
    assert rs.check(rs.item_from_c2pa(c2pa_receipt(), "f", "Invalid"), fetch=fetcher()).status == rs.INVALID
    revoked = rs.check(rs.item_from_c2pa(c2pa_receipt(), "f", "Valid"), fetch=fetcher(consent=False))
    assert revoked.status == rs.REVOKED
    widened = rs.item_from_c2pa(c2pa_receipt(modelClasses=["commercial"]), "f", "Trusted")
    assert rs.check(widened, fetch=fetcher()).status == rs.MODIFIED
    extra = rs.item_from_c2pa(c2pa_receipt(md5="x"), "f", "Trusted")
    assert rs.check(extra, fetch=fetcher()).status == rs.INVALID


def test_cli_resync_c2pa_file_fails_check(monkeypatch, tmp_path):
    from consentvid import c2pa_embed as ce
    from consentvid import cli

    monkeypatch.setattr(ce, "extract_consent_receipt", lambda p: (c2pa_receipt(), "Trusted"))
    monkeypatch.setattr(rs, "fetch_verified", fetcher())
    real_check = rs.check
    monkeypatch.setattr(rs, "check", lambda it, **kw: real_check(it, **{**kw, "fetch": fetcher()}))
    f = tmp_path / "file.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = CliRunner().invoke(cli.main, ["resync", str(f), "--check"])
    assert result.exit_code == 1, result.output
    assert "UNBOUND" in result.output


def test_cli_resync_counts_every_entry(monkeypatch, tmp_path):
    from consentvid import cli

    real_check = rs.check
    monkeypatch.setattr(rs, "check", lambda it, **kw: real_check(it, **{**kw, "fetch": fetcher()}))
    m = manifest(file_object(), {**file_object(), "@id": "other", "@type": "cr:FileSet"})
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    result = CliRunner().invoke(cli.main, ["resync", str(p), "--check"])
    assert result.exit_code == 1 and "1 of 2 clip(s) must be dropped" in result.output, result.output


# ------------------------------------------ R2-02 / R2-13: invalid newer


@pytest.mark.parametrize(
    "edit",
    [
        {"authorshipAttested": False},
        {"revocable": False},
        {"adultAttested": None},  # removed below
        {"peoplePresent": "noIdentifiablePeople", "modalities": ["video", "face"]},
        {"createdAt": "yesterday"},
        {"modelClasses": ["commercial\x1b[2K"]},
    ],
)
def test_invalid_consent_is_invalid(edit):
    bad = consent_record(**{k: v for k, v in edit.items() if v is not None})
    for k, v in edit.items():
        if v is None:
            bad.pop(k)
    r = rs.check(item(), fetch=fetcher(consent=bad))
    assert r.status in (rs.INVALID, rs.MODIFIED)
    assert r.status != rs.VALID


def test_future_created_at_is_informational_only():
    """createdAt orders nothing any more: a future one is not an error."""
    c = consent_record(createdAt="2999-01-01T00:00:00Z", expiresAt="2999-06-01T00:00:00Z")
    from consentvid.validate import validate_in_repo

    assert validate_in_repo("org.consentvid.consent", c, ALICE, rkey=K1) == []


# ------------------------------------------------ R2-01: encodingFormat


def test_ipfs_entry_on_git_lookalike_host_with_git_encoding_is_dropped():
    """R2-01: mlcroissant git-clones https://github.com* when encodingFormat
    is git+https, and skips the sha256 check for the cloned directory."""
    video = video_record(storageRef={"kind": "ipfs", "value": f"ipfs://{CID}"})
    fo = file_object(video=video)
    fo["contentUrl"] = f"https://github.com.evil.example/ipfs/{CID}"
    fo["encodingFormat"] = "git+https"
    m = manifest(fo)
    (it,) = rs.items_from_manifest(m)
    results = rs.check_all([it], fetch=fetcher(video=video))
    assert results[0].status == rs.MODIFIED
    assert rs.filter_manifest(m, results, "now")["distribution"] == []


@pytest.mark.parametrize("host", ["github.com.evil.example", "GitHub.com", "gitlab.com", "huggingface.co.x.example"])
def test_ipfs_gateway_may_not_look_like_a_git_host(host):
    video = video_record(storageRef={"kind": "ipfs", "value": f"ipfs://{CID}"})
    fo = {**file_object(video=video), "contentUrl": f"https://{host}/ipfs/{CID}"}
    (it,) = rs.items_from_manifest(manifest(fo))
    assert rs.check(it, fetch=fetcher(video=video)).status == rs.MODIFIED


@pytest.mark.parametrize(
    "host",
    [
        "127.1", "10.1", "0x7f.0.0.1", "localhost", "[::1]", "gw.example:8443",
        # R3-01: Unicode full stops (httpx turns these into the IP)
        "169.254.169\uff0e254", "127.0.0\u30021", "10.0.0.1\uff61",
    ],
)
def test_ipfs_gateway_must_be_public(host):
    """R2-11: the same no-resolve host check as storageRef URLs."""
    assert not rs._url_matches(f"https://{host}/ipfs/{CID}", {"kind": "ipfs", "value": CID})


def test_ascii_ipfs_gateway_still_matches():
    assert rs._url_matches(f"https://gateway.pinata.cloud/ipfs/{CID}", {"kind": "ipfs", "value": CID})


def test_ipfs_entry_on_any_public_gateway_is_still_valid():
    video = video_record(storageRef={"kind": "ipfs", "value": f"ipfs://{CID}"})
    fo = {**file_object(video=video), "contentUrl": f"https://some-gateway.example/ipfs/{CID}"}
    (it,) = rs.items_from_manifest(manifest(fo))
    assert rs.check(it, fetch=fetcher(video=video)).status == rs.VALID


@pytest.mark.parametrize("fmt", ["git+https", "video/*", "application/zip", None])
def test_r2_entry_with_edited_encoding_format_is_modified(fmt):
    fo = file_object()
    if fmt is None:
        del fo["encodingFormat"]
    else:
        fo["encodingFormat"] = fmt
    (it,) = rs.items_from_manifest(manifest(fo))
    r = rs.check(it, fetch=fetcher())
    assert r.status == rs.MODIFIED and "encodingFormat" in r.reason


def test_signed_video_with_git_mime_type_is_invalid():
    video = video_record(mimeType="git+https")
    (it,) = rs.items_from_manifest(manifest(file_object(video=video)))
    assert rs.check(it, fetch=fetcher(video=video)).status == rs.INVALID


def test_encoding_format_follows_the_signed_mime_type():
    video = video_record(mimeType="video/webm")
    (it,) = rs.items_from_manifest(manifest(file_object(video=video)))
    assert it.encoding_format == "video/webm"
    assert rs.check(it, fetch=fetcher(video=video)).status == rs.VALID


def legacy_manifest():
    """A manifest as the pre-cv: exporter (3fc54ea..533c2a3) wrote it."""
    from consentvid.croissant import LEGACY_LICENSE_URL

    fo = file_object()
    receipt = fo.pop("cv:consentReceipt")
    for k in ("peoplePresent", "adultAttested", "videoCid", "cid", "authorshipAttested", "rightsBasis",
              "captureProvenance"):
        receipt.pop(k)
    legacy = {
        "@type": "cr:FileObject",
        "@id": f"video-{K1}",
        "name": fo["name"],
        "description": fo["description"],
        "contentUrl": fo["contentUrl"],
        "encodingFormat": "video/*",
        "sha256": fo["sha256"],
        "cr:atUri": VURI,
        "cr:storageKind": "r2",
        "cr:consentReceipt": receipt,
    }
    m = manifest(legacy)
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    m["license"] = LEGACY_LICENSE_URL
    m["creator"] = {"@type": "sc:Person", "name": "alice.test", "identifier": ALICE}
    return m


def test_legacy_manifest_still_valid():
    (it,) = rs.items_from_manifest(legacy_manifest())
    assert it.legacy and it.problem is None
    r = rs.check(it, fetch=fetcher())
    assert r.status == rs.VALID, r


def test_current_entry_may_not_use_the_legacy_encoding_format():
    fo = {**file_object(), "encodingFormat": "video/*"}
    (it,) = rs.items_from_manifest(manifest(fo))
    assert rs.check(it, fetch=fetcher()).status == rs.MODIFIED


# --------------------------------------------- R2-09: canonical @id only


def test_relative_iri_aliases_are_invalid():
    a = {**file_object(), "@id": "x"}
    b = {**file_object(), "@id": "./x"}
    items = rs.items_from_manifest(manifest(a, b))
    results = [rs.check(i, fetch=fetcher()) for i in items]
    assert [r.status for r in results] == [rs.INVALID, rs.INVALID]
    assert all("non-canonical @id" in r.reason for r in results)


@pytest.mark.parametrize(
    "bad_id",
    [f"{ALICE}/{K2}", f"{BOB}/{K1}", f"video-{K1}", f"{ALICE}/{K1}/", f"./{ALICE}/{K1}", f"{ALICE}/./{K1}",
     f"{ALICE.upper()}/{K1}"],
)
def test_non_canonical_id_is_invalid(bad_id):
    (it,) = rs.items_from_manifest(manifest({**file_object(), "@id": bad_id}))
    r = rs.check(it, fetch=fetcher())
    assert r.status == rs.INVALID and "non-canonical" in r.reason


def test_canonical_id_is_valid():
    (it,) = rs.items_from_manifest(manifest(file_object()))
    assert it.id == f"{ALICE}/{K1}" and rs.check(it, fetch=fetcher()).status == rs.VALID


# --------------------------------- R2-03: --write carries only signed data


def test_blanket_license_is_refused():
    m = manifest(file_object())
    m["license"] = "https://creativecommons.org/publicdomain/zero/1.0/"
    with pytest.raises(ConsentVidError, match="license"):
        rs.items_from_manifest(m)
    del m["license"]
    with pytest.raises(ConsentVidError, match="license"):
        rs.items_from_manifest(m)


def test_legacy_license_only_with_legacy_context():
    from consentvid.croissant import LEGACY_LICENSE_URL

    m = manifest(file_object())
    m["license"] = LEGACY_LICENSE_URL
    with pytest.raises(ConsentVidError, match="license"):
        rs.items_from_manifest(m)


def test_legacy_context_requires_legacy_entries():
    """R3-03: relabelling a current manifest with the legacy context must not
    unlock the legacy dataset-wide CC-BY licence."""
    from consentvid.croissant import LEGACY_LICENSE_URL

    m = manifest(file_object())
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    m["license"] = LEGACY_LICENSE_URL
    with pytest.raises(ConsentVidError, match="legacy @context with current-format entries"):
        rs.items_from_manifest(m)
    # mixing one legacy and one current entry is refused too
    leg = legacy_manifest()
    leg["distribution"].append(file_object())
    with pytest.raises(ConsentVidError, match="legacy @context"):
        rs.items_from_manifest(leg)


def test_legacy_context_requires_legacy_entries_cli(tmp_path):
    from click.testing import CliRunner

    from consentvid.cli import main
    from consentvid.croissant import LEGACY_LICENSE_URL

    m = manifest(file_object())
    m["@context"] = copy.deepcopy(LEGACY_CONTEXTS[0])
    m["license"] = LEGACY_LICENSE_URL
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    r = CliRunner().invoke(main, ["resync", str(p), "--check"])
    assert r.exit_code == 1, r.output


def test_resync_without_write_says_dataset_text_is_unverified(tmp_path, monkeypatch):
    from consentvid.cli import main

    monkeypatch.setattr(rs, "check_all", lambda items, **k: [rs.Result(i.id, i.name, i.consent_uri, rs.VALID) for i in items])
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest(file_object())))
    r = CliRunner().invoke(main, ["resync", str(p)])
    assert r.exit_code == 0, r.output
    assert "dataset-level text (description, rai:*, creator) is not verified" in r.output


def test_current_context_refuses_legacy_entries():
    leg = legacy_manifest()["distribution"][0]
    with pytest.raises(ConsentVidError, match="legacy-format"):
        rs.items_from_manifest(manifest(leg))


def test_write_rebuilds_dataset_level_claims_from_verified_clips():
    m = manifest(file_object())
    m["creator"] = [{"@type": "sc:Person", "name": "Some Other Person", "identifier": "did:plc:someoneelse"}]
    m["rai:dataUseCases"] = ["Purposes granted by every clip: training, commercial", "Model classes: commercial"]
    m["rai:dataLimitations"] = "None. All contributors waived consent restrictions (CC0)."
    m["rai:personalSensitiveInformation"] = "none"
    m["description"] = "Public domain, use for anything."
    m["citeAs"] = "whatever"
    m["name"] = "my dataset"
    m["version"] = "2.0.0"
    results = rs.check_all(rs.items_from_manifest(m), fetch=fetcher())
    assert [r.status for r in results] == [rs.VALID]
    out = rs.filter_manifest(m, results, "now")
    fresh, _ = build_manifest([clip()], name="my dataset", version="2.0.0")
    assert out["creator"] == [{"@type": "sc:Person", "name": ALICE, "identifier": ALICE, "sameAs": f"at://{ALICE}"}]
    assert out["rai:dataUseCases"] == fresh["rai:dataUseCases"]
    assert "commercial" not in json.dumps(out["rai:dataUseCases"])
    assert out["rai:dataLimitations"] == fresh["rai:dataLimitations"]
    assert out["description"] == fresh["description"] and "Public domain" not in json.dumps(out)
    assert out["license"] == fresh["license"] and out["name"] == "my dataset" and out["version"] == "2.0.0"
    assert out["rai:dataReleaseMaintenancePlan"].endswith("Re-synced now: 0 clip(s) dropped, 1 remain.")
    assert set(out) == set(fresh)


def test_lenient_unknown_entries_are_flagged():
    m = manifest(file_object())
    (it,) = rs.items_from_manifest(m)
    r = rs.check(it, fetch=fetcher(raise_for={CURI: OSError("down")}))
    assert r.status == rs.UNKNOWN
    out = rs.filter_manifest(m, [r], "now", strict=False)
    assert out["distribution"] == [file_object()]
    assert "1 clip(s) kept unverified under --lenient" in out["rai:dataLimitations"]
    assert out["creator"] == []  # nothing verified


# ------------------------------------------- R2-08: duplicate JSON keys


def _dup_manifest_text():
    text = json.dumps(manifest(file_object()))
    return text.replace('"contentUrl": ', '"contentUrl": "https://evil.example/other.mp4", "contentUrl": ', 1)


def test_duplicate_keys_are_refused():
    with pytest.raises(ConsentVidError, match="duplicate key 'contentUrl'"):
        rs.load_manifest_json(_dup_manifest_text())
    nested = json.dumps(manifest(file_object())).replace(
        '"purposes": ["training"]', '"purposes": ["training", "commercial"], "purposes": ["training"]', 1
    )
    with pytest.raises(ConsentVidError, match="duplicate key 'purposes'"):
        rs.load_manifest_json(nested)
    assert rs.load_manifest_json(json.dumps(manifest(file_object())))["distribution"]
    with pytest.raises(ValueError):
        rs.load_manifest_json("not json")
    with pytest.raises(ValueError):
        rs.load_manifest_json('{"a": NaN}')


def test_cli_resync_refuses_duplicate_keys(monkeypatch, tmp_path):
    from consentvid import cli

    checked = []
    monkeypatch.setattr(rs, "check", lambda it, **kw: checked.append(it))
    p = tmp_path / "m.json"
    p.write_text(_dup_manifest_text())
    result = CliRunner().invoke(cli.main, ["resync", str(p), "--check"])
    assert result.exit_code == 1 and "duplicate key" in result.output and checked == []


# ------------------------------------- R2-10: no terminal escapes echoed


def test_cli_resync_escapes_control_characters(monkeypatch, tmp_path):
    from consentvid import cli

    real_check = rs.check
    monkeypatch.setattr(rs, "check", lambda it, **kw: real_check(it, **{**kw, "fetch": fetcher()}))
    evil = "\r\x1b[2K  [      ok] all good\x1b]52;c;cHduZWQ=\x07‮ "
    good = file_object()
    # second entry: non-canonical @id -> INVALID, with the @id in the reason
    m = manifest({**good, "name": evil}, {**file_object(), "name": evil, "@id": "x" + evil})
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    result = CliRunner().invoke(cli.main, ["resync", str(p)], color=True)
    out = result.output
    for ch in ("\r", "\x07", "‮", " ", "\x1b]", "\x1b[2K"):
        assert ch not in out, (ch, out)
    assert "\\x1b" in out and "\\x0d" in out
    assert "1 of 2 clip(s) must be dropped" in out


# ------------------------- R2-15: relay-level takedowns make clips unavailable


def _relay_env(monkeypatch, answer):
    from consentvid import appview as av
    from consentvid import resync as rs_mod

    calls = []

    def relay_repo_status(relay, did):
        calls.append(did)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(av, "relay_repo_status", relay_repo_status)
    monkeypatch.setattr(rs_mod, "_relay_lookup", rs_mod._relay_lookup_impl)
    rs_mod.clear_relay_cache()
    return calls


def _valid_clip_fetch():
    from consentvid.verify import VerifiedRecord

    consent = consent_record()
    video = video_record()

    def fetch(at):
        if at.collection == "org.consentvid.consent":
            return VerifiedRecord(str(at), True, "bafyc1", consent, "3maaaaaaaaaa2", True)
        return VerifiedRecord(str(at), True, VIDEO_CID, video, "3maaaaaaaaaa2", True)

    return fetch


def _clip_item():
    from consentvid.croissant import Clip, _file_object

    clip = Clip(f"at://{ALICE}/org.consentvid.consent/{K1}", "bafyc1", consent_record(),
                f"at://{ALICE}/org.consentvid.video/{K1}", VIDEO_CID, video_record(), ALICE)
    fo = _file_object(clip)
    return rs.items_from_manifest({**_manifest_shell(), "distribution": [fo]})[0]


def _manifest_shell():
    from consentvid.croissant import build_manifest

    m, _ = build_manifest([], name="t")
    return {k: v for k, v in m.items() if k != "distribution"}


@pytest.mark.parametrize(
    "answer, status",
    [
        ((False, "takendown"), rs.UNAVAILABLE),
        ((False, "deleted"), rs.UNAVAILABLE),
        ((True, None), rs.VALID),
        ("not-found", rs.VALID),
        (OSError("relay down"), rs.VALID),
    ],
)
def test_relay_status_gates_otherwise_valid_clips(monkeypatch, answer, status):
    from consentvid.errors import RepoUnavailable

    if answer == "not-found":
        answer = RepoUnavailable(ALICE, "not-found")
    calls = _relay_env(monkeypatch, answer)
    r = rs.check(_clip_item(), fetch=_valid_clip_fetch())
    assert r.status == status, r.reason
    if status == rs.UNAVAILABLE:
        assert "relay reports account" in r.reason
    assert calls == [ALICE]


def test_no_relay_status_skips_the_lookup(monkeypatch):
    calls = _relay_env(monkeypatch, (False, "takendown"))
    assert rs.check(_clip_item(), fetch=_valid_clip_fetch(), relay_status=False).status == rs.VALID
    assert calls == []


def test_one_relay_call_per_did_across_check_all(monkeypatch):
    calls = _relay_env(monkeypatch, (True, None))
    results = rs.check_all([_clip_item() for _ in range(6)], fetch=_valid_clip_fetch(), workers=4)
    assert {r.status for r in results} == {rs.VALID} and calls == [ALICE]


# ------------------------- R3-01: relay processing statuses are not account statuses


@pytest.mark.parametrize("status", ["throttled", "host-throttled", "desynchronized"])
def test_relay_operational_status_keeps_a_proven_clip_valid(monkeypatch, status):
    calls = _relay_env(monkeypatch, (False, status))
    r = rs.check(_clip_item(), fetch=_valid_clip_fetch())
    assert r.status == rs.VALID and r.verified, r.reason
    assert f"relay reports {status}; live index may be stale" in r.reason
    assert calls == [ALICE]


@pytest.mark.parametrize("status", ["takendown", "mystery", None])
def test_relay_account_or_unknown_status_still_fails_closed(monkeypatch, status):
    _relay_env(monkeypatch, (False, status))
    r = rs.check(_clip_item(), fetch=_valid_clip_fetch())
    assert r.status == rs.UNAVAILABLE and "relay reports account" in r.reason


# ------------------------- R3-12: a 5xx PDS plus a relay that knows the account is gone


def _failing_fetch(exc):
    def fetch(at):
        raise exc

    return fetch


def _http_500():
    import httpx

    req = httpx.Request("GET", "https://pds.example/xrpc/com.atproto.sync.getRecord")
    return httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))


def test_pds_500_and_relay_deleted_is_unavailable_not_revoked(monkeypatch):
    calls = _relay_env(monkeypatch, (False, "deleted"))
    r = rs.check(_clip_item(), fetch=_failing_fetch(_http_500()))
    assert r.status == rs.UNAVAILABLE and not r.verified
    assert r.reason == "PDS unreachable; relay reports account deleted"
    assert calls == [ALICE]


@pytest.mark.parametrize("answer", [(False, "throttled"), "not-found", OSError("relay down"), (True, None)])
def test_pds_500_without_an_account_answer_stays_unknown(monkeypatch, answer):
    from consentvid.errors import RepoUnavailable

    if answer == "not-found":
        answer = RepoUnavailable(ALICE, "not-found")
    _relay_env(monkeypatch, answer)
    assert rs.check(_clip_item(), fetch=_failing_fetch(_http_500())).status == rs.UNKNOWN


def test_pds_network_error_on_the_video_asks_the_relay_too(monkeypatch):
    _relay_env(monkeypatch, (False, "takendown"))
    ok = _valid_clip_fetch()

    def fetch(at):
        if at.collection == "org.consentvid.video":
            raise OSError("down")
        return ok(at)

    # The consent read succeeded, so the relay's takedown already applies.
    assert rs.check(_clip_item(), fetch=fetch).status == rs.UNAVAILABLE


def test_pds_failure_skips_the_relay_when_disabled(monkeypatch):
    calls = _relay_env(monkeypatch, (False, "deleted"))
    r = rs.check(_clip_item(), fetch=_failing_fetch(_http_500()), relay_status=False)
    assert r.status == rs.UNKNOWN and calls == []


def test_pds_failures_ask_the_relay_once_per_did(monkeypatch):
    calls = _relay_env(monkeypatch, (False, "suspended"))
    results = rs.check_all([_clip_item() for _ in range(5)], fetch=_failing_fetch(OSError("down")), workers=3)
    assert {r.status for r in results} == {rs.UNAVAILABLE} and calls == [ALICE]
