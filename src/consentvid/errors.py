"""Exceptions with user-facing messages. The CLI turns these into clean errors."""


class ConsentVidError(Exception):
    """Base class for expected, user-actionable failures."""


class ConfigError(ConsentVidError):
    """Missing credentials, env vars or optional dependencies."""


class VerificationError(ConsentVidError):
    """A repo proof (commit signature, MST path or block hash) did not verify."""


class SignatureMismatch(VerificationError):
    """A commit's signature does not match the DID's current signing key.
    May mean the key rotated or the repo moved: worth one identity refresh."""


class LexiconDnsError(ConsentVidError):
    """The _lexicon TXT lookup failed (timeout, SERVFAIL, ...): unlike
    NXDOMAIN / no answer, this says nothing about whether a record exists."""


class RepoUnavailable(ConsentVidError):
    """The contributor's repo can't be read: account deleted, deactivated,
    taken down or suspended. ``status`` holds the machine-readable reason."""

    def __init__(self, did: str, status: str, message: str = "", *, pds: str | None = None):
        self.did = did
        self.status = status
        # The PDS that answered with this status (None when the DID
        # document itself said so, e.g. a tombstone): after a migration the
        # old PDS answers like this, so a reader re-resolves (R3-04).
        self.pds = pds
        super().__init__(message or f"repo {did} unavailable ({status})")


class UsageError(ConsentVidError, ValueError):
    """A bad argument (an option value, a URI, a missing term). ``param_hint``
    names the CLI option it corresponds to, when there is one; the CLI turns
    it into a usage error (exit status 2)."""

    def __init__(self, message: str, *, param_hint: str | None = None):
        self.param_hint = param_hint
        super().__init__(message)


class RateLimited(ConsentVidError):
    """The PDS refused a write for now (HTTP 429). ``reset``: epoch seconds
    after which a retry may succeed, if the PDS said."""

    def __init__(self, message: str, *, reset: float | None = None):
        self.reset = reset
        super().__init__(message)


class AlreadyRegistered(ConsentVidError):
    """These bytes are already registered as one of your videos (``uri``)."""

    def __init__(self, message: str, *, uri: str):
        self.uri = uri
        super().__init__(message)


class Cancelled(ConsentVidError):
    """A confirmation callback declined the action; nothing was changed."""
