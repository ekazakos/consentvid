"""What a consent receipt permits.

Filters use all-of semantics: a researcher asking for ``purpose=training,
modelClass=commercial`` needs clips whose consent grants *both*. (Matching any
one of them would hand out clips for uses their owners never agreed to.)
"""

from __future__ import annotations

from consentvid.records import normalize_dt, parse_at_uri


def consent_order_key(uri: str, created_at) -> tuple[str, str]:
    """Total order on consents for one video: (createdAt, rkey)."""
    try:
        rkey = parse_at_uri(uri).rkey
    except ValueError:
        rkey = ""
    return (normalize_dt(created_at) or "", rkey)


def covers(consent: dict, *, purposes=(), model_classes=(), modalities=()) -> bool:
    return (
        set(purposes) <= set(consent.get("purposes") or [])
        and set(model_classes) <= set(consent.get("modelClasses") or [])
        and set(modalities) <= set(consent.get("modalities") or [])
    )


def latest_per_video(consents: list[tuple[str, dict]]) -> tuple[dict[str, tuple[str, dict]], list[str]]:
    """One authoritative consent per video: the most recently created one,
    ties broken by record key. resync uses the same total order, so export and
    re-sync always agree on which consent governs.

    Pass ALL of a repo's consents, invalid ones included: the newest record
    governs even when it is invalid (then nothing is granted, and the caller
    must drop the video rather than fall back to an older consent). A
    createdAt that doesn't parse can't be ordered and counts as newest.

    Returns ({videoUri: (consentUri, consent)}, [superseded consent URIs]).
    """
    best: dict[str, tuple[str, dict]] = {}
    superseded: list[str] = []

    def key(uri: str, c: dict):
        return (normalize_dt(c.get("createdAt")) is None, *consent_order_key(uri, c.get("createdAt")))

    for uri, c in consents:
        v = c.get("videoUri")
        if v not in best:
            best[v] = (uri, c)
        elif key(uri, c) > key(*best[v]):
            superseded.append(best[v][0])
            best[v] = (uri, c)
        else:
            superseded.append(uri)
    return best, superseded


def describe_terms(consent: dict) -> str:
    exp = consent.get("expiresAt")
    return (
        f"{', '.join(consent.get('purposes', []))} · {', '.join(consent.get('modelClasses', []))}"
        f" · {', '.join(consent.get('modalities', []))}" + (f" · expires {exp}" if exp else "")
    )
