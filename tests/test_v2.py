"""V2 record formats, account signals, self-labels, rollback witness and
Jetstream failover."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import (
    ALICE,
    BOB,
    HASH,
    K1,
    K2,
    V2_HASH,
    VIDEO_CID,
    consent_record,
    consent_v2_record,
    video_record,
    video_v2_cid,
    video_v2_record,
)
from consentvid import appview as av
from consentvid import cli, signals, versions
from consentvid import resync as rs
from consentvid.records import CONSENT_V2_NSID, VIDEO_V2_NSID, compute_cid, raw_cid_from_sha256
from consentvid.validate import validate_in_repo, validate_record, write_policy_errors
from consentvid.verify import VerifiedRecord

V2, C2 = VIDEO_V2_NSID, CONSENT_V2_NSID
V1, C1 = "org.consentvid.video", "org.consentvid.consent"


# ------------------------------------------------------------ validation ---


def test_valid_v2_pair():
    assert validate_in_repo(V2, video_v2_record(), ALICE, rkey=K1) == []
    assert validate_in_repo(C2, consent_v2_record(), ALICE, rkey=K1) == []


@pytest.mark.parametrize(
    "edit,needle",
    [
        ({"contentHash": HASH}, "not a CID"),  # V1 spelling
        ({"contentHash": VIDEO_CID}, "raw codec"),  # a record CID, not the bytes'
        ({"storage": {"url": "https://example.org/x"}}, "no $type"),
        ({"storage": {"$type": f"{V2}#r2Ref", "url": "http://example.org/x"}}, "https"),
        ({"storage": {"$type": f"{V2}#r2Ref"}}, "missing required field: storage.url"),
        ({"storage": {"$type": f"{V2}#ipfsRef", "cid": "../etc"}}, "CID"),
        ({"storage": {"$type": f"{V2}#localRef", "name": "a\x07b"}}, "control"),
        ({"langs": ["en", "EN-gb", "cs", "de"]}, "maxLength 3"),
        ({"langs": ["xx_yy"]}, "BCP-47"),
        ({"tags": ["a"] * 9}, "maxLength 8"),
        ({"tags": ["x" * 65]}, "maxGraphemes 64"),
        ({"durationMs": -1}, "below minimum 0"),
        ({"size": 0}, "below minimum 1"),
        ({"width": 2**53}, "above maximum"),
        ({"mimeType": "git+https"}, "not a video container type"),
        ({"labels": {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "x" * 129}]}}, "maxLength 128"),
        ({"labels": {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "a‮b"}]}}, "labels"),
        ({"title": ""}, "minLength"),
    ],
)
def test_invalid_v2_videos(edit, needle):
    errs = validate_record(V2, video_v2_record(**edit))
    assert any(needle in e for e in errs), errs


def test_unknown_storage_type_is_accepted_but_unreachable():
    """Open union: a member type we don't know validates, but its bytes are
    opaque to us (never dereferenced)."""
    from consentvid.storage import public_url

    rec = video_v2_record(storage={"$type": "com.example.s3#ref", "bucket": "b"})
    assert validate_record(V2, rec) == []
    ref = versions.video_view(V2, rec)["storageRef"]
    assert ref == {"kind": "com.example.s3#ref", "value": ""} and public_url(ref) is None


def test_closed_union_rejects_unknown_member():
    from consentvid.validate import _Ctx, _validate_union

    errs = _validate_union({"refs": ["#a"], "closed": True}, {"$type": "x#b"}, _Ctx("x", {"a": {}}), "f")
    assert errs and "not one of" in errs[0]


@pytest.mark.parametrize(
    "edit,needle",
    [
        ({"subject": {"uri": f"at://{ALICE}/{V1}/{K1}", "cid": VIDEO_CID}}, "expected a org.consentvid.videoV2"),
        ({"subject": {"uri": f"at://{ALICE}/{V2}/{K1}"}}, "missing required field: subject.cid"),
        ({"subject": {"uri": f"at://{ALICE}/{V2}/{K1}", "cid": V2_HASH}}, "subject.cid must be the CID of a record"),
        ({"peoplePresent": "onlyMe"}, "peoplePresent must be one of only-me"),
        ({"peoplePresent": "no-identifiable-people"}, "contradict"),
        ({"adultAttested": False}, "adultAttested"),
        ({"authorshipAttested": False}, "authorshipAttested"),
        ({"purposes": ["training", "training"]}, "duplicates"),
        ({"modelClasses": []}, "minLength"),
        ({"compensation": "x" * 65}, "maxLength 64"),
        ({"modalities": ["video‮"]}, "control or format"),
        ({"expiresAt": "2025-01-01T00:00:00Z"}, "expiresAt must be after createdAt"),
    ],
)
def test_invalid_v2_consents(edit, needle):
    rec = consent_v2_record(**edit)
    if "peoplePresent" in edit and edit["peoplePresent"] == "no-identifiable-people":
        rec["modalities"] = ["video", "face"]
    errs = validate_in_repo(C2, rec, ALICE, rkey=K1)
    assert any(needle in e for e in errs), errs


def test_v2_consent_missing_attestations_and_people_is_invalid():
    rec = consent_v2_record()
    for k in ("adultAttested", "peoplePresent", "subject"):
        r = {**rec}
        del r[k]
        assert any(f"missing required field: {k}" in e for e in validate_record(C2, r))


def test_v2_sidecar_and_ownership_rules():
    other_key = consent_v2_record(rkey=K2)
    assert any("sidecar" in e for e in validate_in_repo(C2, other_key, ALICE, rkey=K1))
    foreign = consent_v2_record(did=BOB)
    assert any("only reference the writer's own video" in e for e in validate_in_repo(C2, foreign, ALICE, rkey=K1))


def test_v1_consent_cannot_bind_a_v2_video():
    rec = consent_record(videoUri=f"at://{ALICE}/{V2}/{K1}")
    assert any("expected a org.consentvid.video record" in e for e in validate_in_repo(C1, rec, ALICE, rkey=K1))


def test_write_policy_refuses_paths_in_v2_local_names():
    rec = video_v2_record(storage={"$type": f"{V2}#localRef", "name": "/Users/me/clip.mp4"})
    assert write_policy_errors(V2, rec)


def test_format_uri():
    from consentvid.validate import _validate_string

    assert _validate_string({"format": "uri"}, "https://x.example/a", "p") == []
    assert _validate_string({"format": "uri"}, "not a uri", "p")
    assert _validate_string({"format": "uri"}, "https://" + "a" * 9000, "p")


# ------------------------------------------------------------- versions ----


def test_vocabulary_round_trips():
    for v in ("openWeights", "creditOnly", "ownRecording", "otherRightsHolder", "selfAttested",
              "onlyMe", "allConsented", "noIdentifiablePeople", "training", "academic"):
        assert versions.kebab_to_camel(versions.camel_to_kebab(v)) == v
    assert versions.camel_to_kebab("openWeights") == "open-weights"


def test_view_and_record_round_trip():
    v = video_v2_record(
        description="d", size=5, langs=["en", "cs"], tags=["a"], mimeType="video/mp4", durationMs=3,
        labels={"$type": versions.SELF_LABELS_TYPE, "values": [{"val": "nudity"}]},
    )
    view = versions.video_view(V2, v)
    assert view["contentHash"] == HASH and view["language"] == "en" and view["labels"] == ["nudity"]
    assert versions.video_record_v2(view) == v
    c = consent_v2_record(expiresAt="2030-01-01T00:00:00.000Z")
    cv = versions.consent_view(C2, c)
    assert cv["rightsBasis"] == "ownRecording" and cv["revocable"] is True
    assert cv["videoUri"] == c["subject"]["uri"] and cv["videoCid"] == c["subject"]["cid"]
    assert versions.consent_record_v2(cv) == c


def test_v2_content_hash_is_the_blob_cid():
    assert V2_HASH == raw_cid_from_sha256(HASH)
    assert versions.content_hash_of_cid(VIDEO_CID) is None  # dag-cbor, not raw


# -------------------------------------------------------------- appview ----


def _commit(did, coll, rkey, op, record=None, cid="bafyx"):
    return {"kind": "commit", "did": did, "time_us": 1,
            "commit": {"operation": op, "collection": coll, "rkey": rkey, "cid": cid, "record": record}}


def _index_v2(conn, video=None, consent=None):
    video = video or video_v2_record()
    av.handle_event(conn, _commit(ALICE, V2, K1, "create", video, cid=compute_cid(video)))
    av.handle_event(conn, _commit(ALICE, C2, K1, "create", consent or consent_v2_record(video_cid=compute_cid(video)),
                                  cid="bafyc2"))
    conn.commit()


def test_appview_indexes_v2_and_stores_raw_records():
    conn = av.connect()
    _index_v2(conn)
    (row,) = av.query_consents(conn)
    assert row["uri"] == f"at://{ALICE}/{C2}/{K1}" and json.loads(row["model_classes"]) == ["academic"]
    assert json.loads(row["record"])["$type"] == C2 and json.loads(row["video_record"])["$type"] == V2
    assert row["rights_basis"] == "ownRecording"  # internal vocabulary in the columns


def test_appview_v2_consent_for_v1_video_is_invalid():
    conn = av.connect()
    av.handle_event(conn, _commit(ALICE, V1, K1, "create", video_record(), cid=VIDEO_CID))
    bad = consent_v2_record(**{"subject": {"uri": f"at://{ALICE}/{V1}/{K1}", "cid": VIDEO_CID}})
    assert av.handle_event(conn, _commit(ALICE, C2, K1, "create", bad))[0] == "invalid"
    conn.commit()
    assert av.query_consents(conn) == []


def test_appview_leaves_out_self_labeled_videos_unless_asked():
    conn = av.connect()
    video = video_v2_record(labels={"$type": versions.SELF_LABELS_TYPE, "values": [{"val": "graphic-media"}]})
    _index_v2(conn, video=video)
    assert av.query_consents(conn) == []
    assert len(av.query_consents(conn, include_labeled=True)) == 1


def test_appview_v2_delete_revokes():
    conn = av.connect()
    _index_v2(conn)
    av.handle_event(conn, _commit(ALICE, C2, K1, "delete"))
    conn.commit()
    assert av.query_consents(conn) == [] and [r["uri"] for r in av.revocations(conn)] == [f"at://{ALICE}/{C2}/{K1}"]


def test_signal_gate_hides_unchecked_accounts_and_queues_them(monkeypatch):
    monkeypatch.setattr(av, "REQUIRE_SIGNAL_CHECK", True)
    conn = av.connect()
    _index_v2(conn)
    assert av.query_consents(conn) == []
    assert av.pending_dids(conn) == [ALICE]
    av.set_account_signals(conn, ALICE, False, False)
    conn.commit()
    assert len(av.query_consents(conn)) == 1


@pytest.mark.parametrize("bot,denied,purposes,visible", [
    (True, False, ["training"], False),
    (False, True, ["training"], False),
    (False, True, ["evaluation"], True),  # the preference only covers training
    (False, False, ["training"], True),
])
def test_signal_flags_filter_queries(monkeypatch, bot, denied, purposes, visible):
    monkeypatch.setattr(av, "REQUIRE_SIGNAL_CHECK", True)
    conn = av.connect()
    video = video_v2_record()
    _index_v2(conn, video, consent_v2_record(video_cid=compute_cid(video), purposes=purposes))
    av.set_account_signals(conn, ALICE, bot, denied)
    conn.commit()
    assert bool(av.query_consents(conn)) is visible


def test_live_signal_events_flag_at_once_and_queue_a_reread():
    conn = av.connect()
    _index_v2(conn)
    av.set_account_signals(conn, ALICE, False, False)
    profile = {"$type": "app.bsky.actor.profile",
               "labels": {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "bot"}]}}
    op, msg = av.handle_event(conn, _commit(ALICE, "app.bsky.actor.profile", "self", "update", profile))
    conn.commit()
    assert op == "signal" and "bot self-label" in msg
    assert av.query_consents(conn) == [] and ALICE in av.pending_dids(conn)
    # A verified read that started before the live flag doesn't clear it.
    av.set_account_signals(conn, ALICE, False, False, checked_at="2000-01-01T00:00:00.000Z")
    conn.commit()
    assert av.query_consents(conn) == []
    # One that started after it does. (Same millisecond counts as before:
    # such a read may not have seen the flag.)
    from datetime import UTC, datetime, timedelta

    from consentvid.records import format_dt

    av.set_account_signals(conn, ALICE, False, False, checked_at=format_dt(datetime.now(UTC) + timedelta(seconds=1)))
    conn.commit()
    assert len(av.query_consents(conn)) == 1


def test_live_preference_deny_flags_training_consents():
    conn = av.connect()
    _index_v2(conn)
    av.set_account_signals(conn, ALICE, False, False)
    pref = {"$type": signals.AI_PREF_NSID, "scope": {"$type": f"{signals.AI_PREF_NSID}#globalScope"},
            "preferences": {"training": {"allow": False, "updatedAt": "2026-01-01T00:00:00Z"}},
            "updatedAt": "2026-01-01T00:00:00Z"}
    op, msg = av.handle_event(conn, _commit(ALICE, signals.AI_PREF_NSID, "self", "create", pref))
    conn.commit()
    assert "AI training denied" in msg and av.query_consents(conn) == []


def test_signal_events_of_strangers_are_ignored():
    conn = av.connect()
    assert av.handle_event(conn, _commit(BOB, "app.bsky.actor.profile", "self", "update", {})) is None
    assert av.pending_dids(conn) == []


def test_migration_queues_contributors_for_a_signal_check(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = av.connect(path)
    av.handle_event(conn, _commit(ALICE, V1, K1, "create", video_record(), cid=VIDEO_CID))
    av.handle_event(conn, _commit(ALICE, C1, K1, "create", consent_record()))
    conn.commit()
    conn.execute("DELETE FROM pending_backfill")
    conn.commit()
    conn.close()
    raw = sqlite3.connect(path)  # simulate an index from before the signal columns
    for col in ("bot", "training_denied", "signals_at", "flagged_at"):
        raw.execute(f"ALTER TABLE accounts DROP COLUMN {col}")
    raw.commit()
    raw.close()
    assert av.pending_dids(av.connect(path)) == [ALICE]


# ------------------------------------------------------------- backfill ----


def test_backfill_reads_both_versions_and_records_signals(monkeypatch):
    from consentvid import identity

    video = video_v2_record()
    vcid = compute_cid(video)
    listing = {
        V2: [SimpleNamespace(uri=f"at://{ALICE}/{V2}/{K1}", cid=vcid, value=video)],
        C2: [SimpleNamespace(uri=f"at://{ALICE}/{C2}/{K1}", cid="bafyc", value=consent_v2_record(video_cid=vcid))],
        V1: [SimpleNamespace(uri=f"at://{ALICE}/{V1}/{K2}", cid=VIDEO_CID, value=video_record())],
    }
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(True, None), (True, None)])
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: listing.get(coll, []))
    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(training_denied=True, detail="x"))
    monkeypatch.setattr(av, "REQUIRE_SIGNAL_CHECK", True)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert r.exit_code == 0 and "3 record(s) indexed" in r.output, r.output
    conn = av.connect()
    row = conn.execute("SELECT training_denied, signals_at FROM accounts WHERE did = ?", (ALICE,)).fetchone()
    assert row["training_denied"] == 1 and row["signals_at"]
    assert av.query_consents(conn) == []  # the consent grants training
    # A second backfill sees the V1 video gone, but keeps the V2 one.
    listing[V1] = []
    CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    uris = {r["uri"] for r in av.connect().execute("SELECT uri FROM videos")}
    assert uris == {f"at://{ALICE}/{V2}/{K1}"}


def test_backfill_fails_closed_when_signals_cant_be_read(monkeypatch):
    from consentvid import identity

    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(cli, "_repo_statuses", lambda did, relay: [(True, None), (True, None)])
    monkeypatch.setattr(cli, "_list_all", lambda client, did, coll: [])

    def boom(did):
        raise ConnectionError("PDS down")

    monkeypatch.setattr(signals, "_lookup", boom)
    r = CliRunner().invoke(cli.main, ["appview", "backfill", "alice.test"])
    assert "1 failed" in r.output


# -------------------------------------------------------------- signals ----


def _pref(scope, allow, rkey="self"):
    t = {"global": "#globalScope", "collection": "#collectionScope", "entity": "#entityScope"}[scope[0]]
    sc = {"$type": f"{signals.AI_PREF_NSID}{t}"}
    if scope[0] == "collection":
        sc["collection"] = scope[1]
    if scope[0] == "entity":
        sc["entity"] = scope[1]
    return {"$type": signals.AI_PREF_NSID, "scope": sc, "updatedAt": "2026-01-01T00:00:00Z",
            "preferences": {"training": {"allow": allow, "updatedAt": "2026-01-01T00:00:00Z"}}}


@pytest.mark.parametrize("global_allow,overrides,denied", [
    (None, [], False),
    (False, [], True),
    (True, [], False),
    (False, [(("collection", V2), True)], False),  # the more specific statement wins
    (True, [(("collection", C2), False)], True),
    (True, [(("collection", V2), True), (("collection", C2), False)], True),  # a deny wins among overrides
    (True, [(("collection", "app.bsky.feed.post"), False)], False),  # not ours
    (True, [(("entity", "did:web:ai.example"), False)], False),  # consumer-specific: not applied
])
def test_training_preference_precedence(global_allow, overrides, denied):
    g = _pref(("global", None), global_allow) if global_allow is not None else None
    got, _ = signals.decide_training(g, [_pref(s, a) for s, a in overrides])
    assert got is denied


def test_bot_profile_detection():
    lab = lambda *v: {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": x} for x in v]}  # noqa: E731
    assert signals.is_bot_profile({"labels": lab("bot")})
    assert not signals.is_bot_profile({"labels": lab("porn")})
    assert not signals.is_bot_profile({"labels": {"values": [{"val": "bot"}]}})  # no $type
    assert not signals.is_bot_profile(None)


def test_lookup_reads_self_records_with_proofs_and_overrides(monkeypatch):
    from consentvid import identity, verify

    records = {
        f"at://{ALICE}/app.bsky.actor.profile/self": {"$type": "app.bsky.actor.profile"},
        f"at://{ALICE}/{signals.AI_PREF_NSID}/self": _pref(("global", None), True),
        f"at://{ALICE}/{signals.AI_PREF_NSID}/{K1}": _pref(("collection", C2), False),
    }
    fetched = []

    def fetch(uri):
        fetched.append(uri)
        v = records.get(uri)
        return VerifiedRecord(uri, v is not None, "bafyx" if v else None, v, "3maaaaaaaaaz2", True)

    monkeypatch.setattr(verify, "fetch_verified", fetch)
    monkeypatch.setattr(identity, "public_client", lambda did: None)
    monkeypatch.setattr(identity, "list_all", lambda client, did, coll: [
        SimpleNamespace(uri=f"at://{ALICE}/{coll}/self", value=records[f"at://{ALICE}/{coll}/self"]),
        SimpleNamespace(uri=f"at://{ALICE}/{coll}/{K1}", value=records[f"at://{ALICE}/{coll}/{K1}"]),
        SimpleNamespace(uri=f"at://{ALICE}/{coll}/{K2}", value=_pref(("collection", "app.bsky.feed.post"), False)),
    ])
    got = signals._lookup_impl(ALICE)
    assert got.training_denied and not got.bot
    assert f"at://{ALICE}/{signals.AI_PREF_NSID}/{K1}" in fetched  # the override was read with a proof
    assert f"at://{ALICE}/{signals.AI_PREF_NSID}/{K2}" not in fetched  # not one of ours


def test_account_signals_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(signals, "_lookup", lambda did: calls.append(did) or signals.NONE)
    signals.account_signals(ALICE)
    signals.account_signals(ALICE)
    signals.account_signals(ALICE, fresh=True)
    assert calls == [ALICE, ALICE]


# --------------------------------------------------------------- resync ----


CURI2 = f"at://{ALICE}/{C2}/{K1}"
VURI2 = f"at://{ALICE}/{V2}/{K1}"


def _v2_item_and_fetch(video=None, consent=None, rev="r", consent_rev=None):
    from consentvid.croissant import Clip, _file_object, build_manifest

    video = video or video_v2_record()
    vcid = compute_cid(video)
    consent = consent or consent_v2_record(video_cid=vcid)
    clip = Clip(CURI2, "bafyc2", versions.consent_view(C2, consent), VURI2, vcid, versions.video_view(V2, video), ALICE)
    m, _ = build_manifest([clip], name="t")
    m["distribution"] = [_file_object(clip)]
    (item,) = rs.items_from_manifest(m)

    def fetch(at):
        uri = str(at)
        if uri == CURI2:
            return VerifiedRecord(uri, True, "bafyc2", consent, consent_rev or rev, True)
        if uri == VURI2:
            return VerifiedRecord(uri, True, vcid, video, rev, True)
        raise AssertionError(uri)

    return item, fetch


def test_resync_accepts_a_v2_clip():
    item, fetch = _v2_item_and_fetch()
    res = rs.check(item, fetch=fetch, relay_status=False)
    assert res.status == rs.VALID, res.reason
    assert item.receipt["rightsBasis"] == "ownRecording"  # manifests use the internal vocabulary


def test_resync_labeled_clip_is_dropped_unless_included():
    video = video_v2_record(labels={"$type": versions.SELF_LABELS_TYPE, "values": [{"val": "porn"}]})
    item, fetch = _v2_item_and_fetch(video=video)
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.LABELED
    assert rs.check(item, fetch=fetch, relay_status=False, include_labeled=True).status == rs.VALID


@pytest.mark.parametrize("sig,status", [
    (signals.AccountSignals(bot=True), rs.BOT_ACCOUNT),
    (signals.AccountSignals(training_denied=True, detail="account-wide default"), rs.CONFLICTING_PREFERENCE),
])
def test_resync_account_signals(monkeypatch, sig, status):
    monkeypatch.setattr(signals, "_lookup", lambda did: sig)
    item, fetch = _v2_item_and_fetch()
    res = rs.check(item, fetch=fetch, relay_status=False)
    assert res.status == status and status in rs.DROP_STATUSES


def test_resync_signal_lookup_failure_is_unknown(monkeypatch):
    def boom(did):
        raise ConnectionError("down")

    monkeypatch.setattr(signals, "_lookup", boom)
    item, fetch = _v2_item_and_fetch()
    assert rs.check(item, fetch=fetch, relay_status=False).status == rs.UNKNOWN


def test_resync_stale_pds_is_dropped(monkeypatch):
    monkeypatch.setattr(rs, "_witness_lookup", lambda did: "3mzzzzzzzzzz2")
    item, fetch = _v2_item_and_fetch(rev="3maaaaaaaaaz2")
    res = rs.check(item, fetch=fetch)
    assert res.status == rs.STALE and res.verified


def test_resync_witness_older_than_pds_is_fine_and_missing_witness_is_a_note(monkeypatch):
    monkeypatch.setattr(rs, "_witness_lookup", lambda did: "3maaaaaaaaaa2")
    item, fetch = _v2_item_and_fetch(rev="3maaaaaaaaaz2")
    assert rs.check(item, fetch=fetch).status == rs.VALID
    rs.clear_witness_cache()
    monkeypatch.setattr(rs, "_witness_lookup", lambda did: None)
    res = rs.check(item, fetch=fetch)
    assert res.status == rs.VALID and "rollback not checked" in res.reason


def test_witness_is_read_before_the_pds(monkeypatch):
    order = []
    monkeypatch.setattr(rs, "_witness_lookup", lambda did: order.append("relay") or None)
    item, fetch = _v2_item_and_fetch()
    rs.check(item, fetch=lambda at: order.append("pds") or fetch(at))
    assert order[:2] == ["relay", "pds"]


def test_relay_latest_rev(monkeypatch):
    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (200, {"cid": "x", "rev": "3maaaaaaaaaz2"}))
    assert av.relay_latest_rev("https://relay.example", ALICE) == "3maaaaaaaaaz2"
    monkeypatch.setattr(av, "_relay_get", lambda relay, path, params: (200, {"rev": "not-a-tid"}))
    assert av.relay_latest_rev("https://relay.example", ALICE) is None


def test_far_future_revs_are_refused():
    from consentvid.records import _encode_tid
    from consentvid.verify import MAX_REV_SKEW_US, rev_is_far_future

    now = 1_800_000_000_000_000
    soon = _encode_tid((now + MAX_REV_SKEW_US - 1) << 10)
    later = _encode_tid((now + MAX_REV_SKEW_US + 1) << 10)
    assert not rev_is_far_future(soon, now) and rev_is_far_future(later, now)


# ------------------------------------------------------------------ cli ----


def test_contribute_refuses_bot_and_preference_conflicts(fake_client, tmp_path, monkeypatch):
    from test_cli import _contribute

    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(bot=True))
    r = _contribute(fake_client, tmp_path, monkeypatch)
    assert r.exit_code != 0 and "bots cannot give consent" in r.output and not fake_client.repo.records
    signals.clear_cache()
    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(training_denied=True, detail="d"))
    r = _contribute(fake_client, tmp_path, monkeypatch)
    assert r.exit_code != 0 and "denies AI training" in r.output and not fake_client.repo.records


def test_contribute_fails_closed_when_signals_cant_be_read(fake_client, tmp_path, monkeypatch):
    from test_cli import _contribute

    def boom(did):
        raise ConnectionError("down")

    monkeypatch.setattr(signals, "_lookup", boom)
    r = _contribute(fake_client, tmp_path, monkeypatch)
    assert r.exit_code != 0 and "nothing was written" in r.output and not fake_client.repo.records


def test_contribute_writes_self_labels(fake_client, tmp_path, monkeypatch):
    from test_cli import _fake_media, _upload, run

    _fake_media(monkeypatch)
    monkeypatch.setattr("consentvid.storage.upload", _upload())
    monkeypatch.setattr(cli, "get_client", lambda: fake_client)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"bytes")
    r = run("register-video", str(f), "--title", "t", "--storage-kind", "r2", "--self-label", "nudity")
    assert r.exit_code == 0, r.output
    ((_, video),) = [v for k, v in fake_client.repo.records.items() if k[0] == V2]
    assert video["labels"] == {"$type": versions.SELF_LABELS_TYPE, "values": [{"val": "nudity"}]}


def test_revoke_a_v2_video_deletes_its_v2_consent(fake_client, monkeypatch):
    from test_cli import run

    fake_client.repo.records[(V2, K1)] = (video_v2_cid(), video_v2_record())
    fake_client.repo.records[(C2, K1)] = ("bafyc", consent_v2_record())
    fake_client.repo.records[(C1, K1)] = ("bafyv1", consent_record())  # another version: untouched
    monkeypatch.setattr(cli, "get_client", lambda: fake_client)
    r = run("revoke", f"at://{ALICE}/{V2}/{K1}", "-y")
    assert r.exit_code == 0, r.output
    assert set(fake_client.repo.records) == {(C1, K1)}


def test_export_reads_both_versions_and_skips_labels_and_conflicts(fake_client, tmp_path, monkeypatch):
    from consentvid import identity
    from test_cli import run

    labeled = video_v2_record(labels={"$type": versions.SELF_LABELS_TYPE, "values": [{"val": "sexual"}]})
    fake_client.repo.records[(V2, K1)] = (video_v2_cid(), video_v2_record())
    fake_client.repo.records[(C2, K1)] = ("bafyc", consent_v2_record())
    fake_client.repo.records[(V2, K2)] = (compute_cid(labeled), labeled)
    fake_client.repo.records[(C2, K2)] = ("bafyd", consent_v2_record(rkey=K2, video_cid=compute_cid(labeled)))
    fake_client.repo.records[(V1, "3maaaaaaaaad2")] = (VIDEO_CID, video_record())
    fake_client.repo.records[(C1, "3maaaaaaaaad2")] = ("bafye", consent_record(rkey="3maaaaaaaaad2"))
    monkeypatch.setattr(identity, "resolve_did", lambda r: ALICE)
    monkeypatch.setattr(identity, "public_client", lambda did: fake_client)
    monkeypatch.setattr(identity, "display_name", lambda did: "alice.test")
    out = tmp_path / "m.json"
    r = run("export", str(out), "--repo", "alice.test", "--model-class", "academic")
    assert r.exit_code == 0, r.output
    ids = {fo["@id"] for fo in json.loads(out.read_text())["distribution"]}
    assert ids == {f"{ALICE}/{K1}", f"{ALICE}/3maaaaaaaaad2"} and "self-labeled sexual" in r.output
    r = run("export", str(out), "--repo", "alice.test", "--include-labeled")
    assert len(json.loads(out.read_text())["distribution"]) == 3
    signals.clear_cache()
    monkeypatch.setattr(signals, "_lookup", lambda did: signals.AccountSignals(bot=True))
    r = run("export", str(out), "--repo", "alice.test")
    assert r.exit_code != 0 and "bot" in r.output


def test_filters_take_either_spelling():
    assert cli._camel_values(None, None, ("open-weights", "openWeights")) == ("openWeights", "openWeights")


# --------------------------------------------------------------- failover --


def test_jetstream_defaults_list_two_instances():
    assert [u.split("/")[2] for u in av.DEFAULT_JETSTREAMS] == [
        "jetstream.us-east.bsky.network", "jetstream.us-west.bsky.network"]
    assert av.jetstream_urls() == list(av.JETSTREAM_URLS)


def test_index_fails_over_and_resumes_by_time(tmp_path, monkeypatch):
    import websockets

    from test_appview import _Socket, v2_commit

    conn = av.connect(tmp_path / "a.db")
    recent = av._now_us() - 60_000_000
    av._set_meta(conn, av.SEQ_KEY, 42)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-east.bsky.network")
    av.set_cursor(conn, recent)
    conn.commit()
    calls, events = [], []

    def connect(url, **kw):
        calls.append(url)
        if "us-east" in url:
            raise websockets.exceptions.InvalidHandshake("down")
        return _Socket([v2_commit(ALICE, V1, K1, "create", video_record(), seq=7)])

    async def no_sleep(s):
        return None

    monkeypatch.setattr(av.websockets, "connect", connect)
    monkeypatch.setattr(av.asyncio, "sleep", no_sleep)
    n = asyncio.run(av.index_loop(conn, on_event=lambda op, x: events.append((op, x)), stop_after=1))
    assert n == 1
    east = [u for u in calls if "us-east" in u]
    west = [u for u in calls if "us-west" in u]
    assert len(east) == av.FAILOVER_AFTER and east[0].endswith("&cursor=42")  # exact seq on its own host
    assert west[0].endswith(f"&cursor={recent - av.CURSOR_REWIND_US}")  # time on the other one
    assert ("failover", "switching to Jetstream jetstream.us-west.bsky.network") in events
    assert av._meta(conn, av.HOST_KEY) == "jetstream.us-west.bsky.network" and av.get_seq(conn) == 7


def test_index_starts_on_the_instance_its_seq_belongs_to(tmp_path, monkeypatch):
    from test_appview import _Socket, v2_commit

    conn = av.connect(tmp_path / "a.db")
    av._set_meta(conn, av.SEQ_KEY, 42)
    av._set_meta(conn, av.HOST_KEY, "jetstream.us-west.bsky.network")
    conn.commit()
    calls = []
    frames = [v2_commit(ALICE, V1, K1, "create", video_record(), seq=43)]
    monkeypatch.setattr(av.websockets, "connect", lambda url, **kw: calls.append(url) or _Socket(frames))
    asyncio.run(av.index_loop(conn, stop_after=1))
    assert "us-west" in calls[0] and calls[0].endswith("&cursor=42")


# -------------------------------------------------------------- lexicons ---


def test_v2_lexicons_follow_the_style_rules():
    from consentvid.lexicons import load_lexicon

    consent = load_lexicon(C2)["defs"]["main"]["record"]["properties"]
    for field in ("modalities", "purposes", "modelClasses"):
        assert all(v == versions.camel_to_kebab(v) for v in consent[field]["items"]["knownValues"])
    for field in ("compensation", "rightsBasis", "captureProvenance", "peoplePresent"):
        assert all(v == v.lower() for v in consent[field]["knownValues"])
    assert "revocable" not in consent and consent["subject"]["ref"] == "com.atproto.repo.strongRef"
    video = load_lexicon(V2)["defs"]["main"]["record"]["properties"]
    for f in ("title", "description"):
        assert video[f]["maxLength"] == 10 * video[f]["maxGraphemes"]
    assert video["contentHash"] == {**video["contentHash"], "format": "cid"} and "maxLength" not in video["contentHash"]
    for f in ("durationMs", "width", "height"):
        assert video[f]["minimum"] == 0 and video[f]["maximum"] == 2**53 - 1


def test_every_string_in_v2_is_bounded():
    from consentvid.lexicons import load_lexicon

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "string":
                assert "maxLength" in node or node.get("format") in ("cid", "datetime", "at-uri", "language"), path
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for nsid in (V2, C2):
        walk(load_lexicon(nsid), nsid)
