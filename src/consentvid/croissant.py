"""Croissant-RAI manifest generation from consentvid records.

Spec: http://mlcommons.org/croissant/  (version 1.0)
RAI extension: http://mlcommons.org/croissant/RAI/

Validate output with `pip install mlcroissant` then `mlcroissant validate <path>`.
"""
from __future__ import annotations

from datetime import date


CROISSANT_CONTEXT = {
    "@language": "en",
    "@vocab": "https://schema.org/",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "cr": "http://mlcommons.org/croissant/",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "data": {"@id": "cr:data", "@type": "@json"},
    "dataType": {"@id": "cr:dataType", "@type": "@vocab"},
    "dct": "http://purl.org/dc/terms/",
    "equivalentProperty": {"@id": "sc:equivalentProperty", "@type": "@vocab"},
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
    "samplingRate": "cr:samplingRate",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform",
}


def _summarize(consents: list[dict]) -> dict:
    return {
        "purposes": sorted({p for c in consents for p in c.get("purposes", [])}),
        "modelClasses": sorted({m for c in consents for m in c.get("modelClasses", [])}),
        "modalities": sorted({m for c in consents for m in c.get("modalities", [])}),
        "anyExpiry": any(c.get("expiresAt") for c in consents),
        "allRevocable": all(c.get("revocable") for c in consents) if consents else False,
    }


def _file_object(video_uri: str, video: dict, consent_uri: str, consent: dict) -> dict:
    from consentvid.storage import public_url

    storage = video.get("storageRef") or {}
    sha256_hex = (video.get("contentHash") or "").removeprefix("sha256-")
    rkey = video_uri.rsplit("/", 1)[-1]
    return {
        "@type": "cr:FileObject",
        "@id": f"video-{rkey}",
        "name": video.get("title") or rkey,
        "description": video.get("description", ""),
        "contentUrl": public_url(storage),
        "encodingFormat": "video/*",
        "sha256": sha256_hex,
        "cr:atUri": video_uri,
        "cr:storageKind": storage.get("kind", "url"),
        "cr:consentReceipt": {
            "atUri": consent_uri,
            "purposes": consent.get("purposes", []),
            "modelClasses": consent.get("modelClasses", []),
            "modalities": consent.get("modalities", []),
            "compensation": consent.get("compensation", "none"),
            "revocable": consent.get("revocable", False),
            "authorshipAttested": consent.get("authorshipAttested", False),
            "rightsBasis": consent.get("rightsBasis"),
            "captureProvenance": consent.get("captureProvenance", "unverified"),
            "expiresAt": consent.get("expiresAt"),
            "createdAt": consent.get("createdAt"),
        },
    }


def build_manifest(
    *,
    creator_handle: str,
    creator_did: str,
    consents_with_uri: list[tuple[str, dict]],
    videos_by_uri: dict[str, dict],
    name: str | None = None,
    description: str | None = None,
    version: str = "0.1.0",
) -> dict:
    today = date.today().isoformat()
    if name is None:
        name = f"consentvid-{creator_handle.replace('.', '-')}-{today}"
    if description is None:
        description = (
            f"Consent-first video dataset registered by {creator_handle} via ConsentVid. "
            "Each clip carries a cryptographically signed ATproto consent receipt declaring "
            "permitted purposes, model classes, modalities, and revocation status."
        )

    matched = [
        (uri, c, videos_by_uri[c["videoUri"]])
        for uri, c in consents_with_uri
        if c.get("videoUri") in videos_by_uri
    ]
    summary = _summarize([c for _, c, _ in matched])
    distribution = [_file_object(c["videoUri"], v, uri, c) for uri, c, v in matched]

    return {
        "@context": CROISSANT_CONTEXT,
        "@type": "sc:Dataset",
        "name": name,
        "description": description,
        "conformsTo": "http://mlcommons.org/croissant/1.0",
        "version": version,
        "datePublished": today,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "citeAs": (
            f"{creator_handle} ({today}). {name}. ConsentVid dataset. "
            f"https://bsky.app/profile/{creator_handle}"
        ),
        "url": f"https://bsky.app/profile/{creator_handle}",
        "creator": {
            "@type": "sc:Person",
            "name": creator_handle,
            "identifier": creator_did,
        },
        "distribution": distribution,
        "recordSet": [],
        "rai:dataCollection": (
            "Videos contributed by ATproto users via ConsentVid. Each contribution writes "
            "a cryptographically signed org.consentvid.video record to the contributor's repo, "
            "plus a org.consentvid.consent receipt declaring permitted uses."
        ),
        "rai:dataCollectionType": ["Crowdsourced"],
        "rai:personalSensitiveInformation": (
            f"Modalities present: {', '.join(summary['modalities']) or 'none declared'}. "
            "Per-clip declarations live in each FileObject's cr:consentReceipt."
        ),
        "rai:dataReleaseMaintenancePlan": (
            "Revocations are published as deletions on the ATproto firehose. Dataset consumers "
            "MUST re-sync consent records before each training run and drop any revoked clips."
        ),
        "rai:dataUseCases": [
            f"Permitted purposes (union): {', '.join(summary['purposes']) or 'none'}",
            f"Permitted model classes (union): {', '.join(summary['modelClasses']) or 'none'}",
            "Per-clip consent is authoritative; the union here is informational only.",
        ],
        "rai:dataLimitations": (
            f"{'Some' if summary['anyExpiry'] else 'No'} clips have expiry dates. "
            f"{'All' if summary['allRevocable'] else 'Not all'} clips are revocable. "
            "See per-FileObject cr:consentReceipt for authoritative terms."
        ),
    }
