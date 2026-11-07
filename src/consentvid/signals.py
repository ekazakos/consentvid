"""Account-level signals that override a consent: bot accounts, and the
contributor's own published AI-usage preferences.

* **Bot accounts may not consent.** An account that labels itself a bot (the
  global self-label ``bot`` on its ``app.bsky.actor.profile/self`` record,
  atproto.com/guides/bot-tutorial) is not a person who recorded a video and
  can affirm being an adult. No profile record = not a bot.

* **community.lexicon.preference.ai** (lexicon-community) lets an account
  publish AI-usage preferences: a record at key ``self`` with a
  ``#globalScope`` sets defaults, and TID-keyed records with a
  ``#collectionScope`` (or ``#entityScope``) override them. A consent that
  grants ``training`` while the same account publicly says training is
  denied is a conflict, and ConsentVid resolves it the conservative way:
  the consent is not honoured (resync: CONFLICTING_PREFERENCE) and
  ``contribute``/``attach-consent`` refuse to write one. Precedence: a
  collectionScope override naming a ConsentVid collection beats the global
  default (the more specific statement wins); among such overrides a deny
  wins. ``#entityScope`` overrides name an AI consumer, which ConsentVid
  cannot know, so they are not applied. Records that don't parse are
  ignored. Only ``training`` is mapped: the preference's
  inference/embedding/syntheticContent have no ConsentVid purpose.

Both are read from the contributor's own PDS. The ``self`` records are
fetched with :func:`consentvid.verify.fetch_verified` (signature + MST proof,
including proof of absence). Overrides (any other record key; TIDs by convention) are found with listRecords (not
provable: a listing can omit records) and each one found is then read with
a proof. Any failure raises: callers fail closed (resync: UNKNOWN).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from consentvid.records import CONSENT_NSIDS, VIDEO_NSIDS, is_record_key, to_dict

AI_PREF_NSID = "community.lexicon.preference.ai"
PROFILE_NSID = "app.bsky.actor.profile"
BOT_LABEL = "bot"
_SELF_LABELS = "com.atproto.label.defs#selfLabels"
_OUR_COLLECTIONS = frozenset(VIDEO_NSIDS + CONSENT_NSIDS)
MAX_OVERRIDES = 50  # overrides read with a proof, per account
SIGNALS_TTL = 600.0  # seconds a lookup is reused within a process


@dataclass(frozen=True)
class AccountSignals:
    bot: bool = False
    training_denied: bool = False
    detail: str = ""  # where the decision came from

    def conflicts(self, consent_view: dict) -> str | None:
        """Why ``consent_view`` (internal vocabulary) must not be honoured
        for this account, or None."""
        if self.bot:
            return "the account labels itself a bot (self-label 'bot'); bots cannot give consent"
        purposes = consent_view.get("purposes") if isinstance(consent_view, dict) else None
        if self.training_denied and isinstance(purposes, list) and "training" in purposes:
            return (
                "the account's community.lexicon.preference.ai denies AI training "
                f"({self.detail}), which contradicts this consent's 'training' purpose"
            )
        return None


NONE = AccountSignals()


def is_bot_profile(profile) -> bool:
    """Does an app.bsky.actor.profile record carry the 'bot' self-label?"""
    if not isinstance(profile, dict):
        return False
    labels = profile.get("labels")
    if not isinstance(labels, dict) or labels.get("$type") != _SELF_LABELS:
        return False
    values = labels.get("values")
    return isinstance(values, list) and any(isinstance(v, dict) and v.get("val") == BOT_LABEL for v in values)


def _scope(record) -> tuple[str, str | None] | None:
    """('global', None) / ('collection', nsid) / ('entity', id) for a
    preference record, None if it doesn't parse."""
    if not isinstance(record, dict) or record.get("$type") != AI_PREF_NSID:
        return None
    scope = record.get("scope")
    t = scope.get("$type") if isinstance(scope, dict) else None
    if t in ("#globalScope", f"{AI_PREF_NSID}#globalScope"):
        return ("global", None)
    if t in ("#collectionScope", f"{AI_PREF_NSID}#collectionScope") and isinstance(scope.get("collection"), str):
        return ("collection", scope["collection"])
    if t in ("#entityScope", f"{AI_PREF_NSID}#entityScope") and isinstance(scope.get("entity"), str):
        return ("entity", scope["entity"])
    return None


def training_allow(record) -> bool | None:
    """The record's preferences.training.allow, or None if undeclared."""
    prefs = record.get("preferences") if isinstance(record, dict) else None
    training = prefs.get("training") if isinstance(prefs, dict) else None
    allow = training.get("allow") if isinstance(training, dict) else None
    return allow if isinstance(allow, bool) else None


def decide_training(global_record, overrides) -> tuple[bool, str]:
    """(denied, detail) from the 'self' record (or None) and the override
    records. See the module docstring for the precedence."""
    relevant = []
    for rec in overrides:
        scope = _scope(rec)
        if scope and scope[0] == "collection" and scope[1] in _OUR_COLLECTIONS:
            allow = training_allow(rec)
            if allow is not None:
                relevant.append((allow, scope[1]))
    if relevant:
        denied = [c for allow, c in relevant if allow is False]
        if denied:
            return True, f"override for {denied[0]}"
        return False, "override allows training"
    scope = _scope(global_record)
    if scope == ("global", None) and training_allow(global_record) is False:
        return True, "account-wide default"
    return False, ""


def _lookup_impl(did: str) -> AccountSignals:
    from consentvid.identity import list_all, public_client
    from consentvid.verify import fetch_verified

    profile = fetch_verified(f"at://{did}/{PROFILE_NSID}/self")
    bot = profile.present and is_bot_profile(profile.value)
    pref_self = fetch_verified(f"at://{did}/{AI_PREF_NSID}/self")
    overrides = []
    listed = list_all(public_client(did), did, AI_PREF_NSID)
    candidates = []
    for r in listed:
        rkey = str(getattr(r, "uri", "")).rsplit("/", 1)[-1]
        if rkey == "self" or not is_record_key(rkey):
            continue
        value = to_dict(getattr(r, "value", None))
        scope = _scope(value)
        if scope and scope[0] == "collection" and scope[1] in _OUR_COLLECTIONS:
            candidates.append(rkey)
    for rkey in candidates[:MAX_OVERRIDES]:
        rec = fetch_verified(f"at://{did}/{AI_PREF_NSID}/{rkey}")
        if rec.present:
            overrides.append(rec.value)
    if len(candidates) > MAX_OVERRIDES:
        # Can't read them all: fail closed.
        return AccountSignals(bot, True, f"more than {MAX_OVERRIDES} ConsentVid overrides")
    denied, detail = decide_training(pref_self.value if pref_self.present else None, overrides)
    return AccountSignals(bot, denied, detail)


_lookup = _lookup_impl  # tests replace this (offline by default)
_cache: dict[str, tuple[float, AccountSignals]] = {}
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def account_signals(did: str, *, fresh: bool = False) -> AccountSignals:
    """The account's signals, looked up at most once per DID per
    SIGNALS_TTL (``fresh`` skips the cache). Raises on any failure."""
    with _guard:
        lock = _locks.setdefault(did, threading.Lock())
    with lock:
        hit = _cache.get(did)
        if not fresh and hit is not None and time.monotonic() - hit[0] <= SIGNALS_TTL:
            return hit[1]
        answer = _lookup(did)
        _cache[did] = (time.monotonic(), answer)
        return answer


def clear_cache() -> None:
    with _guard:
        _cache.clear()
