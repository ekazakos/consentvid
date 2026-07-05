"""Publish and resolve org.consentvid.* lexicons on the ATproto network.

Until a lexicon is *resolvable*, records must be written with validate:false —
the PDS can't find the schema to check against, and other tools can't discover
what the record type means. Making it resolvable turns org.consentvid.* from a
placeholder squatted on bsky.social into a first-class, schema-validated
namespace.

ATproto resolves a lexicon from its NSID by domain authority:

    org.consentvid.consent   --reverse-->   consentvid.org

A validator looks up a DNS TXT record at ``_lexicon.<authority>`` holding
``did=<did>``, then fetches a ``com.atproto.lexicon.schema`` record (rkey = the
full NSID) from that DID's repo. The record *is* the lexicon document.

So publishing has two steps:
  1. Write each lexicon as a com.atproto.lexicon.schema record (this module /
     `consentvid publish-lexicons`).
  2. Add the DNS TXT record at the registrar (manual — printed for you).

NOTE: DNS-based lexicon resolution is a young part of the AT Protocol; verify
the exact record shape against the current spec before relying on it in
production. This implementation follows the com.atproto.lexicon.schema /
_lexicon TXT design.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

SCHEMA_COLLECTION = "com.atproto.lexicon.schema"
# Repo copy is authoritative; web/lexicons mirrors it for the static site.
LEXICON_DIR = Path(__file__).resolve().parent.parent.parent / "lexicons"


def nsid_authority(nsid: str) -> str:
    """org.consentvid.consent -> consentvid.org (the domain that must host the
    DNS TXT record and the schema DID)."""
    parts = nsid.split(".")
    if len(parts) < 3:
        raise ValueError(f"NSID must have at least 3 segments: {nsid}")
    # Drop the record-name segment; reverse the remaining domain authority.
    domain_parts = parts[:-1]
    return ".".join(reversed(domain_parts))


def dns_record_name(nsid: str) -> str:
    return f"_lexicon.{nsid_authority(nsid)}"


def load_lexicon(nsid: str) -> dict:
    path = LEXICON_DIR / f"{nsid}.json"
    if not path.exists():
        raise FileNotFoundError(f"No lexicon file for {nsid} at {path}")
    return json.loads(path.read_text())


def discover_lexicons() -> list[str]:
    """NSIDs of every lexicon JSON file shipped in the repo."""
    return sorted(p.stem for p in LEXICON_DIR.glob("*.json"))


def build_schema_record(lexicon: dict) -> dict:
    """A com.atproto.lexicon.schema record is the lexicon document itself with a
    $type. The record key is the NSID (the lexicon's `id`)."""
    record = dict(lexicon)
    record["$type"] = SCHEMA_COLLECTION
    return record


def resolve_did_via_dns(nsid: str) -> str | None:
    """Look up the DID that publishes a lexicon via its _lexicon TXT record.

    Uses dnspython if available, else shells out to `dig`/`nslookup`. Returns the
    DID string or None if no record is found.
    """
    name = dns_record_name(nsid)

    def _extract(text: str) -> str | None:
        for token in text.replace('"', " ").split():
            if token.startswith("did="):
                return token[len("did="):]
            if token.startswith("did:"):
                return token
        return None

    try:
        import dns.resolver  # type: ignore

        for rdata in dns.resolver.resolve(name, "TXT"):
            did = _extract(rdata.to_text())
            if did:
                return did
        return None
    except ImportError:
        pass
    except Exception:
        return None

    for cmd in (["dig", "+short", "TXT", name], ["nslookup", "-type=TXT", name]):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            ).stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        did = _extract(out)
        if did:
            return did
    return None
