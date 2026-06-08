"""Consumer-side re-sync: drop revoked/expired clips from a Croissant manifest.

A ConsentVid dataset manifest embeds, per FileObject, the at:// URI of the
signed consent receipt (``cr:consentReceipt.atUri``). Consent is revoked by
DELETING that record from the contributor's repo. This module re-checks every
referenced consent against the live network and reports which clips a dataset
consumer must drop before a training run — the behaviour the manifest's own
``rai:dataReleaseMaintenancePlan`` promises.

The check is authoritative against the contributor's repo (``getRecord`` →
``RecordNotFound`` means revoked); it does not depend on running an AppView.
Expiry is evaluated from the receipt's signed ``expiresAt`` (immutable once
written), so it needs no network call.
"""
from __future__ import annotations

from datetime import datetime, timezone


VALID = "valid"
REVOKED = "revoked"
EXPIRED = "expired"
UNKNOWN = "unknown"
DROP_STATUSES = (REVOKED, EXPIRED)


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_at_uri(uri: str):
    """Split an at:// URI into (did, collection, rkey). Raises ValueError if malformed."""
    parts = uri.removeprefix("at://").split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"Bad AT-URI: {uri}")
    return parts[0], parts[1], parts[2]


def iter_file_objects(manifest: dict):
    for fo in manifest.get("distribution", []) or []:
        if fo.get("@type") == "cr:FileObject":
            yield fo


def consent_status(client, consent_uri, expires_at=None, now=None) -> str:
    """Classify a consent receipt as valid / revoked / expired / unknown.

    ``unknown`` means a network error or malformed URI — the caller should NOT
    drop the clip on unknown (fail-open on transient errors, not on revocation).
    """
    now = now or datetime.now(timezone.utc)
    exp = _parse_dt(expires_at)
    if exp and exp <= now:
        return EXPIRED
    try:
        did, collection, rkey = parse_at_uri(consent_uri)
    except ValueError:
        return UNKNOWN
    try:
        client.com.atproto.repo.get_record(
            {"repo": did, "collection": collection, "rkey": rkey}
        )
    except Exception as e:  # noqa: BLE001 — the SDK raises many error types
        msg = str(e).lower()
        if "recordnotfound" in msg or "could not locate" in msg or "not find record" in msg:
            return REVOKED
        return UNKNOWN
    return VALID


def resync_manifest(client, manifest: dict, now=None) -> list[dict]:
    """Re-check every FileObject's consent receipt. Returns one result dict per
    clip with keys: id, name, consentUri, videoUri, status."""
    results = []
    for fo in iter_file_objects(manifest):
        receipt = fo.get("cr:consentReceipt") or {}
        consent_uri = receipt.get("atUri")
        status = (
            consent_status(client, consent_uri, receipt.get("expiresAt"), now=now)
            if consent_uri
            else UNKNOWN
        )
        results.append(
            {
                "id": fo.get("@id"),
                "name": fo.get("name"),
                "consentUri": consent_uri,
                "videoUri": fo.get("cr:atUri"),
                "status": status,
            }
        )
    return results


def filter_manifest(manifest: dict, results: list[dict], now_iso: str) -> dict:
    """Return a copy of the manifest with revoked/expired FileObjects removed and
    a re-sync note appended to the data-release maintenance plan."""
    drop = {r["id"] for r in results if r["status"] in DROP_STATUSES}
    kept = [fo for fo in manifest.get("distribution", []) or [] if fo.get("@id") not in drop]
    out = dict(manifest)
    out["distribution"] = kept
    note = (
        f"Re-synced {now_iso}: dropped {len(drop)} clip(s) whose consent was "
        f"revoked or expired; {len(kept)} clip(s) remain valid."
    )
    existing = out.get("rai:dataReleaseMaintenancePlan", "")
    out["rai:dataReleaseMaintenancePlan"] = f"{existing} {note}".strip()
    return out
