"""Exceptions with user-facing messages. The CLI turns these into clean errors."""


class ConsentVidError(Exception):
    """Base class for expected, user-actionable failures."""


class ConfigError(ConsentVidError):
    """Missing credentials, env vars or optional dependencies."""


class VerificationError(ConsentVidError):
    """A repo proof (commit signature, MST path or block hash) did not verify."""


class RepoUnavailable(ConsentVidError):
    """The contributor's repo can't be read: account deleted, deactivated,
    taken down or suspended. ``status`` holds the machine-readable reason."""

    def __init__(self, did: str, status: str, message: str = ""):
        self.did = did
        self.status = status
        super().__init__(message or f"repo {did} unavailable ({status})")
