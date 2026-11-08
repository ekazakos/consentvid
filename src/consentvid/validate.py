"""Validate org.consentvid.* records against their lexicons + domain rules.

bsky.social does not resolve third-party lexicons on write (on-demand
resolution is optional per the AT Protocol spec), so schema checking is ours to
do. Three entry points, from weakest to strongest:

* ``validate_record(nsid, record)``  – lexicon schema + record-local rules.
* ``validate_in_repo(nsid, record, did, rkey=...)`` – adds rules that depend on
  where the record lives: its record key must be a TID (lexicon ``key: tid``),
  a consent may only reference a video in its own repo, and a consent's record
  key must equal its video's (the consent is the video's sidecar record, like
  app.bsky.feed.threadgate is a post's). Every ingest path (AppView backfill +
  live index, resync, verify, C2PA) uses this and passes the record key.
* ``write_policy_errors(nsid, record)`` – extra hygiene our own CLI enforces on
  write (e.g. never publish an absolute filesystem path) but which we don't use
  to reject other clients' records.

The schema validator implements the subset of the Lexicon language our records
use: object / string / integer / boolean / array / ref / union with required,
maxLength/minLength (UTF-8 bytes for strings), maxGraphemes/minGraphemes
(extended grapheme clusters, Unicode UAX #29, counted with the ``regex``
package), minimum/maximum, enum, const, and the formats at-uri, datetime, cid,
did, language and uri. Refs and union members may name a def in the same
lexicon (``#name``) or one of the few atproto defs our records use
(com.atproto.repo.strongRef, com.atproto.label.defs#selfLabels), which are
built in. Unions are open unless ``closed``: a member type we don't know is
accepted unchecked. Per spec ``knownValues`` is advisory (open) while
``enum`` is closed, and unknown fields are ignored.

Both record versions are validated: org.consentvid.video/consent (V1) and
org.consentvid.videoV2/consentV2. A consent may only reference a video of
its own version (versions.py).

A consent's ``createdAt`` is informational: there is at most one consent per
video (the sidecar rule), so no ordering between consents is ever needed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import regex

from consentvid.lexicons import load_lexicon
from consentvid.records import (
    CONSENT_NSID,
    CONSENT_NSIDS,
    CONSENT_V2_NSID,
    VIDEO_FOR_CONSENT,
    VIDEO_MIME_TYPES,
    VIDEO_NSID,
    VIDEO_NSIDS,
    VIDEO_V2_NSID,
    has_unsafe_chars,
    https_url_error,
    is_did,
    is_ipfs_cid,
    is_tid,
    parse_at_uri,
    parse_dt,
    url_host_port_error,
)
from consentvid.versions import (
    BSKY_KIND,
    PDS_KIND,
    SELF_LABELS_TYPE,
    blob_parts,
    consent_view,
    storage_ref_of,
    video_view,
)
from consentvid.vocab import IDENTITY_MODALITIES, PEOPLE_PRESENT, PEOPLE_PRESENT_V2

BSKY_POST_NSID = "app.bsky.feed.post"

# Used with fullmatch only ('$' would accept a trailing newline).
_SHA256_RE = re.compile(r"sha256-[0-9a-f]{64}")
_MAX_SAFE_INT = 2**53 - 1
# BCP-47 (RFC 5646 §2.1), ported from @atproto/syntax (language.ts): the
# strict parseLanguageString path, which the atproto interop vectors follow.
_LANG_RE = re.compile(
    r"(?P<grandfathered>(en-GB-oed|i-ami|i-bnn|i-default|i-enochian|i-hak|i-klingon|i-lux|i-mingo|i-navajo"
    r"|i-pwn|i-tao|i-tay|i-tsu|sgn-BE-FR|sgn-BE-NL|sgn-CH-DE)|(art-lojban|cel-gaulish|no-bok|no-nyn"
    r"|zh-guoyu|zh-hakka|zh-min|zh-min-nan|zh-xiang))"
    r"|((?P<language>([A-Za-z]{2,3}(-(?P<extlang>[A-Za-z]{3}(-[A-Za-z]{3}){0,2}))?)|[A-Za-z]{4}|[A-Za-z]{5,8})"
    r"(-(?P<script>[A-Za-z]{4}))?(-(?P<region>[A-Za-z]{2}|[0-9]{3}))?"
    r"(-(?P<variant>[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(-(?P<extension>[0-9A-WY-Za-wy-z](-[A-Za-z0-9]{2,8})+))*"
    r"(-(?P<privateUseA>[xX](-[A-Za-z0-9]{1,8})+))?)"
    r"|(?P<privateUseB>[xX](-[A-Za-z0-9]{1,8})+)",
    re.ASCII,
)
_PRIMARY_RE = re.compile(r"[a-z]{2,3}", re.ASCII)
_VARIANT_RE = re.compile(r"[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}", re.ASCII)
_SINGLETON_RE = re.compile(r"[0-9A-WYZa-wyz]", re.ASCII)


def _duplicate_variant_or_singleton(tag: str) -> bool:
    """RFC 5646 §4.1: no repeated variant or extension singleton (case-
    insensitive); the regex alone can't say."""
    subtags = tag.split("-")
    i = 1
    if len(subtags[0]) in (2, 3):
        count = 0
        while count < 3 and i < len(subtags) and re.fullmatch(r"[A-Za-z]{3}", subtags[i]):
            i, count = i + 1, count + 1
    if i < len(subtags) and re.fullmatch(r"[A-Za-z]{4}", subtags[i]):
        i += 1
    if i < len(subtags) and re.fullmatch(r"[A-Za-z]{2}|[0-9]{3}", subtags[i]):
        i += 1
    seen: set[str] = set()
    while i < len(subtags) and _VARIANT_RE.fullmatch(subtags[i]):
        if subtags[i].lower() in seen:
            return True
        seen.add(subtags[i].lower())
        i += 1
    singletons: set[str] = set()
    while i < len(subtags) and _SINGLETON_RE.fullmatch(subtags[i]):
        if subtags[i].lower() in singletons:
            return True
        singletons.add(subtags[i].lower())
        i += 1
        while i < len(subtags) and re.fullmatch(r"[A-Za-z0-9]{2,8}", subtags[i]):
            i += 1
    return False


def is_language(tag) -> bool:
    """A well-formed BCP-47 tag with a lowercase 2-3 letter primary subtag,
    as @atproto/syntax's strict parser and the interop vectors require."""
    if not isinstance(tag, str):
        return False
    m = _LANG_RE.fullmatch(tag)
    if m is None:
        return False
    if not (m.group("grandfathered") or m.group("privateUseB")) and _duplicate_variant_or_singleton(tag):
        return False
    language = m.group("language")
    return language is None or bool(_PRIMARY_RE.fullmatch(language.split("-")[0]))
_INT64 = (-(2**63), 2**63 - 1)  # atproto integers are 64-bit signed
# atproto.com/specs/lexicon#uri: a URI with a scheme; at most 8 KB.
_URI_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:[^\s]+", re.ASCII)
_URI_MAX = 8192
# The atproto defs our records reference, built in (so validation never
# resolves anything over the network).
_EXTERNAL_DEFS = {
    "com.atproto.repo.strongRef": {
        "type": "object",
        "required": ["uri", "cid"],
        "properties": {"uri": {"type": "string", "format": "at-uri"}, "cid": {"type": "string", "format": "cid"}},
    },
    SELF_LABELS_TYPE: {
        "type": "object",
        "required": ["values"],
        "properties": {
            "values": {"type": "array", "maxLength": 10, "items": {"type": "ref", "ref": "com.atproto.label.defs#selfLabel"}}
        },
    },
    "com.atproto.label.defs#selfLabel": {
        "type": "object",
        "required": ["val"],
        "properties": {"val": {"type": "string", "maxLength": 128}},
    },
}


def grapheme_len(s: str) -> int:
    """Number of extended grapheme clusters (what a reader sees as
    characters: 'e' + combining accent, a flag or a skin-toned emoji is one)."""
    return len(regex.findall(r"\X", s))
_URL_KINDS = ("url", "r2", "peertube")
# Open-vocabulary consent fields: shown to researchers and in terminals.
_VOCAB_LIST_FIELDS = ("purposes", "modelClasses", "modalities")
_VOCAB_STR_FIELDS = ("captureProvenance", "compensation", "rightsBasis", "peoplePresent")


def validate_record(nsid: str, record: dict, *, now: datetime | None = None) -> list[str]:
    """Lexicon schema + record-local domain rules. Empty list == valid.

    Total: records come from untrusted repos and firehoses, so any internal
    failure becomes a validation error rather than an exception that could
    halt an indexer or a re-sync."""
    try:
        lexicon = load_lexicon(nsid)
    except FileNotFoundError:
        return [f"no local lexicon for {nsid}; cannot validate"]
    try:
        return _validate_record(nsid, record, lexicon, now or datetime.now(UTC))
    except Exception as e:  # noqa: BLE001
        return [f"record could not be validated: {type(e).__name__}"]


def _str_list(vals) -> bool:
    return isinstance(vals, list) and all(isinstance(x, str) for x in vals)


def _validate_record(nsid: str, record, lexicon: dict, now: datetime) -> list[str]:
    if not isinstance(record, dict):
        return [f"record must be an object, got {type(record).__name__}"]
    # Records always carry $type (atproto.com/specs/lexicon).
    if "$type" not in record:
        return ["missing $type"]
    if record["$type"] != nsid:
        return [f"$type is {record.get('$type')!r}, expected {nsid!r}"]
    defs = lexicon.get("defs", {})
    schema = defs.get("main", {}).get("record")
    if not isinstance(schema, dict):
        return [f"lexicon {nsid} has no main record schema"]
    errors = _validate_object(schema, record, _Ctx(nsid, defs), "")
    if nsid == CONSENT_NSID:
        errors.extend(_consent_rules(record))
    elif nsid == CONSENT_V2_NSID:
        errors.extend(_consent_v2_rules(record))
    elif nsid == VIDEO_NSID:
        errors.extend(_video_rules(record))
    elif nsid == VIDEO_V2_NSID:
        errors.extend(_video_v2_rules(record))
    return errors


def _consent_v2_rules(record: dict) -> list[str]:
    """V2 consents get the same domain rules as V1, applied to the internal
    view (kebab-case vocabulary mapped back, subject -> videoUri/videoCid),
    plus V2's own: peoplePresent spelled as in V2, and the subject must be
    a videoV2 record."""
    errors = []
    people = record.get("peoplePresent")
    if isinstance(people, str) and people not in PEOPLE_PRESENT_V2:
        errors.append(
            "peoplePresent must be one of " + ", ".join(PEOPLE_PRESENT_V2)
            + ": consent must state who identifiably appears in the video"
        )
    for field in ("modalities", "purposes", "modelClasses"):
        vals = record.get(field)
        if _str_list(vals) and len(set(vals)) != len(vals):
            errors.append(f"{field} contains duplicates")
    subject = record.get("subject")
    if isinstance(subject, dict) and isinstance(subject.get("uri"), str):
        try:
            parse_at_uri(subject["uri"], VIDEO_V2_NSID)
        except ValueError as e:
            errors.append(f"subject.uri: {e}")
    view = consent_view(CONSENT_V2_NSID, record)
    # peoplePresent and duplicate errors were reported above in V2 terms.
    errors.extend(
        e for e in _consent_rules(view)
        if not e.startswith("peoplePresent must be") and not e.endswith("contains duplicates")
        and not (e.startswith("videoCid") and "subject" not in record)
    )
    return [e.replace("videoCid", "subject.cid") for e in errors]


def _video_v2_rules(record: dict) -> list[str]:
    errors = []
    h = record.get("contentHash")
    if isinstance(h, str) and _is_cid(h) and _cid_codec(h) != _RAW:
        errors.append("contentHash must be the CID of the raw bytes (raw codec, bafkrei...), not of a record")
    mime = record.get("mimeType")
    if mime is not None and mime not in VIDEO_MIME_TYPES:
        errors.append(f"mimeType {mime!r} is not a video container type ({', '.join(sorted(VIDEO_MIME_TYPES))})")
    langs = record.get("langs")
    if _str_list(langs) and len(set(langs)) != len(langs):
        errors.append("langs contains duplicates")
    tags = record.get("tags")
    if _str_list(tags) and any(has_unsafe_chars(t) for t in tags):
        errors.append("tags contain control or format characters")
    for val in _self_label_values(record):
        if not val or has_unsafe_chars(val):
            errors.append("labels contain an empty value or control/format characters")
            break
    storage = record.get("storage")
    if isinstance(storage, dict):
        ref = storage_ref_of(storage, VIDEO_V2_NSID)
        errors.extend(e.replace("storageRef.value", "storage") for e in storage_ref_errors(ref))
        if ref.get("kind") == BSKY_KIND and isinstance(h, str) and ref.get("value") != h:
            errors.append("storage.blob must equal contentHash: the post's video blob is the registered bytes")
        if ref.get("kind") == PDS_KIND:
            errors.extend(_pds_blob_rules(record, ref))
    return errors


def _pds_blob_rules(record: dict, ref: dict) -> list[str]:
    """A #pdsBlobRef binds to the record's bytes: same CID as contentHash,
    same size, same container type."""
    from consentvid.records import same_container

    errors = []
    h = record.get("contentHash")
    if isinstance(h, str) and ref.get("value") and ref.get("value") != h:
        errors.append("storage.blob.ref must equal contentHash: the blob is the registered bytes")
    size = record.get("size")
    if isinstance(size, int) and isinstance(ref.get("size"), int) and size != ref["size"]:
        errors.append(f"storage.blob.size {ref['size']} differs from size {size}")
    mime, blob_mime = record.get("mimeType"), ref.get("mimeType")
    if isinstance(mime, str) and isinstance(blob_mime, str) and not same_container(mime, blob_mime):
        errors.append(f"storage.blob.mimeType {blob_mime!r} is not the same container as mimeType {mime!r}")
    return errors


def _self_label_values(record: dict) -> list:
    labels = record.get("labels")
    if not isinstance(labels, dict) or labels.get("$type") != SELF_LABELS_TYPE:
        return []
    values = labels.get("values")
    return [v.get("val") for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _consent_rules(record: dict) -> list[str]:
    errors = []
    # The lexicon can require presence but not value: a receipt without an
    # affirmative authorship attestation must never be honoured.
    if record.get("authorshipAttested") is not True:
        errors.append("authorshipAttested must be true: consent requires an explicit authorship attestation")
    # Consent to processing personal data (faces, voices) must be withdrawable
    # (GDPR Art. 7(3)); a "non-revocable" receipt is not valid consent.
    if record.get("revocable") is not True:
        errors.append("revocable must be true: consent must always be withdrawable")
    # Bystanders: the authorship attestation covers rights to the recording,
    # not the people in it. Every consent must say who appears.
    people = record.get("peoplePresent")
    if people not in PEOPLE_PRESENT:
        errors.append(
            "peoplePresent must be one of " + ", ".join(PEOPLE_PRESENT)
            + ": consent must state who identifiably appears in the video"
        )
    elif people == "noIdentifiablePeople" and _str_list(record.get("modalities") or []):
        bad = IDENTITY_MODALITIES & set(record.get("modalities") or [])
        if bad:
            errors.append(
                f"modalities {sorted(bad)} contradict peoplePresent=noIdentifiablePeople"
            )
    # Minors can't give this consent; self-declared, like authorship.
    if record.get("adultAttested") is not True:
        errors.append("adultAttested must be true: the contributor must affirm they are an adult able to consent")
    for field in ("modalities", "purposes", "modelClasses"):
        vals = record.get(field)
        if _str_list(vals) and len(set(vals)) != len(vals):
            errors.append(f"{field} contains duplicates")
    for field in _VOCAB_LIST_FIELDS:
        vals = record.get(field)
        if _str_list(vals) and any(has_unsafe_chars(x) for x in vals):
            errors.append(f"{field} contains control or format characters")
    for field in _VOCAB_STR_FIELDS:
        v = record.get(field)
        if isinstance(v, str) and has_unsafe_chars(v):
            errors.append(f"{field} contains control or format characters")
    created, expires = parse_dt(record.get("createdAt")), parse_dt(record.get("expiresAt"))
    if created and expires and expires <= created:
        errors.append("expiresAt must be after createdAt")
    # A strong reference to a record: the CID of a dag-cbor block. Optional
    # in the published schema, but required by every reader: an unpinned
    # consent would follow any later rewrite of its video record.
    vcid = record.get("videoCid")
    if not isinstance(vcid, str):
        errors.append("videoCid is required: a consent must pin the exact video version")
    if isinstance(vcid, str) and _is_cid(vcid) and _cid_codec(vcid) != _DAG_CBOR:
        errors.append("videoCid must be the CID of a record (dag-cbor), not of raw bytes")
    return errors


def _video_rules(record: dict) -> list[str]:
    errors = []
    h = record.get("contentHash")
    if isinstance(h, str) and not _SHA256_RE.fullmatch(h):
        errors.append("contentHash must be 'sha256-' + 64 lowercase hex chars")
    for field in ("durationMs", "width", "height"):
        v = record.get(field)
        if isinstance(v, int) and not isinstance(v, bool):
            if v < 0:
                errors.append(f"{field} must be non-negative")
            elif v > _MAX_SAFE_INT:
                # atproto.com/specs/data-model: integers should fit 53 bits,
                # or JS PDSes/AppViews read a different value.
                errors.append(f"{field} {v} exceeds 2^53-1")
    mime = record.get("mimeType")
    if mime is not None and mime not in VIDEO_MIME_TYPES:
        errors.append(f"mimeType {mime!r} is not a video container type ({', '.join(sorted(VIDEO_MIME_TYPES))})")
    lang = record.get("language")
    if isinstance(lang, str) and lang and not is_language(lang):
        errors.append(f"language {lang!r} is not a BCP-47 tag")
    errors.extend(storage_ref_errors(record.get("storageRef")))
    return errors


def storage_ref_errors(ref) -> list[str]:
    """Per-kind syntax of a storageRef. Applies to every record we read, so a
    record pointing at file:, javascript:, internal hosts or '../' paths is
    invalid everywhere (resync, AppView, export)."""
    if not isinstance(ref, dict):
        return []  # the schema check reports it
    kind, value = ref.get("kind"), ref.get("value")
    if not isinstance(kind, str) or not isinstance(value, str):
        return []  # the schema check reports it
    if kind in _URL_KINDS:
        why = https_url_error(value) or url_host_port_error(value)
        return [f"storageRef.value for {kind!r} {why}"] if why else []
    if kind == "ipfs":
        if not is_ipfs_cid(value.removeprefix("ipfs://")):
            return ["storageRef.value for 'ipfs' must be a CID (bafy... or Qm...), optionally ipfs://-prefixed"]
        return []
    if kind == BSKY_KIND:
        errors = []
        if not (_is_cid(value) and _cid_codec(value) == _RAW):
            errors.append("storage.blob must be the CID of the raw video bytes (bafkrei...)")
        post = ref.get("post") if isinstance(ref.get("post"), dict) else {}
        try:
            parse_at_uri(post.get("uri") if isinstance(post.get("uri"), str) else "", BSKY_POST_NSID)
        except ValueError as e:
            errors.append(f"storage.post.uri must be an app.bsky.feed.post URI: {e}")
        cid = post.get("cid")
        if isinstance(cid, str) and _is_cid(cid) and _cid_codec(cid) != _DAG_CBOR:
            errors.append("storage.post.cid must be the CID of a record (dag-cbor)")
        return errors
    if kind == PDS_KIND:
        if not (_is_cid(value) and _cid_codec(value) == _RAW):
            return ["storage.blob.ref must be the CID of the raw video bytes (bafkrei...)"]
        return []
    # 'local' (and kinds we don't know): opaque names, never dereferenced.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return ["storageRef.value must not contain control characters"]
    return []


def consent_ownership_errors(consent_did: str, record: dict, nsid: str = CONSENT_NSID) -> list[str]:
    """A consent may only reference a video in the SAME repo.

    The authorship attestation is signed by the consent writer's key; it means
    nothing for a video in someone else's repo. Without this rule any account
    could attach consent to (and attest authorship of) another person's video.
    """
    video = _video_at(record, nsid)
    if isinstance(video, str):
        return [video]
    if video.did != consent_did:
        return [
            f"videoUri belongs to {video.did}, but the consent is written by "
            f"{consent_did}: consent may only reference the writer's own video"
        ]
    return []


def post_ownership_errors(repo_did: str, record: dict) -> list[str]:
    """A videoV2 record may only point at a Bluesky post in its own repo:
    otherwise anyone could register, and consent for, someone else's video."""
    ref = storage_ref_of(record.get("storage"), VIDEO_V2_NSID)
    if ref.get("kind") != BSKY_KIND:
        return []
    try:
        post = parse_at_uri((ref.get("post") or {}).get("uri") or "", BSKY_POST_NSID)
    except ValueError:
        return []  # reported by storage_ref_errors
    if post.did != repo_did:
        return [
            f"storage.post belongs to {post.did}, but the video record is in {repo_did}'s repo: "
            "only your own Bluesky posts can be registered"
        ]
    return []


def _video_at(record: dict, nsid: str = CONSENT_NSID):
    """The AtUri of the video a consent references (V1 videoUri / V2
    subject.uri, which must name a video of the same version), or an error
    message."""
    view = consent_view(nsid, record) if isinstance(record, dict) else {}
    uri = view.get("videoUri")
    try:
        return parse_at_uri(uri if isinstance(uri, str) else "", VIDEO_FOR_CONSENT[nsid])
    except ValueError as e:
        return f"{'videoUri' if nsid == CONSENT_NSID else 'subject.uri'}: {e}"


def sidecar_key_errors(record: dict, rkey: str, nsid: str = CONSENT_NSID) -> list[str]:
    """A consent's record key must equal its video's record key. That makes
    the consent a sidecar of the video (as a threadgate is of its post), so
    the PDS itself allows at most one consent per video, and no reader ever
    has to choose between several."""
    video = _video_at(record, nsid)
    if isinstance(video, str):
        return []  # reported by the schema / ownership checks
    if video.rkey != rkey:
        return ["consent record key must equal its video's record key (sidecar rule)"]
    return []


def validate_in_repo(
    nsid: str, record: dict, repo_did: str, *, rkey: str | None = None, now: datetime | None = None
) -> list[str]:
    """validate_record plus rules that depend on where the record lives.
    ``rkey`` is the record's key in the repo; pass it whenever it is known
    (every read path knows it). None skips the key rules (e.g. before a PDS
    assigns the key)."""
    errors = validate_record(nsid, record, now=now)
    if rkey is not None and not is_tid(rkey):
        errors.append(f"record key {rkey!r} is not a TID (lexicon key: tid)")
    if nsid == VIDEO_V2_NSID and isinstance(record, dict) and not errors:
        errors.extend(post_ownership_errors(repo_did, record))
    if nsid in CONSENT_NSIDS and isinstance(record, dict):
        if not errors:
            errors.extend(consent_ownership_errors(repo_did, record, nsid))
        if rkey is not None:
            errors.extend(sidecar_key_errors(record, rkey, nsid))
    return errors


def write_policy_errors(nsid: str, record: dict) -> list[str]:
    """Hygiene our CLI enforces on write. Records are public and permanent in
    the firehose history, so mistakes here can't be taken back."""
    errors = []
    if nsid in VIDEO_NSIDS:
        ref = video_view(nsid, record).get("storageRef") or {}
        value = ref.get("value", "")
        if ref.get("kind") == "local" and ("/" in value or "\\" in value):
            errors.append(
                "storageRef.value for 'local' must be a bare name, not a filesystem "
                "path: records are public and a path leaks your username and folders"
            )
    return errors


# --------------------------------------------------------------- schema ------


class _Ctx:
    """The lexicon being validated against: its NSID and defs."""

    def __init__(self, nsid: str, defs: dict):
        self.nsid, self.defs = nsid, defs

    def resolve(self, ref: str) -> dict | None:
        """The def a ref names: '#name' or '<this nsid>#name' locally, else a
        built-in atproto def. None if unknown."""
        if not isinstance(ref, str):
            return None
        if ref.startswith("#"):
            return self.defs.get(ref[1:])
        base, _, name = ref.partition("#")
        if base == self.nsid:
            return self.defs.get(name or "main")
        return _EXTERNAL_DEFS.get(ref if name and name != "main" else base)

    def full(self, ref: str) -> str:
        """A ref as the $type a union member carries: '#x' -> '<nsid>#x',
        'nsid#main' -> 'nsid'."""
        if ref.startswith("#"):
            ref = f"{self.nsid}{ref}"
        return ref[: -len("#main")] if ref.endswith("#main") else ref


def _validate_object(schema: dict, value, ctx: _Ctx, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path or '<root>'}: expected object, got {_typename(value)}"]
    errors: list[str] = []
    for req in schema.get("required", []):
        if req not in value:
            errors.append(f"missing required field: {path}{req}")
    props = schema.get("properties", {})
    for key, val in value.items():
        if key == "$type":
            continue
        field_schema = props.get(key)
        if field_schema is None:
            continue  # unknown fields are ignored, per spec
        if val is None and key not in schema.get("nullable", []):
            errors.append(f"{path}{key}: null is not allowed")
            continue
        errors.extend(_validate_field(field_schema, val, ctx, f"{path}{key}"))
    return errors


def _validate_field(field_schema: dict, value, ctx: _Ctx, path: str) -> list[str]:
    ftype = field_schema.get("type")

    if ftype == "ref":
        ref = field_schema.get("ref", "")
        target = ctx.resolve(ref)
        if target is None:
            return [f"{path}: unresolvable ref {ref!r}"]
        if target.get("type") == "object":
            return _validate_object(target, value, ctx, f"{path}.")
        return _validate_field(target, value, ctx, path)

    if ftype == "union":
        return _validate_union(field_schema, value, ctx, path)

    if ftype == "object":
        return _validate_object(field_schema, value, ctx, f"{path}.")

    if ftype == "string":
        return _validate_string(field_schema, value, path)

    if ftype == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path}: expected integer, got {_typename(value)}"]
        if not _INT64[0] <= value <= _INT64[1]:
            return [f"{path}: {value} is outside the 64-bit integer range"]
        errs = []
        if "minimum" in field_schema and value < field_schema["minimum"]:
            errs.append(f"{path}: {value} below minimum {field_schema['minimum']}")
        if "maximum" in field_schema and value > field_schema["maximum"]:
            errs.append(f"{path}: {value} above maximum {field_schema['maximum']}")
        if "enum" in field_schema and value not in field_schema["enum"]:
            errs.append(f"{path}: {value} not in enum {field_schema['enum']}")
        return errs

    if ftype == "blob":
        return _validate_blob(field_schema, value, path)

    if ftype == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean, got {_typename(value)}"]
        if "const" in field_schema and value != field_schema["const"]:
            return [f"{path}: must be {field_schema['const']}"]
        return []

    if ftype == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {_typename(value)}"]
        errs = []
        if "minLength" in field_schema and len(value) < field_schema["minLength"]:
            errs.append(f"{path}: {len(value)} items, minLength {field_schema['minLength']}")
        if "maxLength" in field_schema and len(value) > field_schema["maxLength"]:
            errs.append(f"{path}: {len(value)} items, maxLength {field_schema['maxLength']}")
        item_schema = field_schema.get("items", {})
        for i, item in enumerate(value):
            errs.extend(_validate_field(item_schema, item, ctx, f"{path}[{i}]"))
        return errs

    return []  # unsupported type: don't block


def _validate_blob(field_schema: dict, value, path: str) -> list[str]:
    """An atproto blob ref ({"$type": "blob", "ref", "mimeType", "size"}),
    with the lexicon's accept and maxSize: a PDS checks these only for the
    lexicons it knows, so readers must. The legacy {cid, mimeType} form is
    refused (records written today never use it)."""
    if not isinstance(value, dict):
        return [f"{path}: expected blob, got {_typename(value)}"]
    if value.get("$type") != "blob":
        return [f"{path}: a blob ref needs $type 'blob'"]
    cid, mime, size = blob_parts(value)
    errs = []
    if not cid or not _is_cid(cid):
        errs.append(f"{path}.ref: not a CID link")
    if not mime or "/" not in mime:
        errs.append(f"{path}.mimeType: missing or not a MIME type")
    if size is None or size < 0:
        errs.append(f"{path}.size: missing or not a non-negative integer")
    accept = field_schema.get("accept")
    if mime and isinstance(accept, list) and accept and not any(_mime_accepts(a, mime) for a in accept):
        errs.append(f"{path}.mimeType: {mime!r} is not one of {accept}")
    max_size = field_schema.get("maxSize")
    if size is not None and isinstance(max_size, int) and size > max_size:
        errs.append(f"{path}.size: {size} bytes exceeds maxSize {max_size}")
    return errs


def _mime_accepts(pattern: str, mime: str) -> bool:
    mime = mime.split(";", 1)[0].strip().lower()
    pattern = pattern.lower()
    if pattern == "*/*":
        return True
    if pattern.endswith("/*"):
        return mime.startswith(pattern[:-1])
    return mime == pattern


def _validate_union(field_schema: dict, value, ctx: _Ctx, path: str) -> list[str]:
    """A union value is an object whose $type names its member. Open unions
    (the default) accept members we don't know, unchecked."""
    if not isinstance(value, dict):
        return [f"{path}: expected object, got {_typename(value)}"]
    t = value.get("$type")
    if not isinstance(t, str) or not t:
        return [f"{path}: union member has no $type"]
    for ref in field_schema.get("refs", []):
        if ctx.full(ref) == t:
            target = ctx.resolve(ref)
            if target is None:
                return [f"{path}: unresolvable ref {ref!r}"]
            return _validate_object(target, value, ctx, f"{path}.")
    if field_schema.get("closed"):
        return [f"{path}: $type {t!r} is not one of {field_schema.get('refs')}"]
    return []


def _validate_string(field_schema: dict, value, path: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{path}: expected string, got {_typename(value)}"]
    errs = []
    if "enum" in field_schema and value not in field_schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {field_schema['enum']}")
    if "const" in field_schema and value != field_schema["const"]:
        errs.append(f"{path}: must be {field_schema['const']!r}")
    n = len(value.encode("utf-8"))
    if "maxLength" in field_schema and n > field_schema["maxLength"]:
        errs.append(f"{path}: {n} bytes exceeds maxLength {field_schema['maxLength']}")
    if "minLength" in field_schema and n < field_schema["minLength"]:
        errs.append(f"{path}: {n} bytes below minLength {field_schema['minLength']}")
    if "maxGraphemes" in field_schema or "minGraphemes" in field_schema:
        g = grapheme_len(value)
        if "maxGraphemes" in field_schema and g > field_schema["maxGraphemes"]:
            errs.append(f"{path}: {g} characters exceeds maxGraphemes {field_schema['maxGraphemes']}")
        if "minGraphemes" in field_schema and g < field_schema["minGraphemes"]:
            errs.append(f"{path}: {g} characters below minGraphemes {field_schema['minGraphemes']}")
    fmt = field_schema.get("format")
    if fmt == "datetime" and parse_dt(value) is None:
        errs.append(f"{path}: {value!r} is not an atproto datetime (ISO-8601 with timezone)")
    elif fmt == "at-uri":
        try:
            parse_at_uri(value)
        except ValueError as e:
            errs.append(f"{path}: {e}")
    elif fmt == "did" and not is_did(value):
        errs.append(f"{path}: {value!r} is not a DID")
    elif fmt == "cid" and not _is_cid(value):
        errs.append(f"{path}: {value!r} is not a CID")
    elif fmt == "language" and not is_language(value):
        errs.append(f"{path}: {value!r} is not a BCP-47 language tag")
    elif fmt == "uri" and (len(value) > _URI_MAX or not _URI_RE.fullmatch(value)):
        errs.append(f"{path}: {value[:80]!r} is not a URI")
    return errs


_DAG_CBOR, _RAW, _SHA256 = 0x71, 0x55, 0x12


def _is_cid(value: str) -> bool:
    """A CID as atproto's data model allows it in records: CIDv1, dag-cbor or
    raw, sha-256 with a 32-byte digest, in canonical lowercase base32 ('b...')
    so that string comparison is CID comparison. CIDv0, other bases and other
    hashes are refused."""
    if not isinstance(value, str) or not value.startswith("b") or value != value.lower():
        return False
    try:
        import libipld

        cid = libipld.decode_cid(value)
        h = cid["hash"]
        if cid["version"] != 1 or cid["codec"] not in (_DAG_CBOR, _RAW):
            return False
        if h["code"] != _SHA256 or h["size"] != 32 or len(h["digest"]) != 32:
            return False
        raw = bytes([1, cid["codec"], _SHA256, 32]) + h["digest"]
        return libipld.encode_cid(raw) == value
    except Exception:  # noqa: BLE001
        return False


def _cid_codec(value: str) -> int | None:
    try:
        import libipld

        return libipld.decode_cid(value)["codec"]
    except Exception:  # noqa: BLE001
        return None


def _typename(value) -> str:
    return type(value).__name__
