"""ConsentVid's public Python API: register videos, attach signed consent
receipts, withdraw them, and check them, from scripts and notebooks.

    from consentvid import Session, Terms

    s = Session.login()                       # cached session or settings
    terms = Terms(purposes=["training"], modalities=["video", "audio"],
                  model_classes=["academic"], people="onlyMe",
                  adult=True, authorship=True)
    clip = s.contribute("a.mp4", terms, title="Making pasta")
    s.revoke(clip.video_uri)

Everything goes through the same code as the ``consentvid`` CLI (the CLI is
a thin layer over this module), so the same rules apply: records are
validated locally before anything is uploaded or written; the account's bot
self-label and AI-usage preference are checked; identifying metadata is
stripped from files; a video and its consent are written in one atomic
commit, the consent under the video's own record key.

Nothing here prompts. Consent terms and attestations have no defaults: every
consent states them explicitly (GDPR Art. 4(11)). Progress messages and
warnings go to the ``consentvid`` logger, or to ``Session(on_message=...)``.

For many videos use :meth:`Session.contribute_many`: sequential writes that
stay inside the PDS's write budget, wait out rate limits, and resume from a
JSONL log after an interruption.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from consentvid import _core, versions
from consentvid.errors import (
    AlreadyRegistered,
    Cancelled,
    ConfigError,
    ConsentVidError,
    RateLimited,
    RepoUnavailable,
    UsageError,
)
from consentvid.records import (
    CONSENT_FOR_VIDEO,
    CONSENT_NSIDS,
    VIDEO_FOR_CONSENT,
    VIDEO_NSID,
    VIDEO_NSIDS,
    format_dt,
    hash_file,
    is_expired,
    parse_at_uri,
)
from consentvid.vocab import (
    CAPTURE_PROVENANCE,
    COMPENSATION,
    IDENTITY_MODALITIES,
    MODALITIES,
    OFFERED_MODEL_CLASSES,
    OFFERED_PURPOSES,
    PEOPLE_PRESENT,
    RIGHTS_BASIS,
    SENSITIVE_SELF_LABELS,
    STORAGE_KINDS,
)

__all__ = [
    "AlreadyRegistered",
    "BulkItem",
    "BulkResult",
    "Cancelled",
    "CheckResult",
    "ConfigError",
    "Consent",
    "ConsentEntry",
    "ConsentVidError",
    "Contribution",
    "RateLimited",
    "RepoUnavailable",
    "Revocation",
    "RevokePlan",
    "Session",
    "Terms",
    "UsageError",
    "Video",
    "VideoEntry",
    "check",
]

RevokePlan = _core.RevokePlan


# ---------------------------------------------------------------- terms ------


def _values(name: str, given, allowed: Sequence[str]) -> tuple[str, ...]:
    """Normalise a multi-valued term: a list of values, either spelling
    (V2 kebab-case 'open-weights' or 'openWeights'), no duplicates."""
    if isinstance(given, str):
        given = [given]
    if not given:
        raise UsageError(f"{name}: state at least one of {', '.join(allowed)}; nothing is granted by default")
    out = []
    for v in given:
        camel = versions.kebab_to_camel(v) if isinstance(v, str) else v
        if camel not in allowed:
            raise UsageError(f"{name}: {v!r} is not one of {', '.join(allowed)}")
        out.append(camel)
    return tuple(dict.fromkeys(out))


def _choice(name: str, given, allowed: Sequence[str]) -> str:
    camel = versions.kebab_to_camel(given) if isinstance(given, str) else given
    if camel not in allowed:
        raise UsageError(f"{name}: {given!r} is not one of {', '.join(allowed)}")
    return camel


def _expiry(value) -> str | None:
    """An expiry as the atproto datetime string the record will carry."""
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        value = datetime.now(UTC) + value
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise UsageError("expires: a datetime needs a timezone (e.g. datetime(..., tzinfo=UTC))")
        if value <= datetime.now(UTC):
            raise UsageError("expires: expiry must be in the future")
        return format_dt(value)
    if isinstance(value, str):
        return _core._parse_expiry(value)
    raise UsageError(f"expires: expected a datetime, date, timedelta or string, got {type(value).__name__}")


@dataclass(frozen=True, kw_only=True)
class Terms:
    """The terms of one consent. Every field without a default is required:
    nothing is granted by default.

    * ``purposes``: what the video may be used for (e.g. "training",
      "evaluation").
    * ``modalities``: what may be used ("video", "audio", "face", "voice",
      "text", ...).
    * ``model_classes``: who may use it ("academic", "openWeights", ...).
    * ``people``: who identifiably appears: "onlyMe", "allConsented"
      (everyone shown agreed to this use) or "noIdentifiablePeople".
    * ``adult``: you affirm, signed by your key, that you are an adult able
      to consent. Must be True.
    * ``authorship``: you affirm, signed by your key, that you recorded the
      video (or otherwise hold the rights) and may consent for it. Must be
      True. Consenting for someone else's video is unlawful.
    * ``expires``: optional: a timezone-aware datetime, a date, a timedelta,
      or a string ("2027-01-01", "90d", "12w", "2y"). Relative values are
      fixed when the Terms object is created.
    * ``compensation``, ``rights_basis``, ``capture_provenance``: as the
      CLI's --compensation / --rights-basis / --capture-provenance.

    Values may be given in either spelling ("open-weights" or
    "openWeights"); they are stored in the internal (camelCase) one.
    """

    purposes: Sequence[str]
    modalities: Sequence[str]
    model_classes: Sequence[str]
    people: str
    adult: bool
    authorship: bool
    expires: Any = None
    compensation: str = "none"
    rights_basis: str = "ownRecording"
    capture_provenance: str = "selfAttested"
    expires_at: str | None = field(init=False, default=None)

    def __post_init__(self):
        if self.authorship is not True:
            raise UsageError(
                "authorship=True is required: you affirm (signed by your key) that you recorded this video "
                "yourself or otherwise hold the right to consent. Consenting on behalf of someone else's "
                "video (e.g. a YouTube re-upload) is unlawful."
            )
        if self.adult is not True:
            raise UsageError(
                "adult=True is required: you affirm (signed by your key) that you are 18 or older (or the "
                "age of majority where you live) and able to consent."
            )
        set_ = object.__setattr__
        set_(self, "purposes", _values("purposes", self.purposes, OFFERED_PURPOSES))
        set_(self, "modalities", _values("modalities", self.modalities, MODALITIES))
        set_(self, "model_classes", _values("model_classes", self.model_classes, OFFERED_MODEL_CLASSES))
        set_(self, "people", _choice("people", self.people, PEOPLE_PRESENT))
        set_(self, "compensation", _choice("compensation", self.compensation, COMPENSATION))
        set_(self, "rights_basis", _choice("rights_basis", self.rights_basis, RIGHTS_BASIS))
        set_(self, "capture_provenance", _choice("capture_provenance", self.capture_provenance, CAPTURE_PROVENANCE))
        if self.capture_provenance == "attested":
            raise UsageError(
                "capture_provenance: 'attested' is reserved for capture-time cryptographic provenance "
                "(C2PA/CAWG bound to your DID), which ConsentVid cannot verify yet; use selfAttested."
            )
        if self.people == "noIdentifiablePeople" and set(IDENTITY_MODALITIES) & set(self.modalities):
            raise UsageError("modalities face/voice contradict people='noIdentifiablePeople'")
        set_(self, "expires_at", _expiry(self.expires))

    def _kwargs(self) -> dict:
        """The keyword arguments of the core consent builders."""
        return {
            "modalities": self.modalities,
            "purposes": self.purposes,
            "model_classes": self.model_classes,
            "expires_at": self.expires_at,
            "compensation": self.compensation,
            "people": self.people,
            "rights_basis": self.rights_basis,
            "capture_provenance": self.capture_provenance,
            "attest": True,
            "adult": True,
        }

    def _consent_for(self) -> Callable[[str, str], dict]:
        kw = self._kwargs()

        def build(video_uri: str, video_cid: str) -> dict:
            return _core._consent_record(video_uri, video_cid, **kw)

        return build


# -------------------------------------------------------------- results ------


@dataclass(frozen=True)
class Video:
    """A registered video record."""

    uri: str
    cid: str
    content_hash: str
    record: dict  # internal (version-independent) view of the record


@dataclass(frozen=True)
class Consent:
    """A consent receipt as written."""

    uri: str
    cid: str
    record: dict
    replaced: bool = False


@dataclass(frozen=True)
class Contribution:
    """A video and its consent, written together in one commit."""

    video_uri: str
    video_cid: str
    consent_uri: str
    consent_cid: str
    content_hash: str
    video: dict
    consent: dict

    @property
    def storage(self) -> dict:
        return self.video.get("storageRef") or {}


@dataclass(frozen=True)
class Revocation:
    """What a revoke deleted."""

    withdrawn: tuple[str, ...]  # consent URIs
    video_deleted: str | None  # the video URI, when a video was revoked
    bytes_deleted: bool = False
    bytes_note: str | None = None


@dataclass(frozen=True)
class ConsentEntry:
    uri: str
    cid: str
    record: dict  # internal view
    expired: bool
    keyed: bool  # under its video's own record key (the sidecar); readers ignore it otherwise


@dataclass(frozen=True)
class VideoEntry:
    """One of your videos, with its consent (the sidecar), from :meth:`Session.list`.

    ``status``: "consented", "expired", "no-consent", or "ignored-consent"
    (a consent exists but not under the video's record key, so readers
    ignore it)."""

    uri: str
    cid: str
    version: int
    title: str | None
    content_hash: str | None
    storage: dict
    record: dict
    consent: ConsentEntry | None
    status: str
    raw: dict = field(repr=False)


@dataclass(frozen=True)
class CheckResult:
    """The verdict ``consentvid resync`` would give the clip (see
    consentvid.resync: "valid", "revoked", "expired", "modified", "invalid",
    "unavailable", "unknown", "bot-account", "conflicting-preference",
    "labeled", "stale", ...)."""

    status: str
    reason: str
    verified: bool
    video_uri: str | None
    consent_uri: str | None

    @property
    def ok(self) -> bool:
        return self.status == "valid"


@dataclass
class BulkItem:
    """One video for :meth:`Session.contribute_many`. ``path`` is a local
    file, or use ``post`` (an at:// URI or bsky.app link of one of your
    Bluesky posts) instead. ``terms`` overrides the run's terms for this
    item; the other fields are the per-video options of
    :meth:`Session.contribute`."""

    path: str | Path | None = None
    post: str | None = None
    title: str | None = None
    description: str = ""
    language: str = ""
    tags: Sequence[str] = ()
    self_labels: Sequence[str] = ()
    terms: Terms | None = None
    storage_value: str | None = None

    @property
    def key(self) -> str:
        if self.post:
            return f"post:{self.post}"
        return str(Path(self.path).expanduser().resolve())


@dataclass(frozen=True)
class BulkResult:
    """The outcome for one item of a bulk run. ``status``: "contributed",
    "skipped" (done in an earlier run, or already registered) or "failed"."""

    key: str
    status: str
    contribution: Contribution | None = None
    video_uri: str | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("contributed", "skipped")


# ------------------------------------------------------------ write budget ---


class _WriteBudget:
    """Client-side pacing under a PDS's write budget. bsky.social allows
    5000 points per hour and 35000 per day per account; a create costs 3
    points, an update 2, a delete 1. Waiting here keeps a long bulk run
    from hitting the server's 429 at all (it is still handled)."""

    def __init__(self, per_hour: int | None, per_day: int | None, sleep=time.sleep, clock=time.monotonic):
        self.windows = [(w, n) for w, n in ((3600.0, per_hour), (86400.0, per_day)) if n]
        self.spent: deque[tuple[float, int]] = deque()
        self.sleep, self.clock = sleep, clock

    def take(self, points: int, on_wait: Callable[[float], None] | None = None) -> None:
        if not self.windows:
            return
        while True:
            now = self.clock()
            horizon = max(w for w, _ in self.windows)
            while self.spent and self.spent[0][0] <= now - horizon:
                self.spent.popleft()
            wait = 0.0
            for window, limit in self.windows:
                if points > limit:
                    raise UsageError(f"one write ({points} points) exceeds the {limit}-point budget")
                inside = [(t, p) for t, p in self.spent if t > now - window]
                used = sum(p for _, p in inside)
                if used + points > limit:
                    # Wait until enough of the oldest spending leaves the window.
                    excess = used + points - limit
                    for t, p in inside:
                        excess -= p
                        if excess <= 0:
                            wait = max(wait, t + window - now)
                            break
            if wait <= 0:
                self.spent.append((now, points))
                return
            if on_wait:
                on_wait(wait)
            self.sleep(wait)


_CREATE_POINTS = 3
_CONTRIBUTE_POINTS = 2 * _CREATE_POINTS  # the video and its consent


# ---------------------------------------------------------------- session ----


class Session:
    """An authenticated contributor session. Writes go to your own repo,
    one at a time (a lock serialises writes from several threads)."""

    def __init__(self, client, *, on_message: Callable[[str, str], None] | None = None):
        """Wrap an authenticated ``atproto.Client`` (``client.me.did`` set).
        Usually you want :meth:`login`. ``on_message(level, text)`` receives
        progress and warnings (level "info", "success" or "warning");
        without it they go to the ``consentvid`` logger."""
        self._client = client
        self._on_message = on_message
        self._lock = threading.RLock()

    @classmethod
    def login(
        cls,
        handle: str | None = None,
        password: str | None = None,
        *,
        on_message: Callable[[str, str], None] | None = None,
    ) -> Session:
        """Sign in. With ``handle`` and ``password`` (an app password, never
        your main password) a new session is created on your own PDS and
        cached in ~/.consentvid (replacing any cached one, like
        ``consentvid login``). Without them: the cached session, else
        CONSENTVID_HANDLE / CONSENTVID_APP_PASSWORD from the environment or
        ~/.consentvid/config.env."""
        if (handle is None) != (password is None):
            raise UsageError("give both handle and password, or neither")
        session = cls(None, on_message=on_message)
        with session._reporting():
            _core._load_env()
            if handle is not None:
                _core._end_cached_session()
                _core.SESSION_PATH.unlink(missing_ok=True)
                session._client = _core._session_client(handle, password)
            else:
                session._client = _core.get_client()
        return session

    # -- plumbing --

    def _reporting(self):
        session = self

        class _Ctx:
            def __enter__(self):
                self.token = _core.set_reporter(session._on_message) if session._on_message else None

            def __exit__(self, *exc):
                if self.token is not None:
                    _core.reset_reporter(self.token)
                return False

        return _Ctx()

    def _write(self):
        lock, reporting = self._lock, self._reporting()

        class _Ctx:
            def __enter__(self):
                lock.acquire()
                reporting.__enter__()

            def __exit__(self, *exc):
                try:
                    reporting.__exit__(*exc)
                finally:
                    lock.release()
                return False

        return _Ctx()

    @property
    def client(self):
        """The underlying ``atproto.Client``."""
        return self._client

    @property
    def did(self) -> str:
        return self._client.me.did

    @property
    def handle(self) -> str:
        return self._client.me.handle

    def __repr__(self) -> str:
        return f"Session({self.handle!r}, {self.did!r})"

    # -- writes --

    def register_video(
        self,
        path: str | Path,
        *,
        title: str,
        description: str = "",
        language: str = "",
        tags: Sequence[str] = (),
        self_labels: Sequence[str] = (),
        storage: str | None = None,
        storage_value: str | None = None,
        keep_metadata: bool = False,
        allow_duplicate: bool = False,
        validate: bool | None = None,
    ) -> Video:
        """Register a video WITHOUT consent (attach one later with
        :meth:`attach_consent`). Usually :meth:`contribute` is what you want."""
        with self._write():
            uri, cid, record = _core._register(
                self._client,
                self._file(path),
                validate=validate,
                **self._video_opts(title, description, language, tags, self_labels, storage, storage_value,
                                   keep_metadata, allow_duplicate),
            )
        return Video(str(uri), str(cid), record["contentHash"], record)

    def contribute(
        self,
        path: str | Path,
        terms: Terms,
        *,
        title: str,
        description: str = "",
        language: str = "",
        tags: Sequence[str] = (),
        self_labels: Sequence[str] = (),
        storage: str | None = None,
        storage_value: str | None = None,
        keep_metadata: bool = False,
        allow_duplicate: bool = False,
        validate: bool | None = None,
    ) -> Contribution:
        """Register a video file AND its consent in one atomic commit.

        The file is stripped of identifying metadata (needs ffmpeg) unless
        ``keep_metadata``, hashed, and uploaded to ``storage`` ("r2",
        "ipfs", "peertube"; default: $CONSENTVID_STORAGE, else r2 if
        configured, else "local"). Or pass ``storage="url"`` and
        ``storage_value`` (an https URL where you host the already-clean
        bytes). Raises AlreadyRegistered if you registered the same bytes
        before (unless ``allow_duplicate``)."""
        self._check_terms(terms)
        with self._write():
            uri, cid, record, consent = _core._register_impl(
                self._client,
                self._file(path),
                validate=validate,
                consent_for=terms._consent_for(),
                **self._video_opts(title, description, language, tags, self_labels, storage, storage_value,
                                   keep_metadata, allow_duplicate),
            )
        return self._contribution(uri, cid, record, consent)

    def contribute_from_post(
        self,
        post: str,
        terms: Terms,
        *,
        title: str | None = None,
        description: str = "",
        language: str = "",
        tags: Sequence[str] = (),
        self_labels: Sequence[str] = (),
        keep_metadata: bool = False,
        allow_duplicate: bool = False,
        validate: bool | None = None,
    ) -> Contribution:
        """Contribute the video of one of YOUR Bluesky posts (an at:// URI or
        a https://bsky.app/profile/<you>/post/<id> link). The bytes stay on
        your PDS, tied to the post: deleting the post removes the video from
        every dataset. ``title`` defaults to the post's first line."""
        self._check_terms(terms)
        self._check_labels(self_labels)
        with self._write():
            uri, cid, record, consent = _core._register_post(
                self._client,
                post,
                title=title,
                description=description,
                language=language,
                tags=tuple(tags),
                self_labels=tuple(self_labels),
                keep_metadata=keep_metadata,
                allow_duplicate=allow_duplicate,
                validate=validate,
                consent_for=terms._consent_for(),
            )
        return self._contribution(uri, cid, record, consent)

    def attach_consent(
        self, video_uri: str, terms: Terms, *, replace: bool = False, validate: bool | None = None
    ) -> Consent:
        """Attach a consent to one of your registered videos (one per video:
        it is written under the video's record key). ``replace=True``
        changes the terms of an existing consent; downstream datasets then
        see it as modified."""
        self._check_terms(terms)
        with self._write():
            uri, cid, record, replaced = _core._attach(
                self._client, video_uri, replace=replace, validate=validate, **terms._kwargs()
            )
        return Consent(str(uri), str(cid), record, bool(replaced))

    def revoke(
        self,
        uri: str,
        *,
        delete_bytes: bool = False,
        confirm: Callable[[RevokePlan], bool] | None = None,
    ) -> Revocation:
        """Withdraw consent. A consent URI deletes that consent; a video URI
        deletes the video and all its consents in one atomic commit
        (``delete_bytes=True`` also removes the stored bytes, unless another
        of your videos uses them). ``confirm(plan)`` is called before
        anything is deleted; returning False raises Cancelled."""
        with self._write():
            out = _core._revoke(self._client, uri, delete_bytes=delete_bytes, confirm=confirm)
        return Revocation(tuple(out.withdrawn), out.video_deleted, out.bytes_deleted, out.bytes_note)

    # -- reads --

    def list(self) -> list[VideoEntry]:
        """Your videos (both record versions) with their consent status."""
        with self._reporting():
            did = self.did
            videos = _core._list_records(self._client, did, VIDEO_NSIDS)
            consents = _core._list_records(self._client, did, CONSENT_NSIDS)
        by_video: dict[str, list] = {}
        for c in consents:
            by_video.setdefault(c.view.get("videoUri"), []).append(c)
        out = []
        for r in videos:
            v = r.view
            entries = [
                ConsentEntry(
                    str(c.uri),
                    str(c.cid),
                    c.view,
                    is_expired(c.view),
                    c.rkey == r.rkey and c.nsid == CONSENT_FOR_VIDEO.get(r.nsid),
                )
                for c in by_video.get(r.uri, [])
            ]
            keyed = [e for e in entries if e.keyed]
            consent = keyed[0] if keyed else (entries[0] if entries else None)
            if consent is None:
                status = "no-consent"
            elif not consent.keyed:
                status = "ignored-consent"
            elif consent.expired:
                status = "expired"
            else:
                status = "consented"
            out.append(
                VideoEntry(
                    uri=str(r.uri),
                    cid=str(r.cid),
                    version=1 if r.nsid == VIDEO_NSID else 2,
                    title=v.get("title"),
                    content_hash=v.get("contentHash"),
                    storage=v.get("storageRef") or {},
                    record=v,
                    consent=consent,
                    status=status,
                    raw=r.raw,
                )
            )
        return out

    def check(self, uri: str, *, include_labeled: bool = False, relay_status: bool = True) -> CheckResult:
        """Check one clip the way ``consentvid resync`` checks a dataset entry
        (proof-verified reads of the consent and the video, its bytes'
        binding, expiry, the account's state and signals). ``uri``: a video
        or consent URI, anyone's (no login needed for the reads)."""
        with self._reporting():
            return check(uri, include_labeled=include_labeled, relay_status=relay_status)

    # -- bulk --

    def contribute_many(
        self,
        items: Iterable[str | Path | BulkItem | Mapping],
        *,
        terms: Terms | None = None,
        terms_for: Callable[[BulkItem], Terms] | None = None,
        title_for: Callable[[BulkItem], str] | None = None,
        resume: str | Path | None = None,
        on_error: str = "continue",
        storage: str | None = None,
        keep_metadata: bool = False,
        validate: bool | None = None,
        points_per_hour: int | None = 5000,
        points_per_day: int | None = 35000,
        max_rate_limit_wait: float = 3600.0,
        retries: int = 3,
        progress: Callable[[BulkResult], None] | None = None,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> list[BulkResult]:
        """Contribute many videos, one at a time.

        ``items``: paths, :class:`BulkItem` objects, or mappings with
        BulkItem's fields. Each item's terms come from ``item.terms``, else
        ``terms_for(item)``, else ``terms``; its title from ``item.title``,
        else ``title_for(item)`` (file names are not used as titles: camera
        names leak capture time and device).

        ``resume``: a JSONL log. Every outcome is appended to it, and items
        it records as done (same path AND same file contents) are skipped,
        so re-running the same call after an interruption carries on where
        it stopped. Bytes already registered are reported as skipped.

        Writes are paced under the PDS's write budget (``points_per_hour`` /
        ``points_per_day``; bsky.social: 5000 / 35000, a contribution costs
        6), a 429 is waited out when its reset is at most
        ``max_rate_limit_wait`` seconds away, and transient network errors
        are retried (``retries``). ``on_error``: "continue" with the next
        item or "stop" at the first failure. A rate limit too long to wait
        for always stops the run. Returns one BulkResult per item handled.
        """
        if on_error not in ("continue", "stop"):
            raise UsageError("on_error must be 'continue' or 'stop'")
        if storage is not None and storage not in STORAGE_KINDS:
            raise UsageError(f"storage: {storage!r} is not one of {', '.join(STORAGE_KINDS)}")
        if terms is not None:
            self._check_terms(terms)
        budget = _WriteBudget(points_per_hour, points_per_day, sleep=_sleep)
        log_path = Path(resume).expanduser() if resume else None
        done = _read_resume_log(log_path) if log_path else {}
        results: list[BulkResult] = []

        def record(result: BulkResult, fingerprint: str | None) -> None:
            results.append(result)
            if log_path:
                _append_resume_log(log_path, result, fingerprint)
            if progress:
                progress(result)

        for raw_item in items:
            item = _bulk_item(raw_item)
            key = item.key
            try:
                fingerprint = _fingerprint(item)
            except OSError as e:
                record(BulkResult(key, "failed", error=f"can't read the file: {e}", error_type=type(e).__name__), None)
                if on_error == "stop":
                    break
                continue
            prior = done.get(key)
            if prior and prior.get("fingerprint") == fingerprint:
                record(BulkResult(key, "skipped", video_uri=prior.get("video_uri"), error="done in an earlier run"),
                       fingerprint)
                continue
            try:
                item_terms = item.terms or (terms_for(item) if terms_for else terms)
                if item_terms is None:
                    raise UsageError("no terms for this item: pass terms=, terms_for= or BulkItem(terms=...)")
                self._check_terms(item_terms)
                title = item.title or (title_for(item) if title_for else None)
                if not item.post and not title:
                    raise UsageError("no title for this item: pass BulkItem(title=...) or title_for=")
                contribution = self._contribute_with_retries(
                    item, item_terms, title, storage, keep_metadata, validate, budget,
                    max_rate_limit_wait, retries, _sleep,
                )
            except AlreadyRegistered as e:
                record(BulkResult(key, "skipped", video_uri=e.uri, error=str(e), error_type="AlreadyRegistered"),
                       fingerprint)
                continue
            except RateLimited as e:
                record(BulkResult(key, "failed", error=str(e), error_type="RateLimited"), fingerprint)
                break
            except (ConsentVidError, ValueError) as e:
                record(BulkResult(key, "failed", error=str(e), error_type=type(e).__name__), fingerprint)
                if on_error == "stop":
                    break
                continue
            except Exception as e:  # noqa: BLE001 — an XRPC or network failure for this item
                record(BulkResult(key, "failed", error=f"{type(e).__name__}: {e}", error_type=type(e).__name__),
                       fingerprint)
                if on_error == "stop":
                    break
                continue
            record(BulkResult(key, "contributed", contribution, contribution.video_uri), fingerprint)
        return results

    def _contribute_with_retries(self, item, terms, title, storage, keep_metadata, validate, budget,
                                 max_rate_limit_wait, retries, sleep) -> Contribution:
        from consentvid.identity import _is_transient_error

        attempt = 0
        while True:
            budget.take(
                _CONTRIBUTE_POINTS,
                on_wait=lambda s: self._say("info", f"pacing writes under the PDS budget: waiting {s:.0f} s"),
            )
            try:
                if item.post:
                    return self.contribute_from_post(
                        item.post, terms, title=title, description=item.description, language=item.language,
                        tags=item.tags, self_labels=item.self_labels, keep_metadata=keep_metadata,
                        validate=validate,
                    )
                return self.contribute(
                    item.path, terms, title=title, description=item.description, language=item.language,
                    tags=item.tags, self_labels=item.self_labels, storage=storage,
                    storage_value=item.storage_value, keep_metadata=keep_metadata, validate=validate,
                )
            except RateLimited as e:
                wait = (e.reset - time.time()) if e.reset else None
                if wait is None or wait > max_rate_limit_wait or attempt >= retries:
                    raise
                self._say("warning", f"rate limited by the PDS; waiting {max(wait, 1):.0f} s")
                sleep(max(wait, 1.0))
            except ConsentVidError:
                raise
            except Exception as e:
                # A lost response is resolved inside the core (it looks the
                # write up by its record key); what reaches here never
                # committed or is a transient read failure. A retry is safe:
                # identical bytes are refused as AlreadyRegistered.
                if not _is_transient_error(e) or attempt >= retries:
                    raise
                self._say("warning", f"transient error ({type(e).__name__}); retrying")
                sleep(min(60.0, 2.0 ** attempt))
            attempt += 1

    # -- helpers --

    def _say(self, level: str, message: str) -> None:
        with self._reporting():
            _core._emit(level, message)

    @staticmethod
    def _file(path) -> Path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise UsageError(f"{p}: not a file", param_hint="VIDEO_PATH")
        return p

    @staticmethod
    def _check_terms(terms) -> None:
        if not isinstance(terms, Terms):
            raise UsageError("terms must be a consentvid.Terms object")

    @staticmethod
    def _check_labels(labels) -> None:
        bad = [x for x in labels if x not in SENSITIVE_SELF_LABELS]
        if bad:
            raise UsageError(f"self_labels: {', '.join(map(str, bad))} not in {', '.join(SENSITIVE_SELF_LABELS)}")

    def _video_opts(self, title, description, language, tags, self_labels, storage, storage_value,
                    keep_metadata, allow_duplicate) -> dict:
        if not title:
            raise UsageError("title is required", param_hint="--title")
        if storage is not None and storage not in STORAGE_KINDS:
            raise UsageError(f"storage: {storage!r} is not one of {', '.join(STORAGE_KINDS)}")
        self._check_labels(self_labels)
        if language:
            primary, sep, rest = language.partition("-")
            language = primary.lower() + sep + rest
        return {
            "title": title,
            "description": description,
            "language": language,
            "tags": tuple(tags),
            "self_labels": tuple(self_labels),
            "storage_kind": storage,
            "storage_value": storage_value,
            "keep_metadata": keep_metadata,
            "allow_duplicate": allow_duplicate,
        }

    @staticmethod
    def _contribution(uri, cid, record, consent) -> Contribution:
        c_uri, c_cid, c_record = consent
        return Contribution(str(uri), str(cid), str(c_uri), str(c_cid), record["contentHash"], record, c_record)


# ------------------------------------------------------------ check ---------


def check(uri: str, *, include_labeled: bool = False, relay_status: bool = True) -> CheckResult:
    """Module-level :meth:`Session.check`: needs no login."""
    from consentvid import resync
    from consentvid.croissant import Clip, build_manifest

    fetch_verified = resync.fetch_verified  # one reader for every read here

    try:
        at = parse_at_uri(uri)
    except ValueError as e:
        raise UsageError(str(e), param_hint="URI") from None
    if at.collection in VIDEO_NSIDS:
        video_at = at
        consent_at = parse_at_uri(f"at://{at.did}/{CONSENT_FOR_VIDEO[at.collection]}/{at.rkey}")
    elif at.collection in CONSENT_NSIDS:
        consent_at = at
        video_at = None
    else:
        raise UsageError(f"not a ConsentVid record: {at.collection}", param_hint="URI")

    def result(status, reason, verified=False):
        return CheckResult(status, reason, verified, str(video_at) if video_at else None, str(consent_at))

    try:
        consent = fetch_verified(consent_at)
    except Exception as e:  # noqa: BLE001 — couldn't check
        return result(resync.UNKNOWN, f"couldn't read the consent: {type(e).__name__}: {e}")
    if not consent.present:
        return result(resync.REVOKED, "no consent record (never given, or withdrawn)", consent.proven)
    try:
        c_view = versions.consent_view(consent_at.collection, consent.value)
    except Exception as e:  # noqa: BLE001
        return result(resync.INVALID, f"malformed consent: {e}", True)
    if not isinstance(c_view, dict) or not isinstance(c_view.get("videoUri"), str):
        return result(resync.INVALID, "malformed consent", True)
    try:
        claimed = parse_at_uri(c_view["videoUri"], VIDEO_FOR_CONSENT[consent_at.collection])
    except ValueError as e:
        return result(resync.INVALID, f"consent names a bad video URI: {e}", True)
    if video_at is not None and str(claimed) != str(video_at):
        return result(resync.INVALID, "the consent under this video's key names another video", True)
    video_at = claimed
    try:
        video = fetch_verified(video_at)
    except Exception as e:  # noqa: BLE001
        return result(resync.UNKNOWN, f"couldn't read the video: {type(e).__name__}: {e}")
    if not video.present:
        return result(resync.REVOKED, "the video record is gone", video.proven)
    try:
        v_view = versions.video_view(video_at.collection, video.value)
    except Exception as e:  # noqa: BLE001
        return result(resync.INVALID, f"malformed video record: {e}", True)
    clip = Clip(str(consent_at), str(consent.cid), c_view, str(video_at), str(video.cid), v_view, at.did)
    manifest, skipped = build_manifest([clip], name="consentvid-check")
    if skipped:
        kind = (v_view.get("storageRef") or {}).get("kind")
        return result(resync.INVALID, f"no public contentUrl: the bytes aren't publicly reachable (storage {kind})")
    item = resync.items_from_manifest(manifest)[0]
    res = resync.check(item, include_labeled=include_labeled, relay_status=relay_status, fetch=fetch_verified)
    return CheckResult(res.status, res.reason, res.verified, res.videoUri or str(video_at), res.consentUri)


# ------------------------------------------------------------ bulk helpers ---


def _bulk_item(raw) -> BulkItem:
    if isinstance(raw, BulkItem):
        item = raw
    elif isinstance(raw, (str, Path)):
        item = BulkItem(path=raw)
    elif isinstance(raw, Mapping):
        unknown = set(raw) - set(BulkItem.__dataclass_fields__)
        if unknown:
            raise UsageError(f"unknown BulkItem field(s): {', '.join(sorted(unknown))}")
        item = BulkItem(**raw)
    else:
        raise UsageError(f"bulk item must be a path, BulkItem or mapping, not {type(raw).__name__}")
    if (item.path is None) == (item.post is None):
        raise UsageError("a bulk item needs exactly one of path or post")
    return item


def _fingerprint(item: BulkItem) -> str:
    """What makes an item 'the same' across runs: the file's contents (a
    file edited since is contributed again), or the post reference."""
    if item.post:
        return f"post:{item.post}"
    return hash_file(Path(item.path).expanduser())


def _read_resume_log(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A line cut short by a crash mid-write: ignore it (the item is
            # retried; identical bytes come back as AlreadyRegistered).
            if n == len(path.read_text(encoding="utf-8").splitlines()):
                continue
            raise UsageError(f"{path}:{n}: not a JSONL resume log") from None
        if isinstance(entry, dict) and entry.get("status") in ("contributed", "skipped") and entry.get("key"):
            done[entry["key"]] = entry
    return done


def _append_resume_log(path: Path, result: BulkResult, fingerprint: str | None) -> None:
    c = result.contribution
    entry = {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "key": result.key,
        "fingerprint": fingerprint,
        "status": result.status,
        "video_uri": result.video_uri,
        "consent_uri": c.consent_uri if c else None,
        "content_hash": c.content_hash if c else None,
        "error": result.error,
        "error_type": result.error_type,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
