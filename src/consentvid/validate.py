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
use: object / string / integer / boolean / array / ref with required,
maxLength (UTF-8 bytes), minLength, minimum/maximum, enum, const, and the
formats at-uri, datetime, cid, did, language. Per spec ``knownValues`` is
advisory (open) while ``enum`` is closed, and unknown fields are ignored.
``maxGraphemes``/``minGraphemes`` are NOT implemented (they count grapheme
clusters, not code points); no ConsentVid lexicon uses them, and a test keeps
it that way.

A consent's ``createdAt`` is informational: there is at most one consent per
video (the sidecar rule), so no ordering between consents is ever needed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from consentvid.lexicons import load_lexicon
from consentvid.records import (
    CONSENT_NSID,
    VIDEO_MIME_TYPES,
    VIDEO_NSID,
    has_unsafe_chars,
    https_url_error,
    is_did,
    is_ipfs_cid,
    is_tid,
    parse_at_uri,
    parse_dt,
    url_host_port_error,
)
from consentvid.vocab import IDENTITY_MODALITIES, PEOPLE_PRESENT

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
    errors = _validate_object(schema, record, defs, "")
    if nsid == CONSENT_NSID:
        errors.extend(_consent_rules(record))
    elif nsid == VIDEO_NSID:
        errors.extend(_video_rules(record))
    return errors


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
    # 'local' (and kinds we don't know): opaque names, never dereferenced.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return ["storageRef.value must not contain control characters"]
    return []


def consent_ownership_errors(consent_did: str, record: dict) -> list[str]:
    """A consent may only reference a video in the SAME repo.

    The authorship attestation is signed by the consent writer's key; it means
    nothing for a video in someone else's repo. Without this rule any account
    could attach consent to (and attest authorship of) another person's video.
    """
    try:
        video = parse_at_uri(record.get("videoUri", ""), VIDEO_NSID)
    except ValueError as e:
        return [f"videoUri: {e}"]
    if video.did != consent_did:
        return [
            f"videoUri belongs to {video.did}, but the consent is written by "
            f"{consent_did}: consent may only reference the writer's own video"
        ]
    return []


def sidecar_key_errors(record: dict, rkey: str) -> list[str]:
    """A consent's record key must equal its video's record key. That makes
    the consent a sidecar of the video (as a threadgate is of its post), so
    the PDS itself allows at most one consent per video, and no reader ever
    has to choose between several."""
    try:
        video = parse_at_uri(record.get("videoUri", ""), VIDEO_NSID)
    except (ValueError, AttributeError):
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
    if nsid == CONSENT_NSID and isinstance(record, dict):
        if not errors:
            errors.extend(consent_ownership_errors(repo_did, record))
        if rkey is not None:
            errors.extend(sidecar_key_errors(record, rkey))
    return errors


def write_policy_errors(nsid: str, record: dict) -> list[str]:
    """Hygiene our CLI enforces on write. Records are public and permanent in
    the firehose history, so mistakes here can't be taken back."""
    errors = []
    if nsid == VIDEO_NSID:
        ref = record.get("storageRef") or {}
        value = ref.get("value", "")
        if ref.get("kind") == "local" and ("/" in value or "\\" in value):
            errors.append(
                "storageRef.value for 'local' must be a bare name, not a filesystem "
                "path: records are public and a path leaks your username and folders"
            )
    return errors


# --------------------------------------------------------------- schema ------


def _validate_object(schema: dict, value, defs: dict, path: str) -> list[str]:
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
        errors.extend(_validate_field(field_schema, val, defs, f"{path}{key}"))
    return errors


def _validate_field(field_schema: dict, value, defs: dict, path: str) -> list[str]:
    ftype = field_schema.get("type")

    if ftype == "ref":
        ref = field_schema.get("ref", "")
        target = defs.get(ref[1:]) if ref.startswith("#") else None
        if target is None:
            return [f"{path}: unresolvable ref {ref!r}"]
        if target.get("type") == "object":
            return _validate_object(target, value, defs, f"{path}.")
        return _validate_field(target, value, defs, path)

    if ftype == "object":
        return _validate_object(field_schema, value, defs, f"{path}.")

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
            errs.extend(_validate_field(item_schema, item, defs, f"{path}[{i}]"))
        return errs

    return []  # unsupported type: don't block


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
