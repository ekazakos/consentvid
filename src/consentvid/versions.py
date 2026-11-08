"""Record versions: org.consentvid.video/consent (V1) and videoV2/consentV2.

Writers write only V2. Readers accept both, and pair a V1 video only with a
V1 consent and a V2 video only with a V2 consent.

Everything past validation works on one internal *view* of a record, which
keeps the V1 field names (``contentHash`` as 'sha256-<hex>', ``storageRef``
{kind, value}, ``videoUri`` + ``videoCid``, lowerCamelCase vocabulary).
Manifests and C2PA receipts use the same vocabulary, so a receipt reads the
same whichever version the signed record is. ``video_view`` and
``consent_view`` take a record that already passed validation; the
``*_record_v2`` functions build the V2 record a writer publishes from a view.
"""

from __future__ import annotations

import re

from consentvid.records import (
    CONSENT_NSID,
    CONSENT_NSIDS,
    CONSENT_V2_NSID,
    VIDEO_NSID,
    VIDEO_V2_NSID,
    raw_cid_from_sha256,
)

SELF_LABELS_TYPE = "com.atproto.label.defs#selfLabels"

# V2 storage union member -> V1 storageRef kind, and the field holding the value.
_STORAGE_V2 = {
    "r2Ref": ("r2", "url"),
    "ipfsRef": ("ipfs", "cid"),
    "peertubeRef": ("peertube", "url"),
    "urlRef": ("url", "url"),
    "localRef": ("local", "name"),
}
_STORAGE_KIND_TO_V2 = {kind: (name, field) for name, (kind, field) in _STORAGE_V2.items()}

# Consent fields whose values are vocabulary (kebab-case in V2).
_VOCAB_LISTS = ("modalities", "purposes", "modelClasses")
_VOCAB_STRS = ("compensation", "rightsBasis", "captureProvenance", "peoplePresent")
# Optional fields copied unchanged between the versions.
_VIDEO_COMMON = ("title", "description", "mimeType", "durationMs", "width", "height", "tags", "createdAt")


def is_v2(nsid: str) -> bool:
    return nsid in (VIDEO_V2_NSID, CONSENT_V2_NSID)


def kebab_to_camel(value):
    """'open-weights' -> 'openWeights' (V2 vocabulary -> the internal one).
    Non-strings pass through."""
    if not isinstance(value, str):
        return value
    return re.sub(r"-([a-z0-9])", lambda m: m.group(1).upper(), value)


def camel_to_kebab(value):
    """'openWeights' -> 'open-weights' (the internal vocabulary -> V2)."""
    if not isinstance(value, str):
        return value
    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "-" + m.group(1).lower(), value)


def _union_name(value: dict, nsid: str) -> str | None:
    """The local def name of a union member of ``nsid`` ('#r2Ref' or
    'org.consentvid.videoV2#r2Ref' -> 'r2Ref'), else None."""
    t = value.get("$type") if isinstance(value, dict) else None
    if not isinstance(t, str):
        return None
    if t.startswith("#"):
        return t[1:]
    prefix = f"{nsid}#"
    return t[len(prefix) :] if t.startswith(prefix) else None


BSKY_KIND = "bsky"  # internal kind of #bskyPostRef (consentvid.bsky)
PDS_KIND = "pds"  # internal kind of #pdsBlobRef (a blob in the contributor's own PDS)
BLOB_TYPE = "blob"


def blob_parts(blob) -> tuple[str, str | None, int | None]:
    """(cid, mimeType, size) of an atproto blob ref, in any shape a record
    arrives in: atproto JSON ({"$link": ...}), a decoded DAG-CBOR link
    (binary CID, from a verified CAR read) or a plain string. ('', None,
    None) if it isn't a blob ref."""
    if not isinstance(blob, dict) or blob.get("$type") != BLOB_TYPE:
        return "", None, None
    from consentvid.bsky import _link

    cid = _link(blob.get("ref")) or ""
    mime = blob.get("mimeType")
    size = blob.get("size")
    return (
        cid,
        mime if isinstance(mime, str) else None,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


def storage_ref_of(storage, nsid: str = VIDEO_V2_NSID, did: str | None = None) -> dict:
    """A V2 storage union value as a V1 storageRef. A storage type we don't
    know becomes {'kind': <its $type>, 'value': ''}: opaque, never
    dereferenced, so its bytes are unreachable for us. ``#bskyPostRef``
    becomes {'kind': 'bsky', 'value': <blob cid>, 'post': {'uri', 'cid'}};
    ``#pdsBlobRef`` becomes {'kind': 'pds', 'value': <blob cid>, 'mimeType',
    'size'} plus 'did' (the repo whose PDS serves it) when ``did`` is given."""
    if not isinstance(storage, dict):
        return {}
    name = _union_name(storage, nsid)
    if name == "pdsBlobRef":
        cid, mime, size = blob_parts(storage.get("blob"))
        ref = {"kind": PDS_KIND, "value": cid, "mimeType": mime, "size": size}
        if did:
            ref["did"] = did
        return ref
    if name == "bskyPostRef":
        post = storage.get("post") if isinstance(storage.get("post"), dict) else {}
        blob = storage.get("blob")
        return {
            "kind": BSKY_KIND,
            "value": blob if isinstance(blob, str) else "",
            "post": {"uri": post.get("uri"), "cid": post.get("cid")},
        }
    if name in _STORAGE_V2:
        kind, field = _STORAGE_V2[name]
        value = storage.get(field)
        return {"kind": kind, "value": value if isinstance(value, str) else ""}
    t = storage.get("$type")
    return {"kind": t if isinstance(t, str) else "unknown", "value": ""}


def self_labels(record: dict) -> list[str]:
    """The self-label values of a V2 video record (or a view)."""
    labels = record.get("labels") if isinstance(record, dict) else None
    if isinstance(labels, list):  # a view
        return [v for v in labels if isinstance(v, str)]
    if not isinstance(labels, dict) or labels.get("$type") != SELF_LABELS_TYPE:
        return []
    values = labels.get("values")
    if not isinstance(values, list):
        return []
    return [v["val"] for v in values if isinstance(v, dict) and isinstance(v.get("val"), str)]


def content_hash_of_cid(cid: str) -> str | None:
    """'sha256-<hex>' for a raw-codec sha-256 CIDv1, else None."""
    try:
        import libipld

        info = libipld.decode_cid(cid)
        h = info["hash"]
        if info["version"] != 1 or info["codec"] != 0x55 or h["code"] != 0x12 or len(h["digest"]) != 32:
            return None
        return "sha256-" + h["digest"].hex()
    except Exception:  # noqa: BLE001
        return None


def video_view(nsid: str, record: dict, did: str | None = None) -> dict:
    """The internal view of a video record of either version. ``did`` (the
    repo the record lives in) is needed to dereference PDS-blob storage."""
    if nsid == VIDEO_NSID:
        view = dict(record)
        view.setdefault("labels", [])
        ref = view.get("storageRef")
        if isinstance(ref, dict) and ref.get("kind") in (BSKY_KIND, PDS_KIND):
            # Bluesky-post and PDS-blob storage exist only in V2: a V1
            # record claiming them is opaque, never dereferenced.
            view["storageRef"] = {"kind": f"v1:{ref['kind']}", "value": ""}
        return view
    view = {k: record[k] for k in _VIDEO_COMMON if k in record}
    view["contentHash"] = content_hash_of_cid(record.get("contentHash", "")) or ""
    view["storageRef"] = storage_ref_of(record.get("storage"), nsid, did)
    langs = record.get("langs")
    if isinstance(langs, list) and langs:
        view["language"] = langs[0]
        view["langs"] = list(langs)
    if "size" in record:
        view["size"] = record["size"]
    view["labels"] = self_labels(record)
    return view


def consent_view(nsid: str, record: dict) -> dict:
    """The internal view of a consent record of either version. V2 implies
    revocable=true (every consent is withdrawable)."""
    if nsid == CONSENT_NSID:
        return dict(record)
    view = {}
    subject = record.get("subject") if isinstance(record.get("subject"), dict) else {}
    view["videoUri"] = subject.get("uri")
    view["videoCid"] = subject.get("cid")
    for k in _VOCAB_LISTS:
        if isinstance(record.get(k), list):
            view[k] = [kebab_to_camel(x) for x in record[k]]
    for k in _VOCAB_STRS:
        if k in record:
            view[k] = kebab_to_camel(record[k])
    view["revocable"] = True
    for k in ("authorshipAttested", "adultAttested", "expiresAt", "createdAt"):
        if k in record:
            view[k] = record[k]
    return view


def view(nsid: str, record: dict, did: str | None = None) -> dict:
    return consent_view(nsid, record) if nsid in CONSENT_NSIDS else video_view(nsid, record, did)


def video_record_v2(v: dict) -> dict:
    """The org.consentvid.videoV2 record for the internal view ``v``."""
    ref = v.get("storageRef") or {}
    if ref.get("kind") == BSKY_KIND:
        post = ref.get("post") or {}
        storage = {
            "$type": f"{VIDEO_V2_NSID}#bskyPostRef",
            "post": {"uri": post.get("uri"), "cid": post.get("cid")},
            "blob": ref.get("value", ""),
        }
    elif ref.get("kind") == PDS_KIND:
        storage = {
            "$type": f"{VIDEO_V2_NSID}#pdsBlobRef",
            "blob": {
                "$type": BLOB_TYPE,
                "ref": {"$link": ref.get("value", "")},
                "mimeType": ref.get("mimeType"),
                "size": ref.get("size"),
            },
        }
    else:
        name, field = _STORAGE_KIND_TO_V2[ref.get("kind")]
        storage = {"$type": f"{VIDEO_V2_NSID}#{name}", field: ref.get("value", "")}
    rec = {
        "$type": VIDEO_V2_NSID,
        "title": v["title"],
        "contentHash": raw_cid_from_sha256(v["contentHash"]),
        "storage": storage,
        "createdAt": v["createdAt"],
    }
    for k in ("description", "mimeType", "durationMs", "width", "height", "size"):
        if v.get(k) not in (None, ""):
            rec[k] = v[k]
    langs = v.get("langs") or ([v["language"]] if v.get("language") else [])
    if langs:
        rec["langs"] = list(langs)
    if v.get("tags"):
        rec["tags"] = list(v["tags"])
    if v.get("labels"):
        rec["labels"] = {"$type": SELF_LABELS_TYPE, "values": [{"val": x} for x in v["labels"]]}
    return rec


def consent_record_v2(c: dict) -> dict:
    """The org.consentvid.consentV2 record for the internal view ``c``."""
    rec = {
        "$type": CONSENT_V2_NSID,
        "subject": {"uri": c["videoUri"], "cid": c["videoCid"]},
    }
    for k in _VOCAB_LISTS:
        rec[k] = [camel_to_kebab(x) for x in c.get(k) or []]
    for k in _VOCAB_STRS:
        if c.get(k) is not None:
            rec[k] = camel_to_kebab(c[k])
    rec["authorshipAttested"] = c.get("authorshipAttested") is True
    rec["adultAttested"] = c.get("adultAttested") is True
    if c.get("expiresAt"):
        rec["expiresAt"] = c["expiresAt"]
    rec["createdAt"] = c["createdAt"]
    return rec
