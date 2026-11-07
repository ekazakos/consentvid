"""Validate org.consentvid.* records against their lexicons + domain rules.

bsky.social does not resolve third-party lexicons on write (on-demand
resolution is optional per the AT Protocol spec), so schema checking is ours to
do. Three entry points, from weakest to strongest:

* ``validate_record(nsid, record)``  – lexicon schema + record-local rules.
* ``validate_in_repo(nsid, record, did)`` – adds rules that depend on which repo
  holds the record (a consent may only reference a video in its own repo).
  Every ingest path (AppView backfill + live index, resync, C2PA) uses this.
* ``write_policy_errors(nsid, record)`` – extra hygiene our own CLI enforces on
  write (e.g. never publish an absolute filesystem path) but which we don't use
  to reject other clients' records.

The schema validator implements the subset of the Lexicon language our records
use: object / string / integer / boolean / array / ref with required,
maxLength (UTF-8 bytes), minLength, maxGraphemes, minimum/maximum, enum, const,
and the formats at-uri, datetime, cid, did, language. Per spec ``knownValues``
is advisory (open) while ``enum`` is closed, and unknown fields are ignored.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from consentvid.lexicons import load_lexicon
from consentvid.records import (
    CONSENT_NSID,
    VIDEO_MIME_TYPES,
    VIDEO_NSID,
    has_unsafe_chars,
    https_url_error,
    is_did,
    is_ipfs_cid,
    parse_at_uri,
    parse_dt,
    url_host_port_error,
)
from consentvid.vocab import IDENTITY_MODALITIES, PEOPLE_PRESENT

# Used with fullmatch only ('$' would accept a trailing newline).
_SHA256_RE = re.compile(r"sha256-[0-9a-f]{64}")
_LANG_RE = re.compile(r"(i|[a-zA-Z]{2,3})(-[a-zA-Z0-9]{1,8})*")
_INT64 = (-(2**63), 2**63 - 1)  # atproto integers are 64-bit signed
_URL_KINDS = ("url", "r2", "peertube")
# A createdAt this far ahead of our clock is refused: newest-createdAt wins,
# so a future-dated consent could never be superseded by a later narrowing.
_CLOCK_SKEW = timedelta(days=1)
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
    if record.get("$type", nsid) != nsid:
        return [f"$type is {record.get('$type')!r}, expected {nsid!r}"]
    defs = lexicon.get("defs", {})
    schema = defs.get("main", {}).get("record")
    if not isinstance(schema, dict):
        return [f"lexicon {nsid} has no main record schema"]
    errors = _validate_object(schema, record, defs, "")
    if nsid == CONSENT_NSID:
        errors.extend(_consent_rules(record, now))
    elif nsid == VIDEO_NSID:
        errors.extend(_video_rules(record))
    return errors


def _consent_rules(record: dict, now: datetime) -> list[str]:
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
    if created and created > now + _CLOCK_SKEW:
        errors.append(
            "createdAt is in the future: the newest consent governs, so a future-dated one "
            "could never be replaced; delete it and write a new one"
        )
    return errors


def _video_rules(record: dict) -> list[str]:
    errors = []
    h = record.get("contentHash")
    if isinstance(h, str) and not _SHA256_RE.fullmatch(h):
        errors.append("contentHash must be 'sha256-' + 64 lowercase hex chars")
    for field in ("durationMs", "width", "height"):
        v = record.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and v < 0:
            errors.append(f"{field} must be non-negative")
    mime = record.get("mimeType")
    if mime is not None and mime not in VIDEO_MIME_TYPES:
        errors.append(f"mimeType {mime!r} is not a video container type ({', '.join(sorted(VIDEO_MIME_TYPES))})")
    lang = record.get("language")
    if isinstance(lang, str) and lang and not _LANG_RE.fullmatch(lang):
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


def validate_in_repo(nsid: str, record: dict, repo_did: str, *, now: datetime | None = None) -> list[str]:
    """validate_record plus rules that depend on the repo holding the record."""
    errors = validate_record(nsid, record, now=now)
    if nsid == CONSENT_NSID and not errors:
        errors.extend(consent_ownership_errors(repo_did, record))
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
    if "maxGraphemes" in field_schema and len(value) > field_schema["maxGraphemes"]:
        errs.append(f"{path}: exceeds maxGraphemes {field_schema['maxGraphemes']}")
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
    elif fmt == "language" and not _LANG_RE.fullmatch(value):
        errs.append(f"{path}: {value!r} is not a BCP-47 language tag")
    return errs


def _is_cid(value: str) -> bool:
    try:
        import libipld

        libipld.decode_cid(value)
        return True
    except Exception:  # noqa: BLE001
        return False


def _typename(value) -> str:
    return type(value).__name__
