"""Croissant 1.0 + RAI dataset manifests from consent receipts.

Spec: http://mlcommons.org/croissant/ (1.0), RAI: http://mlcommons.org/croissant/RAI/

Each clip is a ``cr:FileObject`` carrying a ``cv:consentReceipt``: the AT-URI
and CID of the exact signed consent record, its terms, and the CID of the video
record it binds to. ``consentvid resync`` re-verifies all of that before use.

ConsentVid-specific properties live in our own JSON-LD namespace (``cv:``);
the ``cr:`` namespace belongs to MLCommons. Manifests written by older versions
used ``cr:consentReceipt``/``cr:atUri``; readers accept both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from consentvid import __version__
from consentvid.records import mime_for, normalize_dt
from consentvid.storage import public_url

CV_NS = "https://consentvid.org/ns#"
LICENSE_URL = "https://consentvid.org/license.html"
# What the pre-cv: exporter (3fc54ea..533c2a3) wrote; resync accepts these only
# in manifests with the legacy context / legacy entries.
LEGACY_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
LEGACY_ENCODING_FORMAT = "video/*"

CROISSANT_CONTEXT = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "cr": "http://mlcommons.org/croissant/",
    "cv": CV_NS,
    "rai": "http://mlcommons.org/croissant/RAI/",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "dct": "http://purl.org/dc/terms/",
    "examples": {"@id": "cr:examples", "@type": "@json"},
    "extract": "cr:extract",
    "field": "cr:field",
    "fileObject": "cr:fileObject",
    "fileProperty": "cr:fileProperty",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "parentField": "cr:parentField",
    "path": "cr:path",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
}


# Context written by ConsentVid before the cv: namespace existed (commit
# 3fc54ea..533c2a3). resync accepts exactly these two contexts and nothing
# else: any other context can re-map keys (e.g. alias another term to
# schema.org/contentUrl) so that a Croissant loader and resync read different
# data from the same JSON.
LEGACY_CONTEXTS = (
    {
        **{k: v for k, v in CROISSANT_CONTEXT.items() if k != "cv"},
        "equivalentProperty": {"@id": "sc:equivalentProperty", "@type": "@vocab"},
        "samplingRate": "cr:samplingRate",
    },
)


@dataclass
class Clip:
    consent_uri: str
    consent_cid: str
    consent: dict
    video_uri: str
    video_cid: str
    video: dict
    did: str
    handle: str | None = None


def receipt_of(file_object: dict) -> dict:
    return file_object.get("cv:consentReceipt") or file_object.get("cr:consentReceipt") or {}


def video_uri_of(file_object: dict) -> str | None:
    return file_object.get("cv:atUri") or file_object.get("cr:atUri")


def _file_object(clip: Clip) -> dict | None:
    v, c = clip.video, clip.consent
    storage = v.get("storageRef") or {}
    url = public_url(storage)
    if not url:
        return None  # not dereferenceable: can't be part of a shareable dataset
    return {
        "@type": "cr:FileObject",
        "@id": f"{clip.did}/{clip.video_uri.rsplit('/', 1)[-1]}",
        "name": v.get("title") or clip.video_uri.rsplit("/", 1)[-1],
        "description": v.get("description", ""),
        "contentUrl": url,
        "encodingFormat": v.get("mimeType") or mime_for(url) or "application/octet-stream",
        "sha256": (v.get("contentHash") or "").removeprefix("sha256-"),
        "cv:atUri": clip.video_uri,
        "cv:videoCid": clip.video_cid,
        "cv:storageKind": storage.get("kind"),
        "cv:contributor": clip.did,
        "cv:consentReceipt": {
            "atUri": clip.consent_uri,
            "cid": clip.consent_cid,
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
            # Canonical RFC 3339 UTC, so string/JS comparisons downstream agree
            # with ours (None stays None; resync compares normalised values).
            "expiresAt": normalize_dt(c.get("expiresAt")),
            "createdAt": normalize_dt(c.get("createdAt")),
        },
    }


def build_manifest(
    clips: list[Clip], *, name: str, description: str | None = None, version: str = "1.0.0"
) -> tuple[dict, list[Clip]]:
    """Returns (manifest, clips skipped because their bytes aren't public)."""
    today = date.today().isoformat()
    distribution, skipped = [], []
    for clip in clips:
        fo = _file_object(clip)
        (distribution.append(fo) if fo else skipped.append(clip))

    used = [c for c in clips if c not in skipped]
    contributors = sorted({c.did: c.handle for c in used}.items())
    purposes = sorted({p for c in used for p in c.consent.get("purposes", [])})
    classes = sorted({m for c in used for m in c.consent.get("modelClasses", [])})
    modalities = sorted({m for c in used for m in c.consent.get("modalities", [])})
    single = contributors[0] if len(contributors) == 1 else None
    url = f"https://bsky.app/profile/{single[0]}" if single else "https://consentvid.org"

    manifest = {
        "@context": CROISSANT_CONTEXT,
        "@type": "sc:Dataset",
        "name": name,
        "description": description
        or (
            f"Consent-first video dataset of {len(distribution)} clip(s) from "
            f"{len(contributors)} contributor(s), built with ConsentVid {__version__}. "
            "Every clip carries a signed ATproto consent receipt stating the permitted "
            "purposes, model classes and modalities."
        ),
        "conformsTo": "http://mlcommons.org/croissant/1.0",
        "version": version,
        "datePublished": today,
        "isLiveDataset": True,
        "license": LICENSE_URL,
        "url": url,
        "citeAs": f"{name} ({today}). ConsentVid dataset. {url}",
        "creator": [
            {"@type": "sc:Person", "name": handle or did, "identifier": did} for did, handle in contributors
        ],
        "distribution": distribution,
        "recordSet": [],
        "rai:dataCollection": (
            "Videos contributed by their authors via ConsentVid. Each contributor writes an "
            "org.consentvid.video record and an org.consentvid.consent receipt to their own "
            "ATproto repository, signed by their repository key, including an attestation "
            "that they recorded the video or otherwise hold the right to consent."
        ),
        "rai:dataCollectionType": ["Crowdsourced"],
        "rai:personalSensitiveInformation": (
            f"Modalities consented: {', '.join(modalities) or 'none'}. Clips may show faces "
            "and voices; contributors consented per clip. Identifying container metadata "
            "(location, device) is stripped at upload by default."
        ),
        "rai:dataReleaseMaintenancePlan": (
            "Consent can be withdrawn at any time by deleting the receipt. Consumers MUST run "
            "`consentvid resync <manifest>` before each use; it cryptographically verifies "
            "every receipt against the contributor's repository and drops revoked, expired "
            "or modified clips."
        ),
        "rai:dataUseCases": [
            f"Purposes granted by at least one clip: {', '.join(purposes) or 'none'}",
            f"Model classes granted by at least one clip: {', '.join(classes) or 'none'}",
            "Terms are per clip and authoritative in each cv:consentReceipt; filter on them.",
        ],
        "rai:dataLimitations": (
            "Use is governed by each clip's consent receipt, not by a blanket licence. "
            "Authorship and captureProvenance are self-declared by contributors; "
            "ConsentVid does not yet verify capture-time provenance."
        ),
    }
    return manifest, skipped
