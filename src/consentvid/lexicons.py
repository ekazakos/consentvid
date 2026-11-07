"""Load, publish and resolve the org.consentvid.* lexicons.

The JSON schemas ship inside the package (``consentvid/schemas``) so they're
available to non-editable installs; ``web/lexicons`` mirrors them for the static
site (a test keeps the two identical).

ATproto resolves a lexicon from its NSID by domain authority::

    org.consentvid.consent  ->  authority consentvid.org
    DNS TXT _lexicon.consentvid.org  ->  "did=<did>"
    <did>'s repo, collection com.atproto.lexicon.schema, rkey = the NSID

Publishing a lexicon = writing that schema record + the DNS TXT record.
"""

from __future__ import annotations

import json
import subprocess
from functools import cache
from importlib import resources

SCHEMA_COLLECTION = "com.atproto.lexicon.schema"


def nsid_authority(nsid: str) -> str:
    """org.consentvid.consent -> consentvid.org"""
    parts = nsid.split(".")
    if len(parts) < 3:
        raise ValueError(f"NSID must have at least 3 segments: {nsid}")
    return ".".join(reversed(parts[:-1]))


def dns_record_name(nsid: str) -> str:
    return f"_lexicon.{nsid_authority(nsid)}"


def _schema_dir():
    return resources.files("consentvid") / "schemas"


def discover_lexicons() -> list[str]:
    return sorted(p.name.removesuffix(".json") for p in _schema_dir().iterdir() if p.name.endswith(".json"))


@cache
def _load_text(nsid: str) -> str:
    path = _schema_dir() / f"{nsid}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no lexicon file for {nsid}")
    return path.read_text(encoding="utf-8")


def load_lexicon(nsid: str) -> dict:
    """A fresh copy of the lexicon document (safe to mutate)."""
    return json.loads(_load_text(nsid))


def build_schema_record(lexicon: dict) -> dict:
    """A com.atproto.lexicon.schema record is the lexicon document plus $type."""
    record = dict(lexicon)
    record["$type"] = SCHEMA_COLLECTION
    return record


def schema_matches(local: dict, published: dict) -> bool:
    """True if a published schema record has the same content as the local file."""
    strip = lambda d: {k: v for k, v in d.items() if k != "$type"}  # noqa: E731
    return strip(local) == strip(published)


class ConflictingLexiconRecords(ValueError):
    """Several _lexicon TXT records name different DIDs."""


def dids_from_txt(texts) -> set[str]:
    """Distinct, syntactically valid DIDs in ``did=`` TXT values."""
    from consentvid.records import is_did

    out = set()
    for text in texts:
        value = text.strip().strip('"')
        if value.startswith("did="):
            did = value[len("did=") :].strip()
            if is_did(did):
                out.add(did)
    return out


def lexicon_txt_values(nsid: str) -> list[str]:
    """The TXT strings at _lexicon.<authority> (each record's character
    strings joined, per RFC 7208 style splitting). Empty only when DNS says
    there is no such record (NXDOMAIN or no TXT answer); a lookup that failed
    (timeout, SERVFAIL, no nameservers) raises LexiconDnsError, since it
    proves nothing about the record."""
    from consentvid.errors import LexiconDnsError

    name = dns_record_name(nsid)
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return _dig_txt(name)
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=10)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except Exception as e:  # noqa: BLE001 — Timeout, LifetimeTimeout, NoNameservers, ...
        raise LexiconDnsError(f"DNS lookup for {name} failed: {type(e).__name__}; retry") from None
    return [b"".join(rdata.strings).decode("utf-8", "replace") for rdata in answers]


def _dig_txt(name: str) -> list[str]:
    """TXT strings via the dig command (when dnspython is missing)."""
    from consentvid.errors import LexiconDnsError

    try:
        proc = subprocess.run(["dig", "+short", "TXT", name], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise LexiconDnsError(f"DNS lookup for {name} failed: neither dnspython nor dig is available") from None
    except subprocess.SubprocessError as e:
        raise LexiconDnsError(f"DNS lookup for {name} failed: {type(e).__name__}; retry") from None
    if proc.returncode != 0:
        raise LexiconDnsError(f"DNS lookup for {name} failed: dig exited {proc.returncode}; retry")
    # dig prints one record per line, its strings quoted and space-separated.
    return ["".join(part for part in line.split('"')[1::2]) for line in proc.stdout.splitlines()]


def resolve_did_via_dns(nsid: str, *, strict: bool = False) -> str | None:
    """The DID named by the _lexicon TXT record for this NSID's authority.

    Per atproto.com/specs/lexicon (and the handle spec's TXT rule), several
    records naming different DIDs make resolution fail: None, or
    ConflictingLexiconRecords with ``strict=True``. A failed lookup raises
    LexiconDnsError (never None: "no record" must not be guessed)."""
    dids = dids_from_txt(lexicon_txt_values(nsid))
    if len(dids) > 1:
        if strict:
            raise ConflictingLexiconRecords(
                f"conflicting _lexicon TXT records at {dns_record_name(nsid)}: {', '.join(sorted(dids))}"
            )
        return None
    return next(iter(dids), None)


# ---------------------------------------------------------- evolution guard


# Constraint keywords whose addition, removal or change breaks the lexicon
# evolution rules (atproto.com/specs/lexicon#lexicon-evolution).
_CONSTRAINTS = (
    "type",
    "format",
    "ref",
    "maxLength",
    "minLength",
    "maxGraphemes",
    "minGraphemes",
    "minimum",
    "maximum",
    "maxSize",
    "accept",
    "const",
    "enum",
)


def compat_errors(old: dict, new: dict) -> list[str]:
    """Why replacing the published lexicon ``old`` with ``new`` would break
    existing records or readers. Empty if the change is compatible: only
    descriptions, new optional properties and new knownValues changed."""
    errors: list[str] = []
    old_defs, new_defs = old.get("defs") or {}, new.get("defs") or {}
    if old.get("id") != new.get("id"):
        errors.append(f"id changed from {old.get('id')!r} to {new.get('id')!r}")
    for name, odef in old_defs.items():
        if name not in new_defs:
            errors.append(f"def #{name} removed")
            continue
        _compare(odef, new_defs[name], f"#{name}", errors)
    return errors


def _compare(old, new, path: str, errors: list[str]) -> None:
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            errors.append(f"{path}: changed")
        return
    for key in _CONSTRAINTS:
        if old.get(key) != new.get(key):
            what = "added" if key not in old else "removed" if key not in new else "changed"
            errors.append(f"{path}: {key} {what} ({old.get(key)!r} -> {new.get(key)!r})")
    if old.get("type") == "union":
        # atproto.com/specs/lexicon: only closed unions are frozen; an open
        # union may gain refs (the style guide recommends open unions for
        # exactly that), but never lose one or change openness.
        if bool(old.get("closed")) != bool(new.get("closed")):
            errors.append(f"{path}: union changed from {'closed' if old.get('closed') else 'open'}")
        old_refs, new_refs = set(old.get("refs") or []), set(new.get("refs") or [])
        if old.get("closed"):
            if old_refs != new_refs:
                errors.append(f"{path}: refs of a closed union changed ({sorted(old_refs)} -> {sorted(new_refs)})")
        else:
            for ref in sorted(old_refs - new_refs):
                errors.append(f"{path}: union ref {ref!r} removed")
    if old.get("type") == "record" and old.get("key") != new.get("key"):
        errors.append(f"{path}: record key type changed ({old.get('key')!r} -> {new.get('key')!r})")
    old_known, new_known = old.get("knownValues") or [], new.get("knownValues") or []
    for value in old_known:
        if value not in new_known:
            errors.append(f"{path}: knownValues entry {value!r} removed")
    old_req, new_req = set(old.get("required") or []), set(new.get("required") or [])
    for req in sorted(new_req - old_req):
        errors.append(f"{path}: {req!r} added to required")
    for req in sorted(old_req - new_req):
        errors.append(f"{path}: {req!r} removed from required")
    if set(old.get("nullable") or []) != set(new.get("nullable") or []):
        errors.append(f"{path}: nullable changed")
    old_props, new_props = old.get("properties") or {}, new.get("properties") or {}
    for prop, odef in old_props.items():
        if prop not in new_props:
            errors.append(f"{path}.{prop}: property removed")
        else:
            _compare(odef, new_props[prop], f"{path}.{prop}", errors)
    for sub in ("record", "items", "parameters", "input", "output", "schema", "message"):
        if sub in old or sub in new:
            if sub not in new:
                errors.append(f"{path}.{sub}: removed")
            elif sub not in old:
                errors.append(f"{path}.{sub}: added")
            else:
                _compare(old[sub], new[sub], f"{path}.{sub}", errors)
