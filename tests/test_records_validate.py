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
