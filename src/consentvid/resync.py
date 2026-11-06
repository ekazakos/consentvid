"""Consumer-side re-sync: is every clip in a dataset still consented to?

For each clip we verify, against the contributor's own repository and with
signature + Merkle proofs (see :mod:`consentvid.verify`):

* the consent receipt still exists and is byte-identical to the version the
  manifest was built from (same CID), is valid, and hasn't expired;
* the video record it references still exists, is the version the consent was
  given for, and still commits to the bytes listed in the manifest;
* optionally, that the authorship attestation meets a provenance bar.

No login is needed: everything read here is public.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime

from consentvid.croissant import receipt_of, video_uri_of
from consentvid.errors import RepoUnavailable, VerificationError
from consentvid.records import CONSENT_NSID, VIDEO_NSID, is_expired, normalize_dt, parse_at_uri
from consentvid.validate import validate_in_repo
from consentvid.verify import fetch_verified
from consentvid.vocab import PROVENANCE_ORDER

VALID = "valid"
REVOKED = "revoked"  # consent or video deleted / account deleted
EXPIRED = "expired"
MODIFIED = "modified"  # record rewritten since the manifest was built
INVALID = "invalid"  # fails validation or proof verification
UNATTESTED = "unattested"  # below the requested provenance bar
UNAVAILABLE = "unavailable"  # account deactivated / taken down / suspended
UNKNOWN = "unknown"  # network error: couldn't check

STATUSES = (VALID, REVOKED, EXPIRED, MODIFIED, INVALID, UNATTESTED, UNAVAILABLE, UNKNOWN)
DROP_STATUSES = (REVOKED, EXPIRED, MODIFIED, INVALID, UNATTESTED, UNAVAILABLE)


# Manifest fields that grant permissions. resync compares each with the
# verified record, so editing a manifest can never widen what a clip permits.
_TERM_FIELDS = ("purposes", "modelClasses", "modalities")


@dataclass
class Item:
    id: str | None
    name: str | None
    consent_uri: str | None
    consent_cid: str | None = None  # CID recorded at export time
    expires_at: str | None = None  # manifest copy (checked offline first)
    sha256: str | None = None  # manifest copy of the content hash
    terms: dict | None = None  # manifest's claimed terms (purposes, ...)
    video_uri: str | None = None  # manifest's claimed video
    video_cid: str | None = None  # manifest's claimed video version
    content_url: str | None = None  # where the manifest says the bytes are
    c2pa_state: str | None = None  # validation state when read from a C2PA file


@dataclass
class Result:
    id: str | None
    name: str | None
    consentUri: str | None
    status: str
    reason: str = ""
    verified: bool = False  # outcome backed by a cryptographic proof
    videoUri: str | None = None
    captureProvenance: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _terms(d: dict) -> dict:
    return {k: d.get(k) for k in (*_TERM_FIELDS, "expiresAt")}


def items_from_manifest(manifest: dict) -> list[Item]:
    items = []
    for fo in manifest.get("distribution", []) or []:
        if fo.get("@type") != "cr:FileObject":
            continue
        r = receipt_of(fo)
        items.append(
            Item(
                id=fo.get("@id"),
                name=fo.get("name"),
                consent_uri=r.get("atUri"),
                consent_cid=r.get("cid"),
                expires_at=r.get("expiresAt"),
                sha256=fo.get("sha256") or None,
                terms=_terms(r),
                video_uri=video_uri_of(fo),
                video_cid=fo.get("cv:videoCid"),
                content_url=fo.get("contentUrl"),
            )
        )
    return items


def item_from_c2pa(receipt: dict, name: str, state: str | None) -> Item:
    return Item(
        id=name,
        name=name,
        consent_uri=receipt.get("consentUri"),
        consent_cid=receipt.get("consentCid"),
        expires_at=receipt.get("expiresAt"),
        sha256=(receipt.get("contentHash") or "").removeprefix("sha256-") or None,
        terms=_terms(receipt),
        video_uri=receipt.get("videoUri"),
        video_cid=receipt.get("videoCid"),
        c2pa_state=state or "unknown",
    )


def _same_terms(claimed: dict, live: dict) -> str | None:
    """Name of the first permission field where the manifest disagrees with
    the verified record, else None."""
    for k in _TERM_FIELDS:
        if set(claimed.get(k) or []) != set(live.get(k) or []):
            return k
    if normalize_dt(claimed.get("expiresAt")) != normalize_dt(live.get("expiresAt")):
        return "expiresAt"
    return None


def _url_matches(url: str, ref: dict) -> bool:
    kind, value = ref.get("kind"), ref.get("value") or ""
    if kind == "ipfs":
        return url.rstrip("/").endswith("/ipfs/" + value.removeprefix("ipfs://"))
    return url == value


def check(
    item: Item,
    *,
    now: datetime | None = None,
    min_provenance: str | None = None,
    fetch=fetch_verified,
    list_consents=None,
) -> Result:
    """Classify one clip. ``list_consents(did) -> [(uri, value)]`` enables the
    supersession check (a newer consent for the same video governs)."""
    res = Result(item.id, item.name, item.consent_uri, UNKNOWN)

    def done(status, reason="", verified=False):
        res.status, res.reason, res.verified = status, reason, verified
        return res

    if item.c2pa_state is not None and item.c2pa_state not in ("Valid", "Trusted"):
        return done(INVALID, f"C2PA manifest failed validation ({item.c2pa_state}): file was altered")
    try:
        consent_at = parse_at_uri(item.consent_uri or "", CONSENT_NSID)
    except ValueError as e:
        return done(INVALID, f"bad consent URI: {e}")
    if not item.sha256:
        return done(INVALID, "no sha256 for the clip: bytes can't be bound to the consent")
    if is_expired({"expiresAt": item.expires_at}, now):
        return done(EXPIRED, f"expired at {item.expires_at}")

    # -- consent ------------------------------------------------------------
    try:
        consent = fetch(consent_at)
    except RepoUnavailable as e:
        if e.status == "deleted":
            return done(REVOKED, "contributor account deleted")
        return done(UNAVAILABLE, f"contributor account {e.status}")
    except VerificationError as e:
        return done(INVALID, f"consent proof failed: {e}")
    except Exception as e:  # noqa: BLE001 — network & SDK errors
        return done(UNKNOWN, f"could not check consent: {type(e).__name__}: {e}")
    if not consent.present:
        return done(REVOKED, "consent withdrawn (record deleted)", consent.proven)
    c = consent.value
    errors = validate_in_repo(CONSENT_NSID, c, consent_at.did)
    if errors:
        return done(INVALID, f"consent invalid: {errors[0]}", True)
    res.captureProvenance = c.get("captureProvenance")
    res.videoUri = c["videoUri"]
    if item.consent_cid and consent.cid != item.consent_cid:
        return done(MODIFIED, "consent record changed since export; re-export", True)
    field = _same_terms(item.terms or {}, c)
    if field:
        return done(MODIFIED, f"manifest's {field} differs from the signed consent", True)
    if item.video_uri and item.video_uri != c["videoUri"]:
        return done(MODIFIED, "manifest's video differs from the one consented to", True)
    if is_expired(c, now):
        return done(EXPIRED, f"expired at {c.get('expiresAt')}", True)

    # -- superseded by a newer consent for the same video ---------------------
    if list_consents is not None:
        try:
            newer = [
                uri
                for uri, v in list_consents(consent_at.did)
                if uri != item.consent_uri
                and v.get("videoUri") == c["videoUri"]
                and not validate_in_repo(CONSENT_NSID, v, consent_at.did)
                and (normalize_dt(v.get("createdAt")) or "") > (normalize_dt(c.get("createdAt")) or "")
            ]
        except Exception as e:  # noqa: BLE001
            return done(UNKNOWN, f"could not list the contributor's consents: {type(e).__name__}: {e}")
        if newer:
            return done(MODIFIED, f"superseded by newer consent {newer[0]}", False)

    # -- video it binds to ----------------------------------------------------
    try:
        video = fetch(parse_at_uri(c["videoUri"], VIDEO_NSID))
    except RepoUnavailable as e:
        return done(UNAVAILABLE, f"video repo {e.status}")
    except VerificationError as e:
        return done(INVALID, f"video proof failed: {e}")
    except Exception as e:  # noqa: BLE001
        return done(UNKNOWN, f"could not check video: {type(e).__name__}: {e}")
    if not video.present:
        return done(REVOKED, "video withdrawn (record deleted)", video.proven)
    v = video.value
    errors = validate_in_repo(VIDEO_NSID, v, consent_at.did)
    if errors:
        return done(INVALID, f"video record invalid: {errors[0]}", True)
    if c.get("videoCid") and video.cid != c["videoCid"]:
        return done(MODIFIED, "video record changed after consent was given", True)
    if item.video_cid and video.cid != item.video_cid:
        return done(MODIFIED, "video record changed since export", True)
    if v["contentHash"].removeprefix("sha256-") != item.sha256:
        return done(MODIFIED, "sha256 differs from the signed video record", True)
    if item.content_url and not _url_matches(item.content_url, v.get("storageRef") or {}):
        return done(MODIFIED, "contentUrl differs from the signed video record", True)

    # -- provenance bar -------------------------------------------------------
    if min_provenance:
        tier = PROVENANCE_ORDER.get(c.get("captureProvenance") or "unverified", 0)
        if tier < PROVENANCE_ORDER[min_provenance]:
            return done(UNATTESTED, f"provenance {c.get('captureProvenance')} < {min_provenance}", True)
    notes = []
    if not c.get("videoCid"):
        notes.append("legacy consent: not pinned to a video version")
    if item.c2pa_state == "Valid":
        notes.append("C2PA signer untrusted: file-to-consent binding unverified")
    return done(VALID, "; ".join(notes), True)


def _list_consents(did: str) -> list[tuple[str, dict]]:
    from consentvid.identity import public_client
    from consentvid.records import to_dict

    client, out, cursor = public_client(did), [], None
    while True:
        params = {"repo": did, "collection": CONSENT_NSID, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.com.atproto.repo.list_records(params)
        out.extend((r.uri, to_dict(r.value)) for r in resp.records)
        cursor = resp.cursor
        if not cursor or not resp.records:
            return out


def check_all(items: list[Item], *, workers: int = 8, list_consents=_list_consents, **kw) -> list[Result]:
    cache: dict[str, list] = {}

    def cached(did):
        if did not in cache:
            cache[did] = list_consents(did)
        return cache[did]

    def safe(it: Item) -> Result:
        try:
            return check(it, list_consents=cached, **kw)
        except Exception as e:  # noqa: BLE001 — one bad clip must not abort the run
            return Result(it.id, it.name, it.consent_uri, UNKNOWN, f"internal error: {type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(safe, items))


def filter_manifest(manifest: dict, results: list[Result], now_iso: str, *, strict: bool = True) -> dict:
    """Copy of the manifest without dropped clips; notes the re-sync in the
    maintenance plan. ``strict`` (default) also drops clips whose status is
    unknown: unverifiable consent is not consent."""
    drop_statuses = DROP_STATUSES + ((UNKNOWN,) if strict else ())
    drop = {r.id for r in results if r.status in drop_statuses}
    kept = [fo for fo in manifest.get("distribution", []) or [] if fo.get("@id") not in drop]
    out = dict(manifest)
    out["distribution"] = kept
    note = f"Re-synced {now_iso}: {len(drop)} clip(s) dropped, {len(kept)} remain."
    out["rai:dataReleaseMaintenancePlan"] = (
        f"{manifest.get('rai:dataReleaseMaintenancePlan', '')} {note}".strip()
    )
    return out
