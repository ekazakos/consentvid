"""ConsentVid: a consent-first video dataset platform on ATproto.

The Python API (``Session``, ``Terms``, ...) lives in :mod:`consentvid.api`
and is importable from here; it is loaded on first use, so ``import
consentvid`` stays cheap.
"""

__version__ = "0.1.0"

_API_NAMES = frozenset(
    {
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
    }
)

__all__ = sorted(_API_NAMES)


def __getattr__(name: str):
    if name in _API_NAMES:
        from consentvid import api

        value = getattr(api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'consentvid' has no attribute {name!r}")


def __dir__():
    return sorted({*globals(), *_API_NAMES})
