"""What a consent receipt permits.

Filters use all-of semantics: a researcher asking for ``purpose=training,
modelClass=commercial`` needs clips whose consent grants *both*. (Matching any
one of them would hand out clips for uses their owners never agreed to.)
"""

from __future__ import annotations

from consentvid.records import normalize_dt


def covers(consent: dict, *, purposes=(), model_classes=(), modalities=()) -> bool:
    return (
        set(purposes) <= set(consent.get("purposes") or [])
        and set(model_classes) <= set(consent.get("modelClasses") or [])
        and set(modalities) <= set(consent.get("modalities") or [])
    )


def latest_per_video(consents: list[tuple[str, dict]]) -> tuple[dict[str, tuple[str, dict]], list[str]]:
    """One authoritative consent per video: the most recently created one.

    Returns ({videoUri: (consentUri, consent)}, [superseded consent URIs]).
    """
    best: dict[str, tuple[str, dict]] = {}
    superseded: list[str] = []

    def key(c: dict):
        return normalize_dt(c.get("createdAt")) or ""

    for uri, c in consents:
        v = c.get("videoUri")
        if v not in best:
            best[v] = (uri, c)
        elif key(c) > key(best[v][1]):
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
