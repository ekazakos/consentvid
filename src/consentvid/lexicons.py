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


def resolve_did_via_dns(nsid: str) -> str | None:
    """Return the DID in the _lexicon TXT record for this NSID's authority."""
    name = dns_record_name(nsid)

    def _extract(text: str) -> str | None:
        for token in text.replace('"', " ").split():
            if token.startswith("did="):
                return token[len("did=") :]
        return None

    try:
        import dns.resolver  # type: ignore

        try:
            answers = dns.resolver.resolve(name, "TXT", lifetime=10)
        except Exception:  # NXDOMAIN, NoAnswer, timeout
            return None
        for rdata in answers:
            did = _extract(b"".join(rdata.strings).decode())
            if did:
                return did
        return None
    except ImportError:
        pass
    try:
        out = subprocess.run(
            ["dig", "+short", "TXT", name], capture_output=True, text=True, timeout=10
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return _extract(out)
