import pytest

from conftest import ALICE, BOB, consent_record, video_record
from consentvid.records import format_dt, is_expired, normalize_dt, parse_at_uri, parse_dt
from consentvid.validate import validate_in_repo, validate_record, write_policy_errors

C, V = "org.consentvid.consent", "org.consentvid.video"


@pytest.mark.parametrize(
    "uri",
    [
        "at://alice.bsky.social/org.consentvid.video/abc",  # handle authority: mutable
        "at://did:plc:x/org.consentvid.video",
        "at://did:plc:x/notannsid/abc",
        "https://example.org",
        "at://did:plc:x/org.consentvid.video/..",
    ],
)
def test_bad_at_uris(uri):
    with pytest.raises(ValueError):
        parse_at_uri(uri)


def test_at_uri_collection_check():
    assert parse_at_uri(f"at://{ALICE}/{V}/3abc").rkey == "3abc"
    with pytest.raises(ValueError):
        parse_at_uri(f"at://{ALICE}/{V}/3abc", C)


def test_datetimes_need_timezone_and_are_fixed_width():
    assert parse_dt("2026-01-01T00:00:00") is None
    assert parse_dt("2026-01-01") is None
    assert parse_dt("2026-01-01T00:00:00+02:00") is not None
    a = normalize_dt("2026-01-01T00:00:00+02:00")
    b = normalize_dt("2025-12-31T23:30:00.5Z")
    assert a == "2025-12-31T22:00:00.000Z" and len(a) == len(b) and a < b
    assert format_dt(parse_dt("2026-03-04T05:06:07.123456Z")) == "2026-03-04T05:06:07.123Z"
    assert is_expired({"expiresAt": "2000-01-01T00:00:00Z"})
    assert not is_expired({})


def test_valid_records():
    assert validate_record(V, video_record()) == []
    assert validate_in_repo(C, consent_record(), ALICE) == []


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"authorshipAttested": False}, "authorshipAttested"),
        ({"revocable": False}, "revocable"),
        ({"purposes": []}, "minLength"),
        ({"purposes": ["training", "training"]}, "duplicates"),
        ({"createdAt": "2026-01-01T00:00:00"}, "datetime"),
        ({"expiresAt": "2025-01-01T00:00:00Z"}, "after createdAt"),
        ({"videoCid": "not-a-cid"}, "CID"),
        ({"videoUri": "at://alice.test/org.consentvid.video/x"}, "DID"),
        ({"modalities": "video"}, "expected array"),
        ({"$type": "app.bsky.feed.post"}, "$type"),
        ({"peoplePresent": None}, "null"),
        ({"peoplePresent": "someone"}, "peoplePresent"),
        ({"adultAttested": False}, "adultAttested"),
        ({"peoplePresent": "noIdentifiablePeople", "modalities": ["video", "face"]}, "contradict"),
    ],
)
def test_invalid_consents(change, needle):
    errors = validate_record(C, consent_record(**change))
    assert any(needle in e for e in errors), errors


def test_known_values_are_open_and_unknown_fields_ignored():
    assert validate_record(C, consent_record(purposes=["somethingNew"], extra=1)) == []


def test_consent_must_reference_own_video():
    errors = validate_in_repo(C, consent_record(did=ALICE), BOB)
    assert errors and "own video" in errors[0]


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"contentHash": "sha256-XYZ"}, "contentHash"),
        ({"durationMs": -1}, "non-negative"),
        ({"durationMs": 1.5}, "integer"),
        ({"language": "english!"}, "BCP-47"),
        ({"title": "x" * 301}, "maxLength"),
        ({"storageRef": {"kind": "r2"}}, "storageRef.value"),
    ],
)
def test_invalid_videos(change, needle):
    errors = validate_record(V, video_record(**change))
    assert any(needle in e for e in errors), errors


def test_write_policy_blocks_local_paths():
    rec = video_record(storageRef={"kind": "local", "value": "/Users/me/Desktop/clip.mov"})
    assert validate_record(V, rec) == []  # other clients' records aren't rejected...
    assert write_policy_errors(V, rec)  # ...but our CLI never publishes a path
    assert not write_policy_errors(V, video_record(storageRef={"kind": "local", "value": "clip.mov"}))


def test_consent_without_new_attestations_is_invalid():
    rec = consent_record()
    del rec["peoplePresent"], rec["adultAttested"]
    errors = validate_record(C, rec)
    assert any("peoplePresent" in e for e in errors) and any("adultAttested" in e for e in errors)


def test_deprecated_values_still_valid_but_not_offered():
    from consentvid.vocab import OFFERED_MODEL_CLASSES, OFFERED_PURPOSES

    assert validate_record(C, consent_record(purposes=["researchOnly"], modelClasses=["foundationModel"])) == []
    assert "researchOnly" not in OFFERED_PURPOSES and "foundationModel" not in OFFERED_MODEL_CLASSES
    assert validate_record(C, consent_record(peoplePresent="noIdentifiablePeople", modalities=["video"])) == []


# ------------------------------------------------- R1-04: anchored syntax


def test_trailing_newline_is_not_syntax():
    from consentvid.records import is_did, is_handle

    with pytest.raises(ValueError):
        parse_at_uri(f"at://{ALICE}/{C}/c1\n")
    with pytest.raises(ValueError):
        parse_at_uri(f"at://{ALICE}\n/{C}/c1")
    assert not is_did("did:plc:x\n") and is_did("did:plc:x")
    assert not is_handle("alice.test\n") and is_handle("alice.test")
    assert not is_did(None) and not is_did(["did:plc:x"])
    assert parse_dt("2026-01-01T00:00:00\n+00:00") is None
    errors = validate_record(V, video_record(contentHash="sha256-" + "ab" * 32 + "\n"))
    assert any("contentHash" in e for e in errors)
    assert any("BCP-47" in e for e in validate_record(V, video_record(language="en\n")))


# --------------------------------------------- R1-07: poison-pill records


@pytest.mark.parametrize(
    "change",
    [
        {"modalities": [{}]},
        {"purposes": [[1]]},
        {"modelClasses": [{"a": 1}, {"a": 1}]},
        {"peoplePresent": "noIdentifiablePeople", "modalities": [["face"]]},
        {"createdAt": "9999-12-31T23:59:59-05:00"},
        {"expiresAt": "0001-01-01T00:00:00+01:00"},
    ],
)
def test_poison_consents_return_errors_instead_of_raising(change):
    errors = validate_record(C, consent_record(**change))
    assert errors


def test_overflowing_datetimes_never_parse():
    assert parse_dt("9999-12-31T23:59:59-05:00") is None
    assert normalize_dt("9999-12-31T23:59:59-05:00") is None
    assert normalize_dt("9999-12-31T23:59:59Z") == "9999-12-31T23:59:59.000Z"


def test_integers_are_64_bit():
    assert any("64-bit" in e for e in validate_record(V, video_record(durationMs=2**63)))
    assert any("64-bit" in e for e in validate_record(V, video_record(width=-(2**63) - 1)))
    assert validate_record(V, video_record(durationMs=2**63 - 1)) == []


def test_validate_record_is_total(monkeypatch):
    import consentvid.validate as val

    def boom(*a):
        raise RecursionError("deep")

    monkeypatch.setattr(val, "_validate_object", boom)
    assert validate_record(V, video_record()) == ["record could not be validated: RecursionError"]


# ---------------------------------------- R1-11: storageRef per-kind rules


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "r2", "value": "/Users/x/v.mp4"},
        {"kind": "url", "value": "file:///etc/passwd"},
        {"kind": "url", "value": "http://169.254.169.254/"},
        {"kind": "url", "value": "javascript:alert(1)"},
        {"kind": "url", "value": "https://user:pw@example.org/v.mp4"},
        {"kind": "url", "value": "https://example.org/v.mp4\nX-Header: 1"},
        {"kind": "url", "value": "https:///nohost"},
        {"kind": "peertube", "value": "https://pt.example/w/a b"},
        {"kind": "ipfs", "value": "../ipns/k51x"},
        {"kind": "ipfs", "value": "ipfs://bafyX"},
        {"kind": "local", "value": "clip\x00.mp4"},
    ],
)
def test_bad_storage_refs_rejected(ref):
    assert validate_record(V, video_record(storageRef=ref))


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "r2", "value": "https://pub-x.r2.dev/videos/" + "sha256-" + "ab" * 32},
        {"kind": "url", "value": "https://example.org/v.mp4"},
        {"kind": "peertube", "value": "https://pt.example/w/abcdefgh12"},
        {"kind": "ipfs", "value": "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"},
        {"kind": "ipfs", "value": "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"},
        {"kind": "local", "value": "clip.mp4"},
    ],
)
def test_good_storage_refs_accepted(ref):
    assert validate_record(V, video_record(storageRef=ref)) == []


# ------------------------------------------- R2-11: internal hosts, ports


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "url", "value": "https://169.254.169.254/x"},
        {"kind": "url", "value": "https://localhost:8443/a"},
        {"kind": "url", "value": "https://[::1]/x"},
        {"kind": "url", "value": "https://127.1/x"},
        {"kind": "url", "value": "https://0x7f.0.0.1/x"},
        {"kind": "url", "value": "https://10.1/x"},
        {"kind": "url", "value": "https://2130706433/x"},
        {"kind": "url", "value": "https://printer.local/x"},
        {"kind": "url", "value": "https://intranet/x"},
        {"kind": "url", "value": "https://example.org:8080/v.mp4"},
        {"kind": "r2", "value": "https://pub-x.r2.dev:9/videos/x"},
        {"kind": "peertube", "value": "https://metadata.google.internal/w/abcdefgh12"},
        # R3-01: Unicode full stops that IDNA maps to '.'
        {"kind": "url", "value": "https://169.254.169\uff0e254/x"},
        {"kind": "url", "value": "https://127.0.0\u30021/x"},
        {"kind": "url", "value": "https://10.0.0.1\uff61/x"},
        {"kind": "url", "value": "https://metadata.google.internal\u3002/x"},
    ],
)
def test_internal_https_storage_refs_rejected(ref):
    assert validate_record(V, video_record(storageRef=ref))


def test_unicode_dot_hosts_normalise_to_ips_in_httpx():
    """R3-01: why the hosts above must be refused: httpx contacts the IP."""
    import httpx

    assert httpx.URL("https://169.254.169\uff0e254/x").host == "169.254.169.254"


def test_ascii_public_host_still_passes():
    from consentvid.records import check_public_host, is_ldh_host

    check_public_host("gateway.pinata.cloud")
    check_public_host("xn--bcher-kva.example.")
    assert not is_ldh_host("-a.example") and not is_ldh_host("a..example") and not is_ldh_host("a" * 64 + ".com")
    ref = {"kind": "url", "value": "https://gateway.pinata.cloud/ipfs/x.mp4"}
    assert validate_record(V, video_record(storageRef=ref)) == []


def test_default_https_port_is_fine():
    ref = {"kind": "url", "value": "https://example.org:443/v.mp4"}
    assert validate_record(V, video_record(storageRef=ref)) == []


# ------------------------------------- R2-12: RFC 3339 datetimes only


@pytest.mark.parametrize(
    "value",
    [
        "2026-W01-1T00:00:00Z",  # ISO week date
        "20260101T000000Z",  # basic format
        "2026-01-01T00Z",  # hour only
        "2026-01-01T00:00Z",  # no seconds
        "2026-01-01T00:00:00,5Z",  # comma fraction
        "2026-01-01T00:00:00+01:00:30",  # second offset
        "2026-01-01T00:00:00+0100",  # basic offset
        "2026-01-01t00:00:00Z",
        "2026-01-01T00:00:00z",
        "2026-01-01T00:00:00-00:00",  # negative zero offset (forbidden by atproto)
        "0999-01-01T00:00:00Z",
        "٢٠٢٦-01-01T00:00:00Z",  # non-ASCII digits
    ],
)
def test_non_rfc3339_datetimes_never_parse(value):
    assert parse_dt(value) is None
    assert validate_in_repo(C, consent_record(createdAt=value), ALICE)


def test_format_dt_is_fixed_width_for_small_years():
    from datetime import UTC, datetime

    assert format_dt(datetime(999, 1, 2, 3, 4, 5, tzinfo=UTC)) == "0999-01-02T03:04:05.000Z"
    assert format_dt(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000Z"
    assert parse_dt("2026-01-01T00:00:00.123456789Z") is not None


def test_exported_receipt_carries_normalised_timestamps():
    from conftest import VIDEO_CID
    from consentvid.croissant import Clip, _file_object

    c = consent_record(createdAt="2026-01-01T02:00:00.5+02:00", expiresAt="2030-01-01T00:00:00+00:00")
    clip = Clip(f"at://{ALICE}/{C}/c1", "bafyc", c, f"at://{ALICE}/{V}/v1", VIDEO_CID, video_record(), ALICE)
    r = _file_object(clip)["cv:consentReceipt"]
    assert r["createdAt"] == "2026-01-01T00:00:00.500Z" and r["expiresAt"] == "2030-01-01T00:00:00.000Z"


# ------------------------------- R2-01: mimeType is a video container type


@pytest.mark.parametrize("mime", ["git+https", "application/x-git", "text/html", "video/*", "video/mp4; codecs=avc1"])
def test_video_mime_type_must_be_a_video_container(mime):
    errors = validate_record(V, video_record(mimeType=mime))
    assert any("mimeType" in e for e in errors)


@pytest.mark.parametrize("mime", ["video/mp4", "video/quicktime", "video/webm", "video/x-matroska"])
def test_video_mime_types_accepted(mime):
    assert validate_record(V, video_record(mimeType=mime)) == []


# --------------------------------- R2-13: future-dated consents refused


def test_future_created_at_is_invalid():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 6, tzinfo=UTC)
    soon = format_dt(now + timedelta(hours=1))
    later = format_dt(now + timedelta(days=30))
    assert validate_in_repo(C, consent_record(createdAt=soon), ALICE, now=now) == []
    record = consent_record(createdAt=later, expiresAt="2031-01-01T00:00:00Z")
    assert any("future" in e for e in validate_in_repo(C, record, ALICE, now=now))


# ------------------------------------ R2-10: no control chars in vocab


@pytest.mark.parametrize(
    "change",
    [
        {"modelClasses": ["academic", "commercial\x1b[2K"]},
        {"purposes": ["training\r"]},
        {"modalities": ["video", "audio‮"]},
        {"captureProvenance": "selfAttested\x07"},
        {"compensation": "none\x1b]52;c;AAAA\x07"},
        {"rightsBasis": "own Recording"},
        {"peoplePresent": "onlyMe\x00"},
    ],
)
def test_control_characters_in_consent_terms_are_errors(change):
    assert validate_in_repo(C, consent_record(**change), ALICE)
