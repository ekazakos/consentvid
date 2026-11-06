"""Controlled vocabularies, derived from the lexicons (single source of truth)."""

from __future__ import annotations

from consentvid.lexicons import load_lexicon
from consentvid.records import CONSENT_NSID, VIDEO_NSID

_consent = load_lexicon(CONSENT_NSID)["defs"]["main"]["record"]["properties"]
_video = load_lexicon(VIDEO_NSID)["defs"]

MODALITIES = tuple(_consent["modalities"]["items"]["knownValues"])
PURPOSES = tuple(_consent["purposes"]["items"]["knownValues"])
MODEL_CLASSES = tuple(_consent["modelClasses"]["items"]["knownValues"])
COMPENSATION = tuple(_consent["compensation"]["knownValues"])
RIGHTS_BASIS = tuple(_consent["rightsBasis"]["knownValues"])
CAPTURE_PROVENANCE = tuple(_consent["captureProvenance"]["knownValues"])
STORAGE_KINDS = tuple(_video["storageRef"]["properties"]["kind"]["knownValues"])

# Capture-provenance tiers, weakest to strongest.
PROVENANCE_ORDER = {"unverified": 0, "selfAttested": 1, "attested": 2}
assert set(PROVENANCE_ORDER) == set(CAPTURE_PROVENANCE)
