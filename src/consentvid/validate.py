"""Client-side validation of records against org.consentvid.* lexicons.

bsky.social's PDS does not resolve third-party lexicons at write time (on-demand
resolution is optional per the AT Protocol spec), so records are written with
validate:false / optimistic validation. That leaves schema-checking to us — the
right layer anyway. This module validates a record dict against the local lexicon
definition before we write it (CLI) and before we index it (AppView), so
malformed records never enter the system.

It implements the subset of the Lexicon schema language our records use: object /
string / integer / boolean / array / ref, with required, maxLength (UTF-8 bytes),
maxGraphemes, minLength/maxLength on arrays, enum, and light format checks. Per
the spec, ``knownValues`` is advisory (values outside it are allowed) while
``enum`` is closed; unexpected fields are ignored, not rejected.
"""
from __future__ import annotations

from datetime import datetime

from consentvid.lexicons import load_lexicon

CONSENT_NSID = "org.consentvid.consent"


def validate_record(nsid: str, record: dict) -> list[str]:
    """Validate a record dict against the main record schema of its lexicon.

    Returns a list of human-readable error strings; empty means valid.
    """
    try:
        lexicon = load_lexicon(nsid)
    except FileNotFoundError:
        return [f"no local lexicon for {nsid}; cannot validate"]
    defs = lexicon.get("defs", {})
    main = defs.get("main", {})
    schema = main.get("record")
    if not isinstance(schema, dict):
        return [f"lexicon {nsid} has no main record schema"]
    errors = _validate_object(schema, record, defs, "")
    # Domain policy beyond the schema subset: a consent receipt is only
    # meaningful if the contributor affirmatively attests authorship. The
    # lexicon can require the field be present but not that its value be true, so
    # we enforce authorshipAttested == true here. This single check gates every
    # path — CLI writes (_check_local), AppView backfill, and live Jetstream
    # ingest all run through validate_record — so an unattested consent (e.g. a
    # re-uploaded YouTube clip) never enters the system.
    if nsid == CONSENT_NSID and record.get("authorshipAttested") is not True:
        errors.append(
            "authorshipAttested must be true: a consent receipt requires an "
            "explicit authorship attestation"
        )
    return errors


def _validate_object(schema: dict, value, defs: dict, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{path or '<root>'}: expected object, got {_typename(value)}"]
    for req in schema.get("required", []):
        if req not in value:
            errors.append(f"missing required field: {path}{req}")
    props = schema.get("properties", {})
    for key, val in value.items():
        if key == "$type":
            continue
        field_schema = props.get(key)
        if field_schema is None:
            continue  # unexpected fields are ignored, per spec
        errors.extend(_validate_field(field_schema, val, defs, f"{path}{key}"))
    return errors


def _resolve_ref(field_schema: dict, defs: dict):
    ref = field_schema.get("ref", "")
    if ref.startswith("#"):
        return defs.get(ref[1:])
    # Cross-lexicon refs (nsid#def) are not used by our lexicons.
    return None


def _validate_field(field_schema: dict, value, defs: dict, path: str) -> list[str]:
    ftype = field_schema.get("type")

    if ftype == "ref":
        target = _resolve_ref(field_schema, defs)
        if target is None:
            return [f"{path}: unresolvable ref {field_schema.get('ref')!r}"]
        if target.get("type") == "object":
            return _validate_object(target, value, defs, f"{path}.")
        return _validate_field(target, value, defs, path)

    if ftype == "object":
        return _validate_object(field_schema, value, defs, f"{path}.")

    if ftype == "string":
        return _validate_string(field_schema, value, path)

    if ftype == "integer":
        errs = []
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path}: expected integer, got {_typename(value)}"]
        if "minimum" in field_schema and value < field_schema["minimum"]:
            errs.append(f"{path}: {value} below minimum {field_schema['minimum']}")
        if "maximum" in field_schema and value > field_schema["maximum"]:
            errs.append(f"{path}: {value} above maximum {field_schema['maximum']}")
        return errs

    if ftype == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean, got {_typename(value)}"]
        return []

    if ftype == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {_typename(value)}"]
        errs = []
        if "minLength" in field_schema and len(value) < field_schema["minLength"]:
            errs.append(f"{path}: array has {len(value)} items, minLength {field_schema['minLength']}")
        if "maxLength" in field_schema and len(value) > field_schema["maxLength"]:
            errs.append(f"{path}: array has {len(value)} items, maxLength {field_schema['maxLength']}")
        item_schema = field_schema.get("items", {})
        for i, item in enumerate(value):
            errs.extend(_validate_field(item_schema, item, defs, f"{path}[{i}]"))
        return errs

    # Unknown/unsupported type: don't block.
    return []


def _validate_string(field_schema: dict, value, path: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{path}: expected string, got {_typename(value)}"]
    errs = []
    # enum is a closed set; knownValues is advisory (open) and intentionally
    # not enforced.
    if "enum" in field_schema and value not in field_schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {field_schema['enum']}")
    if "maxLength" in field_schema:
        n = len(value.encode("utf-8"))
        if n > field_schema["maxLength"]:
            errs.append(f"{path}: {n} bytes exceeds maxLength {field_schema['maxLength']}")
    if "minLength" in field_schema:
        n = len(value.encode("utf-8"))
        if n < field_schema["minLength"]:
            errs.append(f"{path}: {n} bytes below minLength {field_schema['minLength']}")
    if "maxGraphemes" in field_schema and len(value) > field_schema["maxGraphemes"]:
        errs.append(f"{path}: {len(value)} chars exceeds maxGraphemes {field_schema['maxGraphemes']}")
    fmt = field_schema.get("format")
    if fmt == "datetime" and not _is_datetime(value):
        errs.append(f"{path}: {value!r} is not a valid ISO-8601 datetime")
    if fmt == "at-uri" and not value.startswith("at://"):
        errs.append(f"{path}: {value!r} is not a valid at-uri")
    return errs


def _is_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (ValueError, AttributeError):
        return False


def _typename(value) -> str:
    return type(value).__name__
