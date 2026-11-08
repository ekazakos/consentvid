import pytest

from conftest import (
    ALICE,
    BOB,
    K1,
    consent_record,
    video_record,
    video_v2_record,
)
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
        parse_at_uri(f"at://{ALICE}\n/{C}/{K1}")
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
    assert validate_record(V, video_record(durationMs=2**53 - 1)) == []  # R2-21: 53-bit bound


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
        "0000-01-01T00:00:00Z",  # valid per spec, but not representable in Python
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
    clip = Clip(f"at://{ALICE}/{C}/{K1}", "bafyc", c, f"at://{ALICE}/{V}/{K1}", VIDEO_CID, video_record(), ALICE)
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


# ----------------------- R1-01: createdAt is informational (no ordering)


def test_future_created_at_is_not_an_error():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 6, tzinfo=UTC)
    later = format_dt(now + timedelta(days=30))
    record = consent_record(createdAt=later, expiresAt="2031-01-01T00:00:00Z")
    assert validate_in_repo(C, record, ALICE, rkey=K1, now=now) == []


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


# ------------------------------------- R1-01: record keys and the sidecar


def test_consent_key_must_equal_its_videos_key():
    from conftest import K2

    assert validate_in_repo(C, consent_record(), ALICE, rkey=K1) == []
    errors = validate_in_repo(C, consent_record(), ALICE, rkey=K2)
    assert any("sidecar" in e for e in errors)


@pytest.mark.parametrize("rkey", ["self", "v1", "3maaaaaaaaab", "3MAAAAAAAAAB2", "kmaaaaaaaaab2", "3maaaaaaaaab2x"])
def test_non_tid_record_key_is_invalid(rkey):
    assert any("not a TID" in e for e in validate_in_repo(V, video_record(), ALICE, rkey=rkey))


def test_record_key_rules_need_the_key():
    from conftest import K2

    # Without an rkey (the PDS assigns it) only record-local rules apply.
    assert validate_in_repo(C, consent_record(rkey=K2), ALICE) == []


# ---------------------------------------------- R1-02: TIDs and CIDs


def test_new_tids_are_valid_monotonic_and_now():
    import time

    from consentvid.records import decode_tid, is_tid, new_tid

    tids = [new_tid() for _ in range(1000)]
    assert all(is_tid(t) for t in tids) and tids == sorted(tids) and len(set(tids)) == 1000
    assert abs((decode_tid(tids[-1]) >> 10) / 1e6 - time.time()) < 1


def test_compute_cid_matches_the_pds(cars):
    from consentvid.records import compute_cid
    from consentvid.verify import verify_car

    _, cid, value, _ = verify_car(
        cars["present"], did=cars["did"], signing_key=cars["signing_key"],
        key=f"org.consentvid.consent/{cars['present_rkey']}",
    )
    assert compute_cid(value) == cid == cars["present_cid"]


# --------------------------------------------------- R1-18: canonical CIDs


@pytest.mark.parametrize(
    "cid",
    [
        "QmbWqxBEKC3P8tqsKc98xmWNzrzDtRLMiMPL8wBuTGsMnR",  # CIDv0
        "BAFYREIE7SJGNUAT5JZPMMNV6WR6HIYVPA4UPFTOBV2RLLNCI5PLEENF2XQ",  # uppercase base32
        "zdpuAyvkgEDQm9TenwGkd5eNaosSxjgEYd8QatfPetgB1CdEZ",  # base58btc
        "f01711220" + "ab" * 32,  # base16
        "bafyreie7sjgnuat5jzpmmnv6wr6hiyvpa4upftobv2rllnci5pleenf2x",  # truncated
    ],
)
def test_non_canonical_cids_are_rejected(cid):
    assert any("is not a CID" in e for e in validate_record(C, consent_record(videoCid=cid)))


def test_canonical_record_cid_accepted_and_raw_refused_for_video_cid():
    from conftest import VIDEO_CID

    assert validate_record(C, consent_record(videoCid=VIDEO_CID)) == []
    raw = "bafkreie7sjgnuat5jzpmmnv6wr6hiyvpa4upftobv2rllnci5pleenf2xq"
    errors = validate_record(C, consent_record(videoCid=raw))
    assert errors and any("dag-cbor" in e for e in errors)


# --------------------------------------------------------- R1-19: $type


def test_record_without_type_is_invalid():
    rec = consent_record()
    del rec["$type"]
    assert validate_record(C, rec) == ["missing $type"]


# ------------------------------------- R1-20: syntax limits, graphemes


def test_syntax_length_caps():
    from consentvid.records import is_did, is_handle, is_nsid

    assert is_did("did:plc:" + "a" * 2040) and not is_did("did:plc:" + "a" * 2041)
    assert not is_handle("a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 61 + ".com")
    long_nsid = ".".join(["a" * 63] * 5) + ".b"
    assert len(long_nsid) > 317 and not is_nsid(long_nsid)
    assert not is_nsid("org-.consentvid.video") and not is_nsid("org.consentvid-.video")
    assert is_nsid("org.consent-vid.video")


@pytest.mark.parametrize(
    "text,n",
    [("abc", 3), ("e\u0301", 1), ("\U0001F44D\U0001F3FD", 1), ("\U0001F1E8\U0001F1FF", 1),
     ("\u0928\u093f", 1), ("\U0001F468\u200d\U0001F469\u200d\U0001F467", 1), ("", 0)],
)
def test_grapheme_len_counts_clusters(text, n):
    """maxGraphemes counts extended grapheme clusters, not code points."""
    from consentvid.validate import grapheme_len

    assert grapheme_len(text) == n


def test_v2_title_limits_are_graphemes_and_bytes():
    from consentvid.records import VIDEO_V2_NSID

    flag = "\U0001F1E8\U0001F1FF"  # one grapheme, 8 bytes
    ok = video_v2_record(title=flag * 300)  # 300 graphemes, 2400 bytes
    assert validate_record(VIDEO_V2_NSID, ok) == []
    errs = validate_record(VIDEO_V2_NSID, video_v2_record(title="x" * 301))
    assert any("exceeds maxGraphemes 300" in e for e in errs)
    errs = validate_record(VIDEO_V2_NSID, video_v2_record(title="e" + "\u0301" * 2000))  # 1 grapheme, 4001 bytes
    assert any("exceeds maxLength 3000" in e for e in errs)


@pytest.mark.parametrize(
    "value", ["0985-04-12T23:20:50.123Z", "0123-01-01T00:00:00Z", "0010-01-01T00:00:00Z", "0001-01-01T00:00:00Z"]
)
def test_small_years_parse_and_stay_fixed_width(value):
    """R2-22: the interop vectors' early years are valid; 0000 is not representable."""
    dt = parse_dt(value)
    assert dt is not None and normalize_dt(value)[:4] == value[:4] and len(normalize_dt(value)) == 24
    assert parse_dt("0000-01-01T00:00:00Z") is None


# ------------------------- R2-18: compute_cid handles $link / $bytes


def test_compute_cid_matches_the_pds_for_a_record_with_a_blob():
    import json
    from pathlib import Path

    from consentvid.records import compute_cid

    fixture = json.loads((Path(__file__).parent / "fixtures/bsky_post_with_blob.json").read_text())
    assert compute_cid(fixture["value"]) == fixture["cid"]  # PDS-assigned CID of a real post


def test_compute_cid_bytes_and_unchanged_consentvid_records():
    import base64

    import libipld

    from consentvid.records import compute_cid

    raw = b"\x00hello"
    with_bytes = compute_cid({"$type": "x.y.z", "b": {"$bytes": base64.b64encode(raw).decode().rstrip("=")}})
    digest = __import__("hashlib").sha256(libipld.encode_dag_cbor({"$type": "x.y.z", "b": raw})).digest()
    assert with_bytes == libipld.encode_cid(b"\x01\x71\x12\x20" + digest)
    rec = consent_record()
    digest = __import__("hashlib").sha256(libipld.encode_dag_cbor(rec)).digest()
    assert compute_cid(rec) == libipld.encode_cid(b"\x01\x71\x12\x20" + digest)
    ambiguous = b"\x01\x71\x12\x20" + bytes(32)
    with pytest.raises(ValueError, match="indistinguishable"):
        compute_cid({"b": {"$bytes": base64.b64encode(ambiguous).decode()}})


# ------------------------- R2-20: language tags follow the interop vectors


def _lang_vectors(name):
    from pathlib import Path

    lines = (Path(__file__).parent / "fixtures/interop" / name).read_text().splitlines()
    return [x.strip() for x in lines if x.strip() and not x.startswith("#")]


@pytest.mark.parametrize("tag", _lang_vectors("language_syntax_valid.txt"))
def test_valid_language_vectors(tag):
    from consentvid.validate import is_language

    assert is_language(tag)
    assert not validate_in_repo(V, video_record(language=tag), ALICE)


@pytest.mark.parametrize("tag", _lang_vectors("language_syntax_invalid.txt") + ["i", "sl-rozaj-rozaj"])
def test_invalid_language_vectors(tag):
    from consentvid.validate import is_language

    assert not is_language(tag)
    assert validate_in_repo(V, video_record(language=tag), ALICE)


def test_cli_lowercases_the_primary_language_subtag():
    from consentvid import cli

    assert cli._normalise_language(None, None, "EN-GB") == "en-GB"
    assert cli._normalise_language(None, None, "") == ""


# ------------------------- R2-21: integers stay within 53 bits


@pytest.mark.parametrize("field", ["durationMs", "width", "height"])
def test_record_integers_fit_53_bits(field):
    assert not validate_in_repo(V, video_record(**{field: 2**53 - 1}), ALICE)
    errors = validate_in_repo(V, video_record(**{field: 2**53}), ALICE)
    assert any("exceeds 2^53-1" in e for e in errors)


@pytest.mark.parametrize("dur", ["inf", "nan", "-5", "1e300"])
def test_probe_drops_out_of_range_durations(dur, monkeypatch, tmp_path):
    import json as _json
    import subprocess

    from consentvid import media

    out = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": dur, "tags": {}},
           "streams": [{"codec_type": "video", "codec_name": "h264", "width": 2**60, "height": 720}]}
    monkeypatch.setattr(media, "have_ffprobe", lambda: True)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=_json.dumps(out), stderr="")
    )
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    p = media.probe(f)
    fields = p.record_fields()
    assert "durationMs" not in fields and "width" not in fields and fields["height"] == 720


# ------------------------- R2-23: open unions may gain refs


def _union_lex(refs, closed=None):
    u = {"type": "union", "refs": refs}
    if closed is not None:
        u["closed"] = closed
    return {"lexicon": 1, "id": "x.y.z", "defs": {"main": {"type": "object", "properties": {"u": u}}}}


@pytest.mark.parametrize(
    "old, new, ok",
    [
        (_union_lex(["#a"]), _union_lex(["#a", "#b"]), True),
        (_union_lex(["#a"], closed=True), _union_lex(["#a", "#b"], closed=True), False),
        (_union_lex(["#a"]), _union_lex(["#a"], closed=True), False),
        (_union_lex(["#a"], closed=True), _union_lex(["#a"]), False),
        (_union_lex(["#a", "#b"]), _union_lex(["#a"]), False),
        (_union_lex(["#a"], closed=False), _union_lex(["#a", "#b"]), True),
    ],
)
def test_union_evolution(old, new, ok):
    from consentvid.lexicons import compat_errors

    assert (compat_errors(old, new) == []) is ok


@pytest.mark.parametrize("handle", ["alice.local", "x.onion", "a.example", "bob.invalid", "c.test", "d.alt"])
def test_reserved_tld_handles_never_resolve(handle):
    from consentvid.errors import ConsentVidError
    from consentvid.identity import resolve_did

    with pytest.raises(ConsentVidError, match="reserved"):
        resolve_did(handle)


# ------------------------- R1-16 / R1-17: lexicon evolution and contract


def _published(nsid):
    import json as _json
    from pathlib import Path

    return _json.loads((Path(__file__).parent / "fixtures" / "published_lexicons" / f"{nsid}.json").read_text())


@pytest.mark.parametrize("nsid", [C, V, "org.consentvid.consentV2", "org.consentvid.videoV2"])
def test_local_lexicons_evolve_compatibly_from_the_published_ones(nsid):
    from consentvid.lexicons import compat_errors, load_lexicon

    assert compat_errors(_published(nsid), load_lexicon(nsid)) == []


def test_compat_errors_catch_breaking_changes():
    import copy

    from consentvid.lexicons import compat_errors, load_lexicon

    old = load_lexicon(C)
    props = lambda d: d["defs"]["main"]["record"]["properties"]  # noqa: E731

    new = copy.deepcopy(old)
    props(new)["compensation"]["maxLength"] = 10
    assert any("maxLength added" in e for e in compat_errors(old, new))
    new = copy.deepcopy(old)
    props(new)["revocable"]["type"] = "string"
    assert any("type changed" in e for e in compat_errors(old, new))
    new = copy.deepcopy(old)
    props(new)["compensation"]["knownValues"].remove("monetary")
    assert any("'monetary' removed" in e for e in compat_errors(old, new))
    new = copy.deepcopy(old)
    new["defs"]["main"]["record"]["required"].append("peoplePresent")
    assert any("added to required" in e for e in compat_errors(old, new))
    new = copy.deepcopy(old)
    del props(new)["rightsBasis"]
    assert any("property removed" in e for e in compat_errors(old, new))
    new = copy.deepcopy(old)
    new["defs"]["main"]["key"] = "any"
    assert any("record key type changed" in e for e in compat_errors(old, new))
    # Compatible: descriptions, new optional properties, new knownValues.
    new = copy.deepcopy(old)
    new["defs"]["main"]["description"] = "changed"
    props(new)["newField"] = {"type": "string"}
    props(new)["compensation"]["knownValues"].append("other")
    assert compat_errors(old, new) == []


def test_mime_type_known_values_are_the_reader_allowlist():
    from consentvid.lexicons import load_lexicon
    from consentvid.records import VIDEO_MIME_TYPES

    mime = load_lexicon(V)["defs"]["main"]["record"]["properties"]["mimeType"]
    assert mime["knownValues"] == sorted(VIDEO_MIME_TYPES)


def test_consent_lexicon_documents_the_sidecar_rule():
    from consentvid.lexicons import load_lexicon

    main = load_lexicon(C)["defs"]["main"]
    assert main["key"] == "tid" and "MUST equal the record key" in main["description"]


# ------------------------- R2-24: the descriptions state the readers' rules


def test_lexicon_descriptions_state_reader_rules():
    from consentvid.lexicons import load_lexicon

    consent = load_lexicon("org.consentvid.consent")["defs"]["main"]["description"]
    assert "peoplePresent is one of its knownValues (readers reject other values)" in consent
    video = load_lexicon("org.consentvid.video")
    sr = video["defs"]["storageRef"]
    assert sr.get("description") and sr["properties"]["kind"].get("description")
    for kind in ("'url'", "'r2'", "'peertube'", "'ipfs'", "'local'"):
        assert kind in sr["properties"]["kind"]["description"]
    props = video["defs"]["main"]["record"]["properties"]
    assert props["title"].get("description")
    for f in ("durationMs", "width", "height"):
        assert "2^53-1" in props[f]["description"]
    assert "same record key" in video["defs"]["main"]["description"]
    # R3-03 / R3-07: every rule validate.py enforces is stated for other writers.
    cprops = load_lexicon(C)["defs"]["main"]["record"]["properties"]
    assert "unless videoCid is present" in consent
    assert "readers treat a consent without it as invalid" in cprops["videoCid"]["description"]
    assert "must not contain control or format characters" in consent
    for f in ("modalities", "purposes", "modelClasses"):
        assert "No duplicate values" in cprops[f]["description"], f
    assert "Must be later than createdAt" in cprops["expiresAt"]["description"]
    assert "64 lowercase hex characters" in props["contentHash"]["description"]
    assert "lowercase primary subtag" in props["language"]["description"]
    assert "readers reject other values" in props["language"]["description"]


def test_consent_without_video_cid_is_invalid():
    """R3-03: an unpinned consent would follow any rewrite of its video."""
    unpinned = consent_record()
    del unpinned["videoCid"]
    assert validate_record(C, unpinned) == ["videoCid is required: a consent must pin the exact video version"]
    assert "videoCid is required: a consent must pin the exact video version" in validate_in_repo(
        C, unpinned, ALICE, rkey=K1
    )


@pytest.mark.parametrize("mime", ["video/matroska", "video/vnd.avi", "video/x-matroska", "video/x-msvideo"])
def test_sniffed_and_legacy_video_mime_names_validate(mime):
    """R3-15: a PDS's file-type sniffing reports video/matroska and
    video/vnd.avi; readers accept them alongside the x- names."""
    assert validate_record(V, video_record(mimeType=mime)) == []


def test_writers_keep_the_x_mime_names():
    from consentvid.records import mime_for

    assert mime_for("a.mkv") == "video/x-matroska" and mime_for("a.avi") == "video/x-msvideo"


def test_raw_cid_from_sha256_matches_a_real_blob_cid():
    """R2-27: contentHash <-> blob CID (digest taken from the CID itself)."""
    import libipld

    from consentvid.records import raw_cid_from_sha256

    blob = "bafkreihwihm6kpd6zuwhhlro75p5qks5qtrcu55jp3gddbfjsieiv7wuka"
    info = libipld.decode_cid(blob)
    assert info["codec"] == 0x55 and info["hash"]["code"] == 0x12
    assert raw_cid_from_sha256("sha256-" + info["hash"]["digest"].hex()) == blob
    with pytest.raises(ValueError):
        raw_cid_from_sha256("sha256-abcd")
