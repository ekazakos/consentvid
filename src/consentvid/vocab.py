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
PEOPLE_PRESENT = tuple(_consent["peoplePresent"]["knownValues"])
STORAGE_KINDS = tuple(_video["storageRef"]["properties"]["kind"]["knownValues"])

# Capture-provenance tiers, weakest to strongest.
PROVENANCE_ORDER = {"unverified": 0, "selfAttested": 1, "attested": 2}
assert set(PROVENANCE_ORDER) == set(CAPTURE_PROVENANCE)

# Values kept valid for existing records but no longer offered on write
# (see the lexicon descriptions): researchOnly overlaps the academic model
# class; foundationModel describes a model, not who may use the data.
DEPRECATED = frozenset({"researchOnly", "foundationModel"})
OFFERED_PURPOSES = tuple(v for v in PURPOSES if v not in DEPRECATED)
OFFERED_MODEL_CLASSES = tuple(v for v in MODEL_CLASSES if v not in DEPRECATED)

# Modalities that capture a person's identity; a consent declaring
# peoplePresent=noIdentifiablePeople can't grant them.
IDENTITY_MODALITIES = frozenset({"face", "voice"})
