"""Private files under ~/.consentvid: created owner-only from the first byte."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def secure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it 0700. mkdir's mode is masked
    by the umask and ignored for existing directories, hence the chmod."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path: Path, data: bytes | str) -> None:
    """Write ``data`` to ``path`` with mode 0600, atomically: the bytes go to
    an exclusively created 0600 temp file in the same directory (never through
    a symlink), which then replaces ``path``. No reader ever sees a
    group/world-readable or half-written file."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # in case of an odd umask on exotic filesystems
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
