"""What a consent receipt permits.

Filters use all-of semantics: a researcher asking for ``purpose=training,
modelClass=commercial`` needs clips whose consent grants *both*. (Matching any
one of them would hand out clips for uses their owners never agreed to.)
"""

from __future__ import annotations


def covers(consent: dict, *, purposes=(), model_classes=(), modalities=()) -> bool:
    return (
        set(purposes) <= set(consent.get("purposes") or [])
        and set(model_classes) <= set(consent.get("modelClasses") or [])
        and set(modalities) <= set(consent.get("modalities") or [])
    )


def describe_terms(consent: dict) -> str:
    exp = consent.get("expiresAt")
    return (
        f"{', '.join(consent.get('purposes', []))} · {', '.join(consent.get('modelClasses', []))}"
        f" · {', '.join(consent.get('modalities', []))}" + (f" · expires {exp}" if exp else "")
    )
