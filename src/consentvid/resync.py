"""Consumer-side re-sync: is every clip in a dataset still consented to?

For each clip we verify, against the contributor's own repository and with
signature + Merkle proofs (see :mod:`consentvid.verify`):

* the consent receipt still exists and is byte-identical to the version the
  manifest was built from (same CID), is valid, and hasn't expired;
* every field the manifest states about the consent (terms, attestations,
  people present, provenance, dates) equals the signed record;
* the video record it references still exists, is the version the consent was
  given for, and still commits to the bytes and URL listed in the manifest;
* optionally, that the authorship attestation meets a provenance bar.

The manifest is untrusted input. Its shape is pinned (no duplicate JSON keys,
exact JSON-LD context, ConsentVid's licence URL, exact top-level and
FileObject keys, canonical @ids, encodingFormat derived from the signed
record, every distribution entry accounted for), so a Croissant loader cannot
see a file that resync did not check, and ``--write`` builds a fresh manifest
from the verified records alone.

A video has at most one consent: the consent's record key must equal the
video's (the sidecar rule, enforced by validate_in_repo), so the PDS itself
refuses a second one. A manifest entry whose consent is not keyed to its video
is INVALID; rewriting the consent changes its CID, which is reported as
MODIFIED from the MST-proven record, so no unverified listing is consulted.

Layered moderation: a relay can take an account down while its own PDS keeps
serving signed records. So after the PDS verifies a consent, the relay's
com.atproto.sync.getRepoStatus is asked too (once per DID per process; relay
from $CONSENTVID_RELAY, default relay1.us-east.bsky.network). An inactive
account status (takendown, suspended, deactivated, deleted, or one we don't
know) makes the clip UNAVAILABLE; the relay's own processing statuses
(throttled, host-throttled, desynchronized: appview.RELAY_OPERATIONAL_STATUSES)
do not, and only add a note. RepoNotFound or a network error tells us nothing
and keeps the PDS verdict. When the PDS itself can't be read (5xx, network),
an inactive account status from the relay turns UNKNOWN into UNAVAILABLE
(never REVOKED). ``relay_status=False`` (CLI ``--no-relay-status``) skips
the relay for offline use.

Both record versions are read (org.consentvid.video/consent and
videoV2/consentV2); a consent only ever binds a video of its own version.

Account signals (consentvid.signals): a consent from an account that
labels itself a bot is BOT_ACCOUNT, and one granting 'training' while the
same account's community.lexicon.preference.ai denies training is
CONFLICTING_PREFERENCE. Both are dropped; a failed lookup is UNKNOWN.

Self-labels: a V2 video carrying a sensitive self-label (porn, sexual,
nudity, graphic-media) is LABELED and dropped, unless ``include_labeled``.

Rollback witness: before reading a repo, the relay's
com.atproto.sync.getLatestCommit names the newest revision it has seen. A
PDS whose signed commit is older than that served a rolled-back (or stale)
repo, which could resurrect a withdrawn consent: STALE, dropped. The relay
is asked first, so a commit made between the two reads can't cause a false
alarm. A relay that can't answer only adds a note. Separately, verify
refuses commits whose rev is more than five minutes in the future.

No login is needed: everything read here is public.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import datetime
from urllib.parse import urlsplit

from consentvid.croissant import (
    CROISSANT_CONTEXT,
    LEGACY_CONTEXTS,
    LEGACY_ENCODING_FORMAT,
    LEGACY_LICENSE_URL,
    LICENSE_URL,
    Clip,
    _file_object,
    build_manifest,
)
from consentvid.errors import ConsentVidError, RepoUnavailable, VerificationError
from consentvid.records import (
    CONSENT_NSIDS,
    VIDEO_FOR_CONSENT,
    check_public_host,
    is_expired,
    is_ipfs_cid,
    is_tid,
    normalize_dt,
    parse_at_uri,
)
from consentvid.storage import public_url
from consentvid.validate import validate_in_repo
from consentvid.verify import fetch_verified
from consentvid.versions import BSKY_KIND, PDS_KIND, consent_view, video_view
from consentvid.vocab import PROVENANCE_ORDER, SENSITIVE_SELF_LABELS

VALID = "valid"
REVOKED = "revoked"  # consent or video deleted / account deleted
EXPIRED = "expired"
MODIFIED = "modified"  # record rewritten since the manifest was built
INVALID = "invalid"  # fails validation or proof verification
UNATTESTED = "unattested"  # below the requested provenance bar
UNAVAILABLE = "unavailable"  # account deactivated / taken down / suspended
UNBOUND = "unbound"  # consent stands, but this file isn't bound to it (C2PA input)
UNKNOWN = "unknown"  # network error: couldn't check
BOT_ACCOUNT = "bot-account"  # the account labels itself a bot
CONFLICTING_PREFERENCE = "conflicting-preference"  # its AI-usage preference denies training
LABELED = "labeled"  # the video carries a sensitive self-label
STALE = "stale"  # the PDS served an older repo revision than the relay has seen

STATUSES = (
    VALID, REVOKED, EXPIRED, MODIFIED, INVALID, UNATTESTED, UNAVAILABLE, UNBOUND,
    BOT_ACCOUNT, CONFLICTING_PREFERENCE, LABELED, STALE, UNKNOWN,
)
DROP_STATUSES = (
    REVOKED, EXPIRED, MODIFIED, INVALID, UNATTESTED, UNAVAILABLE, UNBOUND,
    BOT_ACCOUNT, CONFLICTING_PREFERENCE, LABELED, STALE,
)


# Receipt fields that grant permissions. Always compared with the verified
# record (absent == empty), so editing a manifest can never widen a clip.
_TERM_FIELDS = ("purposes", "modelClasses", "modalities")
# Every key a manifest receipt may carry. Any other key is a modification.
_RECEIPT_KEYS = (
    "atUri",
    "cid",
    "videoCid",
    "purposes",
    "modelClasses",
    "modalities",
    "compensation",
    "revocable",
    "authorshipAttested",
    "rightsBasis",
    "captureProvenance",
    "peoplePresent",
    "adultAttested",
    "expiresAt",
    "createdAt",
)
_DT_FIELDS = ("expiresAt", "createdAt")
# Every key a manifest FileObject may carry. Anything else (md5, sc:contentUrl,
# https://schema.org/contentUrl, sameAs, containedIn, ...) could make a
# Croissant loader fetch or check different bytes than resync verified.
_FO_KEYS = frozenset(
    {
        "@type",
        "@id",
        "name",
        "description",
        "contentUrl",
        "encodingFormat",
        "sha256",
        "cv:atUri",
        "cv:videoCid",
        "cv:storageKind",
        "cv:contributor",
        "cv:consentReceipt",
        "cr:atUri",
        "cr:consentReceipt",
        "cr:storageKind",  # legacy (pre-cv:) manifests
    }
)
_FO_STR_KEYS = (
    "@id",
    "name",
    "description",
    "contentUrl",
    "encodingFormat",
    "sha256",
    "cv:atUri",
    "cv:videoCid",
    "cv:storageKind",
    "cv:contributor",
    "cr:atUri",
    "cr:storageKind",
)
# Top-level keys the exporter writes (plus rai:*). Anything else, e.g.
# 'sc:distribution', 'https://schema.org/distribution', '@graph' or
# '@included', is a side channel for FileObjects resync would never see.
_TOP_KEYS = frozenset(
    {
        "@context",
        "@type",
        "name",
        "description",
        "conformsTo",
        "version",
        "datePublished",
        "isLiveDataset",
        "license",
        "url",
        "citeAs",
        "creator",
        "distribution",
        "recordSet",
    }
)
_TOP_STR_KEYS = ("name", "description", "conformsTo", "version", "datePublished", "license", "url", "citeAs")
_C2PA_ONLY_KEYS = frozenset({"consentUri", "consentCid", "videoUri", "videoCid", "contentHash", "contributor", "revocationNote"})


@dataclass
class Item:
    id: str | None
    name: str | None
    consent_uri: str | None
    consent_cid: str | None = None  # CID recorded at export time
    expires_at: str | None = None  # manifest copy (checked offline first)
    sha256: str | None = None  # manifest copy of the content hash
    receipt: dict | None = None  # the manifest's whole consent receipt
    video_uri: str | None = None  # manifest's claimed video
    video_cid: str | None = None  # manifest's claimed video version
    content_url: str | None = None  # where the manifest says the bytes are
    c2pa_state: str | None = None  # validation state when read from a C2PA file
    contributor: str | None = None  # manifest's cv:contributor
    storage_kind: str | None = None  # manifest's cv:storageKind
    encoding_format: str | None = None  # manifest's encodingFormat
    legacy: bool = False  # entry written by the pre-cv: exporter (cr:atUri)
    problem: str | None = None  # malformed entry: always INVALID


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
    # The verified records behind a VALID result, used to rebuild the
    # manifest entry on --write. Never serialised.
    _verified: Clip | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


def _terms(d: dict) -> dict:
    """The permission fields of a consent record (or receipt)."""
    return {k: d.get(k) for k in (*_TERM_FIELDS, "expiresAt")}


def receipt_from_consent(c: dict, consent_uri: str | None = None, consent_cid: str | None = None) -> dict:
    """The receipt a fresh export writes for consent ``c``."""
    return {
        "atUri": consent_uri,
        "cid": consent_cid,
        "videoCid": c.get("videoCid"),
        "purposes": c.get("purposes", []),
        "modelClasses": c.get("modelClasses", []),
        "modalities": c.get("modalities", []),
        "compensation": c.get("compensation"),
        "revocable": c.get("revocable"),
        "authorshipAttested": c.get("authorshipAttested"),
        "rightsBasis": c.get("rightsBasis"),
        "captureProvenance": c.get("captureProvenance", "unverified"),
        "peoplePresent": c.get("peoplePresent"),
        "adultAttested": c.get("adultAttested"),
        "expiresAt": c.get("expiresAt"),
        "createdAt": c.get("createdAt"),
    }


# ------------------------------------------------------------- manifest ----


class _DuplicateKey(ValueError):
    pass


def _no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def load_manifest_json(text):
    """Parse manifest JSON (str or bytes), refusing duplicate object keys at
    any depth: readers disagree on which copy wins (Python: last, streaming
    and first-wins readers: first), so a duplicate lets two programs see two
    different manifests. Raises ConsentVidError for duplicates and nesting
    too deep, ValueError for anything that is not JSON at all."""
    import json

    try:
        return json.loads(text, object_pairs_hook=_no_dup, parse_constant=_no_constant)
    except _DuplicateKey as e:
        raise ConsentVidError(f"manifest has a {e}: refusing to check it") from None
    except RecursionError:
        raise ConsentVidError("manifest is nested too deeply") from None


def validate_manifest_shape(manifest) -> None:
    """Reject manifests whose JSON-LD could carry FileObjects (or re-mapped
    keys) that resync does not see. Raises ConsentVidError."""
    if not isinstance(manifest, dict):
        raise ConsentVidError("manifest must be a JSON object")
    ctx = manifest.get("@context")
    if ctx != CROISSANT_CONTEXT and ctx not in LEGACY_CONTEXTS:
        raise ConsentVidError(
            "manifest @context is not the ConsentVid Croissant context: a modified context can "
            "re-map keys so loaders read different data than resync checks. Re-export it."
        )
    extra = sorted(k for k in manifest if k not in _TOP_KEYS and not k.startswith("rai:"))
    if extra:
        raise ConsentVidError(f"manifest has unexpected top-level key(s) {extra}; refusing to check it")
    if manifest.get("@type") != "sc:Dataset":
        raise ConsentVidError("manifest @type must be 'sc:Dataset'")
    for k in _TOP_STR_KEYS:
        if k in manifest and not isinstance(manifest[k], str):
            raise ConsentVidError(f"manifest {k} must be a string")
    licenses = (LICENSE_URL, LEGACY_LICENSE_URL) if ctx in LEGACY_CONTEXTS else (LICENSE_URL,)
    if manifest.get("license") not in licenses:
        raise ConsentVidError(
            f"manifest license must be {LICENSE_URL} (per-clip consent receipts govern use); "
            f"got {manifest.get('license')!r}. Refusing a manifest that claims a blanket licence."
        )
    if "isLiveDataset" in manifest and not isinstance(manifest["isLiveDataset"], bool):
        raise ConsentVidError("manifest isLiveDataset must be a boolean")
    for k, v in manifest.items():
        if k.startswith("rai:") and not (
            isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))
        ):
            raise ConsentVidError(f"manifest {k} must be a string or a list of strings")
    creators = manifest.get("creator", [])
    creators = [creators] if isinstance(creators, dict) else creators  # legacy: one object
    if not isinstance(creators, list) or not all(
        isinstance(p, dict)
        and set(p) <= {"@type", "name", "identifier", "sameAs"}
        and p.get("@type", "sc:Person") == "sc:Person"
        and all(isinstance(x, str) for x in p.values())
        for p in creators
    ):
        raise ConsentVidError("manifest creator must be a list of sc:Person {name, identifier, sameAs}")
    if manifest.get("recordSet", []) != []:
        raise ConsentVidError("manifest recordSet must be empty (ConsentVid manifests define none)")
    if not isinstance(manifest.get("distribution"), list):
        raise ConsentVidError("manifest distribution must be a list")
    # The @context decides which licence is acceptable, so it must also decide
    # the entry format: a current manifest relabelled with the legacy context
    # could otherwise claim the legacy dataset-wide CC-BY licence (R3-03).
    _LEGACY_KEYS, _CURRENT_KEYS = ("cr:atUri", "cr:consentReceipt"), ("cv:atUri", "cv:consentReceipt")
    for fo in manifest["distribution"]:
        if not isinstance(fo, dict):
            continue  # reported per entry (INVALID)
        is_legacy = all(k in fo for k in _LEGACY_KEYS) and not any(k in fo for k in _CURRENT_KEYS)
        if ctx in LEGACY_CONTEXTS and not is_legacy:
            raise ConsentVidError("legacy @context with current-format entries; re-export the manifest")
        if ctx == CROISSANT_CONTEXT and any(k in fo for k in _LEGACY_KEYS):
            raise ConsentVidError("current @context with legacy-format (cr:atUri) entries; re-export the manifest")


def _distribution_ids(distribution: list) -> tuple[set, set]:
    """(all string @ids, @ids that occur more than once)."""
    seen, dup = set(), set()
    for fo in distribution:
        i = fo.get("@id") if isinstance(fo, dict) else None
        if isinstance(i, str):
            (dup if i in seen else seen).add(i)
    return seen, dup


def _entry_problem(fo, duplicate_ids: set) -> str | None:
    if not isinstance(fo, dict):
        return f"distribution entry is a {type(fo).__name__}, not a FileObject"
    t = fo.get("@type")
    if t != "cr:FileObject":
        return f"unsupported distribution entry @type {t!r} (only 'cr:FileObject')"
    i = fo.get("@id")
    if not isinstance(i, str) or not i:
        return "distribution entry has no string @id"
    if i in duplicate_ids:
        return f"duplicate @id {i!r} in distribution"
    for key in fo:
        if key not in _FO_KEYS:
            return f"unexpected field {key!r}"
    if "cv:consentReceipt" in fo and "cr:consentReceipt" in fo:
        return "both cv:consentReceipt and cr:consentReceipt"
    if "cv:atUri" in fo and "cr:atUri" in fo:
        return "both cv:atUri and cr:atUri"
    for key in _FO_STR_KEYS:
        if key in fo and not isinstance(fo[key], str) and not (key == "cv:storageKind" and fo[key] is None):
            return f"{key} must be a string"
    receipt = fo.get("cv:consentReceipt", fo.get("cr:consentReceipt"))
    if not isinstance(receipt, dict):
        return "no consent receipt object"
    if i not in _canonical_ids(fo, receipt):
        # JSON-LD resolves relative @ids against the document base, so 'x' and
        # './x' would be one node to RDF tools and Croissant loaders but two
        # clips here. Only the exporter's own spelling is accepted.
        return f"non-canonical @id {i!r} (expected <contributor DID>/<video record key>)"
    return None


def _canonical_ids(fo: dict, receipt: dict) -> set[str]:
    """The @id spellings the exporter would have written for this entry."""
    try:
        consent_at = parse_consent_uri(receipt.get("atUri"))
        video_uri = fo.get("cv:atUri", fo.get("cr:atUri"))
        did, rkey = consent_at.did, parse_at_uri(video_uri, VIDEO_FOR_CONSENT[consent_at.collection]).rkey
    except (ValueError, TypeError):
        return set()
    ids = {f"{did}/{rkey}"}
    if "cr:atUri" in fo:  # pre-cv: exporter (3fc54ea..533c2a3)
        ids.add(f"video-{rkey}")
    return ids


def parse_consent_uri(uri):
    """Parse the AT-URI of a consent of either version. Raises ValueError."""
    at = parse_at_uri(uri if isinstance(uri, str) else "")
    if at.collection not in CONSENT_NSIDS:
        raise ValueError(f"expected a {' or '.join(CONSENT_NSIDS)} record, got {at.collection}: {uri}")
    return at


def items_from_manifest(manifest: dict) -> list[Item]:
    """One Item per distribution entry, malformed ones included (they come
    back INVALID). Raises ConsentVidError if the manifest's shape is off."""
    validate_manifest_shape(manifest)
    distribution = manifest["distribution"]
    _, dup = _distribution_ids(distribution)
    items = []
    for fo in distribution:
        problem = _entry_problem(fo, dup)
        d = fo if isinstance(fo, dict) else {}

        def s(k, d=d):
            return d.get(k) if isinstance(d.get(k), str) else None
        r = d.get("cv:consentReceipt", d.get("cr:consentReceipt"))
        r = r if isinstance(r, dict) else {}
        items.append(
            Item(
                id=s("@id"),
                name=s("name"),
                consent_uri=r.get("atUri") if isinstance(r.get("atUri"), str) else None,
                consent_cid=r.get("cid") if isinstance(r.get("cid"), str) else None,
                expires_at=r.get("expiresAt") if isinstance(r.get("expiresAt"), str) else None,
                sha256=s("sha256") or None,
                receipt=dict(r),
                video_uri=s("cv:atUri") or s("cr:atUri"),
                video_cid=s("cv:videoCid"),
                content_url=s("contentUrl"),
                contributor=s("cv:contributor"),
                storage_kind=s("cv:storageKind") or s("cr:storageKind"),
                encoding_format=s("encodingFormat"),
                legacy="cr:atUri" in d,
                problem=problem,
            )
        )
    return items


def item_from_c2pa(receipt: dict, name: str, state: str | None) -> Item:
    if not isinstance(receipt, dict):
        return Item(id=name, name=name, consent_uri=None, c2pa_state=state or "unknown",
                    problem="C2PA consent assertion is not an object")
    problem = None
    extra = sorted(k for k in receipt if k not in _C2PA_ONLY_KEYS and k not in _RECEIPT_KEYS)
    if extra:
        problem = f"unexpected C2PA receipt field {extra[0]!r}"

    def s(k):
        return receipt.get(k) if isinstance(receipt.get(k), str) else None

    for k in ("consentUri", "consentCid", "videoUri", "videoCid", "contentHash"):
        if k in receipt and receipt[k] is not None and not isinstance(receipt[k], str):
            problem = problem or f"C2PA receipt {k} must be a string"
    contributor = receipt.get("contributor")
    did = contributor.get("did") if isinstance(contributor, dict) else None
    # Map onto the manifest receipt shape so both inputs get the same checks.
    mapped = {k: v for k, v in receipt.items() if k in _RECEIPT_KEYS and k not in ("atUri", "cid", "videoCid")}
    mapped["atUri"], mapped["cid"] = s("consentUri"), s("consentCid")
    return Item(
        id=name,
        name=name,
        consent_uri=s("consentUri"),
        consent_cid=s("consentCid"),
        expires_at=s("expiresAt"),
        sha256=(s("contentHash") or "").removeprefix("sha256-") or None,
        receipt=mapped,
        video_uri=s("videoUri"),
        video_cid=s("videoCid"),
        contributor=did if isinstance(did, str) else None,
        c2pa_state=state or "unknown",
        problem=problem,
    )


# ------------------------------------------------------------ comparison ---


def _receipt_shape_error(receipt: dict) -> tuple[str, str] | None:
    """(status, reason) if the receipt can't even be compared, else None."""
    for k in receipt:
        if k not in _RECEIPT_KEYS:
            return MODIFIED, f"unexpected receipt field {k!r}"
    for k in _TERM_FIELDS:
        v = receipt.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return INVALID, f"receipt {k} must be a list of strings"
    for k in (*_DT_FIELDS, "atUri", "cid", "videoCid", "compensation", "rightsBasis",
              "captureProvenance", "peoplePresent"):
        v = receipt.get(k)
        if v is not None and not isinstance(v, str):
            return INVALID, f"receipt {k} must be a string"
    for k in ("revocable", "authorshipAttested", "adultAttested"):
        v = receipt.get(k)
        if v is not None and not isinstance(v, bool):
            return INVALID, f"receipt {k} must be a boolean"
    return None


def _receipt_mismatch(receipt: dict, c: dict) -> str | None:
    """Name of the first receipt field that disagrees with the verified
    consent record ``c``, else None. The permission fields are always
    compared (absent == empty); other fields only when the receipt states
    them (legacy receipts omit some)."""
    for k in _TERM_FIELDS:
        if set(receipt.get(k) or []) != set(c.get(k) or []):
            return k
    if normalize_dt(receipt.get("expiresAt")) != normalize_dt(c.get("expiresAt")):
        return "expiresAt"
    if receipt.get("expiresAt") is not None and normalize_dt(receipt["expiresAt"]) is None:
        return "expiresAt"
    for k, claimed in receipt.items():
        if k in ("atUri", "cid", "expiresAt", *_TERM_FIELDS):
            continue
        if k == "createdAt":
            if normalize_dt(claimed) != normalize_dt(c.get(k)) or (claimed is not None and normalize_dt(claimed) is None):
                return k
        elif k == "captureProvenance":
            if claimed != c.get(k, "unverified"):
                return k
        elif claimed != c.get(k):
            return k
    return None


_LOADER_SPECIAL_HOSTS = ("github.com", "gitlab.com", "huggingface.co")


def _url_matches(url: str, ref: dict) -> bool:
    """Does the manifest's contentUrl denote the signed storageRef?"""
    kind, value = ref.get("kind"), ref.get("value")
    if not isinstance(url, str) or not isinstance(value, str):
        return False
    if kind == "ipfs":
        cid = value.removeprefix("ipfs://")
        if not is_ipfs_cid(cid):
            return False
        try:
            u = urlsplit(url)
            userinfo = u.username is not None or u.password is not None
            port = u.port
            check_public_host(u.hostname)
        except ValueError:
            return False
        # Croissant loaders special-case some hosts (mlcroissant git-clones
        # anything starting with https://github.com); never let a gateway
        # URL look like one of them.
        if u.netloc.lower().startswith(_LOADER_SPECIAL_HOSTS):
            return False
        return (
            u.scheme == "https"
            and bool(u.netloc)
            and not userinfo
            and port in (None, 443)
            and not u.query
            and not u.fragment
            and u.path == "/ipfs/" + cid
        )
    if kind in ("url", "r2", "peertube"):
        return url == value
    if kind == BSKY_KIND:
        from consentvid import bsky

        return bsky.url_matches(url, ref)
    if kind == PDS_KIND:
        from consentvid import bsky

        return bsky.blob_url_matches(url, ref.get("did"), value)
    return False


def _is_transient(e: BaseException | None) -> bool:
    """Network/transport trouble (worth retrying) as opposed to an answer we
    don't accept. Only transient errors may yield UNKNOWN."""
    import ssl

    import httpx
    from atproto_client.exceptions import NetworkError, RequestException

    # A TLS verification failure is not "the network is down": it is what an
    # interception looks like, so it must never become UNKNOWN (--lenient keeps those).
    chain, seen, x = [], set(), e
    while x is not None and id(x) not in seen:
        seen.add(id(x))
        chain.append(x)
        x = x.__cause__ or x.__context__
    if any(isinstance(x, ssl.SSLError) for x in chain):
        return False
    from consentvid.identity import ResponseTooLarge

    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        # Over the size/time budget: the server's doing, never "transient"
        # (its __context__ may be the httpx timeout that the deadline caused).
        if isinstance(e, ResponseTooLarge):
            return False
        if isinstance(e, (NetworkError, httpx.TransportError, OSError, TimeoutError)):
            return True
        if isinstance(e, RequestException):
            code = getattr(getattr(e, "response", None), "status_code", None)
            return isinstance(code, int) and (code >= 500 or code == 429)
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            return code >= 500 or code == 429
        e = e.__cause__ or e.__context__
    return False


def _fetch_failure(what: str, e: Exception) -> tuple[str, str]:
    from atproto_client.exceptions import BadRequestError

    if _is_transient(e):
        return UNKNOWN, f"could not check {what}: {type(e).__name__}: {e}"
    if isinstance(e, BadRequestError):
        return INVALID, f"PDS rejected the request for the {what}: {e}"
    return INVALID, f"could not verify {what}: {type(e).__name__}: {e}"


# ---------------------------------------------------------- relay status ---


def _relay_url() -> str:
    from consentvid.appview import DEFAULT_RELAY
    from consentvid.storage import require_https

    return require_https("CONSENTVID_RELAY", os.environ.get("CONSENTVID_RELAY") or DEFAULT_RELAY).rstrip("/")


def _relay_lookup_impl(did: str) -> tuple[bool, str | None] | None:
    """(active, status) per the relay, or None when it says nothing useful
    (RepoNotFound: a relay only knows repos it crawls; network errors)."""
    from consentvid.appview import relay_repo_status

    try:
        return relay_repo_status(_relay_url(), did)
    except Exception:  # noqa: BLE001 — no information: keep the PDS verdict
        return None


_relay_lookup = _relay_lookup_impl  # tests replace this (offline by default)
_relay_cache: dict[str, tuple[float, tuple[bool, str | None] | None]] = {}
_relay_locks: dict[str, threading.Lock] = {}
_relay_guard = threading.Lock()


def relay_status_of(did: str) -> tuple[bool, str | None] | None:
    """The relay's view of ``did``, asked at most once per DID per
    IDENTITY_TTL, even from concurrent check_all workers."""
    import time

    from consentvid.identity import IDENTITY_TTL

    with _relay_guard:
        lock = _relay_locks.setdefault(did, threading.Lock())
    with lock:
        hit = _relay_cache.get(did)
        if hit is not None and time.monotonic() - hit[0] <= IDENTITY_TTL:
            return hit[1]
        answer = _relay_lookup(did)
        _relay_cache[did] = (time.monotonic(), answer)
        return answer


def clear_relay_cache() -> None:
    with _relay_guard:
        _relay_cache.clear()


def _witness_lookup_impl(did: str) -> str | None:
    """The newest repo rev the relay has seen for ``did``
    (com.atproto.sync.getLatestCommit), or None if it can't say."""
    from consentvid.appview import relay_latest_rev

    try:
        return relay_latest_rev(_relay_url(), did)
    except Exception:  # noqa: BLE001 — no witness: only a note
        return None


_witness_lookup = _witness_lookup_impl  # tests replace this (offline by default)
_witness_cache: dict[str, tuple[float, str | None]] = {}


def witness_rev_of(did: str) -> str | None:
    """The relay's witnessed rev for ``did``, cached per IDENTITY_TTL. An
    older witness is still a valid lower bound (revs only grow), so the
    cache never causes a false alarm."""
    import time

    from consentvid.identity import IDENTITY_TTL

    with _relay_guard:
        lock = _relay_locks.setdefault(f"witness:{did}", threading.Lock())
    with lock:
        hit = _witness_cache.get(did)
        if hit is not None and time.monotonic() - hit[0] <= IDENTITY_TTL:
            return hit[1]
        rev = _witness_lookup(did)
        rev = rev if is_tid(rev) else None
        _witness_cache[did] = (time.monotonic(), rev)
        return rev


def clear_witness_cache() -> None:
    with _relay_guard:
        _witness_cache.clear()


def stale_against(witnessed: str | None, record) -> bool:
    """Is a verified read older than the relay's witnessed revision?"""
    rev = getattr(record, "rev", None)
    return bool(getattr(record, "proven", False) and is_tid(rev) and is_tid(witnessed) and rev < witnessed)


# ----------------------------------------------------------------- check ---


def check(
    item: Item,
    *,
    now: datetime | None = None,
    min_provenance: str | None = None,
    fetch=fetch_verified,
    relay_status: bool = True,
    include_labeled: bool = False,
) -> Result:
    """Classify one clip. ``relay_status``: also ask the relay whether the
    account is active and which revision it has seen (see the module
    docstring). ``include_labeled``: keep videos with sensitive self-labels."""
    res = Result(item.id, item.name, item.consent_uri, UNKNOWN)

    def done(status, reason="", verified=False):
        res.status, res.reason, res.verified = status, reason, verified
        return res

    if item.problem:
        return done(INVALID, item.problem)
    if item.c2pa_state is not None and item.c2pa_state not in ("Valid", "Trusted"):
        return done(INVALID, f"C2PA manifest failed validation ({item.c2pa_state}): file was altered")
    receipt = item.receipt if item.receipt is not None else {}
    if not isinstance(receipt, dict):
        return done(INVALID, "consent receipt is not an object")
    shape = _receipt_shape_error(receipt)
    if shape:
        return done(*shape)
    try:
        consent_at = parse_consent_uri(item.consent_uri or "")
    except ValueError as e:
        return done(INVALID, f"bad consent URI: {e}")
    consent_nsid = consent_at.collection
    video_nsid = VIDEO_FOR_CONSENT[consent_nsid]
    if not item.sha256:
        return done(INVALID, "no sha256 for the clip: bytes can't be bound to the consent")
    if item.c2pa_state is None and not item.content_url:
        return done(INVALID, "no contentUrl: nothing binds the clip to where its bytes are served")
    if item.contributor is not None and item.contributor != consent_at.did:
        return done(MODIFIED, "manifest's contributor differs from the consent's repo")
    if is_expired({"expiresAt": item.expires_at}, now):
        return done(EXPIRED, f"expired at {item.expires_at}")
    notes: list[str] = []  # caveats carried into a VALID reason

    def pds_failure(what: str, e: Exception) -> Result:
        """A failed PDS read. If it is only "couldn't check" (UNKNOWN), a
        relay that knows the account is gone settles it as UNAVAILABLE (a
        PDS answering 500 for repos it no longer hosts would otherwise keep
        the clip UNKNOWN forever). Never REVOKED: a relay's word is not a
        signed proof of withdrawal."""
        status, reason = _fetch_failure(what, e)
        if status == UNKNOWN and relay_status:
            from consentvid.appview import is_operational

            answer = relay_status_of(consent_at.did)
            if answer is not None and answer[0] is False and not is_operational(answer):
                return done(UNAVAILABLE, f"PDS unreachable; relay reports account {answer[1] or 'inactive'}")
        return done(status, reason)

    # -- consent ------------------------------------------------------------
    # The relay's witness is read BEFORE the PDS: a commit made in between
    # can then only make the PDS newer, never look like a rollback.
    witnessed = witness_rev_of(consent_at.did) if relay_status else None
    if relay_status and witnessed is None:
        notes.append("rollback not checked: the relay named no revision for this repo")
    try:
        consent = fetch(consent_at)
    except RepoUnavailable as e:
        if e.status == "deleted":
            return done(REVOKED, "contributor account deleted")
        return done(UNAVAILABLE, f"contributor account {e.status}")
    except VerificationError as e:
        return done(INVALID, f"consent proof failed: {e}")
    except Exception as e:  # noqa: BLE001 — network & SDK errors
        return pds_failure("consent", e)
    if not consent.present:
        return done(REVOKED, "consent withdrawn (record deleted)", consent.proven)
    if stale_against(witnessed, consent):
        return done(
            STALE,
            f"the PDS served repo revision {consent.rev}, older than the {witnessed} the relay has seen "
            "(rolled back or stale): a withdrawal may be hidden",
            True,
        )
    if relay_status:
        from consentvid.appview import is_operational

        answer = relay_status_of(consent_at.did)
        if answer is not None and not answer[0]:
            if is_operational(answer):
                # throttled / host-throttled / desynchronized describe the
                # relay's own processing, not the account: the PDS has just
                # proven the consent, so it stands.
                notes.append(f"relay reports {answer[1]}; live index may be stale")
            else:
                # 'deleted' too: the PDS still serves the repo, so no
                # revocation is claimed; the clip just can't be used.
                return done(UNAVAILABLE, f"relay reports account {answer[1] or 'inactive'}")
    errors = validate_in_repo(consent_nsid, consent.value, consent_at.did, rkey=consent_at.rkey)
    if errors:
        return done(INVALID, f"consent invalid: {errors[0]}", True)
    c = consent_view(consent_nsid, consent.value)
    res.captureProvenance = c.get("captureProvenance")
    res.videoUri = c["videoUri"]
    if item.consent_cid and consent.cid != item.consent_cid:
        return done(MODIFIED, "consent record changed since export; re-export", True)
    field_name = _receipt_mismatch(receipt, c)
    if field_name:
        return done(MODIFIED, f"manifest's {field_name} differs from the signed consent", True)
    if item.video_uri and item.video_uri != c["videoUri"]:
        return done(MODIFIED, "manifest's video differs from the one consented to", True)
    if is_expired(c, now):
        return done(EXPIRED, f"expired at {c.get('expiresAt')}", True)

    # -- video it binds to ----------------------------------------------------
    video_at = parse_at_uri(c["videoUri"], video_nsid)
    try:
        video = fetch(video_at)
    except RepoUnavailable as e:
        return done(UNAVAILABLE, f"video repo {e.status}")
    except VerificationError as e:
        return done(INVALID, f"video proof failed: {e}")
    except Exception as e:  # noqa: BLE001
        return pds_failure("video", e)
    if not video.present:
        return done(REVOKED, "video withdrawn (record deleted)", video.proven)
    if stale_against(witnessed, video):
        return done(STALE, f"the PDS served repo revision {video.rev}, older than the {witnessed} the relay has seen", True)
    errors = validate_in_repo(video_nsid, video.value, consent_at.did, rkey=video_at.rkey)
    if errors:
        return done(INVALID, f"video record invalid: {errors[0]}", True)
    v = video_view(video_nsid, video.value, consent_at.did)
    if video.cid != c["videoCid"]:  # validate_in_repo requires videoCid
        return done(MODIFIED, "video record changed after consent was given", True)
    if item.video_cid and video.cid != item.video_cid:
        return done(MODIFIED, "video record changed since export", True)
    if v["contentHash"].removeprefix("sha256-") != item.sha256:
        return done(MODIFIED, "sha256 differs from the signed video record", True)
    ref = v.get("storageRef") or {}
    if item.c2pa_state is None and not _url_matches(item.content_url, ref):
        return done(MODIFIED, "contentUrl differs from the signed video record", True)
    if item.storage_kind is not None and item.storage_kind != ref.get("kind"):
        return done(MODIFIED, "manifest's storage kind differs from the signed video record", True)
    if ref.get("kind") == BSKY_KIND:
        # The bytes are a Bluesky post's video: the post must still exist at
        # the pinned version (read with a proof) and embed that blob.
        from consentvid import bsky

        try:
            problem = bsky.check_post(ref, consent_at.did, fetch)
        except RepoUnavailable as e:
            return done(UNAVAILABLE, f"post repo {e.status}")
        except VerificationError as e:
            return done(INVALID, f"post proof failed: {e}")
        except Exception as e:  # noqa: BLE001
            return pds_failure("post", e)
        if problem:
            kind, why = problem
            return done({"gone": UNAVAILABLE, "modified": MODIFIED}.get(kind, INVALID), why, True)
        try:
            bsky.url_for_ref(ref)  # the PDS that serves the blob (cached for _file_object)
        except Exception as e:  # noqa: BLE001
            return done(UNKNOWN, f"couldn't resolve the PDS serving the video: {type(e).__name__}: {e}")

    # -- self-labels and account signals ------------------------------------
    sensitive = sorted(set(v.get("labels") or []) & set(SENSITIVE_SELF_LABELS))
    if sensitive and not include_labeled:
        return done(LABELED, f"the contributor labeled the video {', '.join(sensitive)}", True)
    from consentvid import signals

    try:
        account = signals.account_signals(consent_at.did)
    except Exception as e:  # noqa: BLE001 — fail closed: unknown, dropped under --strict
        return done(UNKNOWN, f"couldn't check the account's bot label / AI-usage preferences: {type(e).__name__}: {e}")
    if account.bot:
        return done(BOT_ACCOUNT, account.conflicts(c) or "bot account", True)
    conflict = account.conflicts(c)
    if conflict:
        return done(CONFLICTING_PREFERENCE, conflict, True)

    # -- provenance bar -------------------------------------------------------
    if min_provenance:
        prov = c.get("captureProvenance") or "unverified"
        tier = PROVENANCE_ORDER.get(prov, 0)
        if prov == "attested":
            # Nothing verifies capture-time provenance yet: a signed
            # 'attested' is the contributor's own claim, like selfAttested.
            tier = PROVENANCE_ORDER["selfAttested"]
        if min_provenance == "attested":
            return done(UNATTESTED, "no capture-provenance verifier yet: 'attested' is self-declared", True)
        if tier < PROVENANCE_ORDER[min_provenance]:
            return done(UNATTESTED, f"provenance {c.get('captureProvenance')} < {min_provenance}", True)

    if item.c2pa_state is not None:
        # A C2PA signature (self-signed or CA-trusted) is not the contributor's
        # DID key, and embedding changes the bytes, so nothing ties THIS file
        # to the consented bytes. The receipt is a pointer, not a proof.
        return done(
            UNBOUND,
            "consent stands, but this file's bytes are not bound to it by the contributor's key "
            f"(C2PA signer is not the DID); use the registered bytes sha256-{item.sha256} "
            f"from {public_url(ref) or 'the contributor'}",
            False,
        )
    clip = Clip(item.consent_uri, consent.cid, c, c["videoUri"], video.cid, v, consent_at.did)
    rebuilt = _file_object(clip)
    if rebuilt is None:
        return done(MODIFIED, "the signed video record has no public URL", True)
    # Loaders act on encodingFormat ('git+https' -> git clone, no hash check),
    # so it must be exactly what the exporter derives from the signed record.
    formats = {rebuilt["encodingFormat"]} | ({LEGACY_ENCODING_FORMAT} if item.legacy else set())
    if item.encoding_format not in formats:
        return done(MODIFIED, "manifest's encodingFormat differs from the signed video record", True)
    if not item.consent_cid:
        notes.append("manifest does not pin the consent CID")
    res._verified = clip
    return done(VALID, "; ".join(notes), True)


def check_all(items: list[Item], *, workers: int = 8, **kw) -> list[Result]:
    def safe(it: Item) -> Result:
        try:
            return check(it, **kw)
        except Exception as e:  # noqa: BLE001 — one bad clip must not abort the run
            # Fail closed: an unexpected error is never "couldn't reach the
            # network", so --lenient must not keep it.
            return Result(
                it.id, it.name, it.consent_uri, INVALID, f"internal error while checking: {type(e).__name__}: {e}"
            )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(safe, items))


def filter_manifest(manifest: dict, results: list[Result], now_iso: str, *, strict: bool = True) -> dict:
    """A new manifest holding only clips that passed (an allowlist).

    Built from scratch by the exporter from the verified records: every kept
    VALID entry and every dataset-level field (creator, licence, rai:*) comes
    from signed data, never from the input. Only the input's name and version
    are carried over. ``strict`` (default) also drops clips whose status is
    unknown: unverifiable consent is not consent. Under --lenient, unknown
    entries are appended as they were (they passed the shape checks) and the
    manifest says so."""
    drop_statuses = DROP_STATUSES + ((UNKNOWN,) if strict else ())
    distribution = manifest.get("distribution")
    distribution = distribution if isinstance(distribution, list) else []
    _, dup = _distribution_ids(distribution)
    dropped_ids = {r.id for r in results if r.status in drop_statuses}
    by_id = {}
    for r in results:
        if isinstance(r.id, str) and r.status not in drop_statuses:
            by_id.setdefault(r.id, r)
    keep_ids = set(by_id) - dup - dropped_ids
    verified, unverified = [], []
    for fo in distribution:
        if not (isinstance(fo, dict) and fo.get("@type") == "cr:FileObject"):
            continue
        i = fo.get("@id")
        if not isinstance(i, str) or i not in keep_ids:
            continue
        r = by_id[i]
        if r.status == VALID and r._verified is not None:
            verified.append(r._verified)
        elif r.status == UNKNOWN:
            unverified.append(fo)
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else "ConsentVid dataset"
    version = manifest.get("version") if isinstance(manifest.get("version"), str) else "1.0.0"
    out, _ = build_manifest(verified, name=name, version=version)
    out["distribution"] = out["distribution"] + unverified
    kept = len(out["distribution"])
    if unverified:
        out["rai:dataLimitations"] += (
            f" {len(unverified)} clip(s) kept unverified under --lenient: their consent could "
            "not be checked, and nothing in this manifest vouches for them."
        )
    note = f"Re-synced {now_iso}: {len(distribution) - kept} clip(s) dropped, {kept} remain."
    out["rai:dataReleaseMaintenancePlan"] = f"{out['rai:dataReleaseMaintenancePlan']} {note}"
    return out
