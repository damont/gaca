"""Safely upsert KEY=value lines in a .env file.

Used for the two kinds of file gaca-api edits: a Hermes profile's own .env. The write is atomic
(temp file in the same directory, then rename), the result is 0600, a symlink at the target is
replaced rather than followed, and every other line is left exactly as it was.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_VALUE = re.compile(r"[A-Za-z0-9._:/@\-\[\]]*")  # no whitespace, quotes, '#', '$', or newlines
_READ_LIMIT = 64_000


def read_text(path: Path) -> str:
    """The file's text, or "" if it is missing, not a regular file, or a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, _READ_LIMIT).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def upsert_env(path: Path, values: dict[str, str]) -> None:
    for key, value in values.items():
        if not _KEY.fullmatch(key) or not _VALUE.fullmatch(value):
            raise ValueError(f"refusing to write {key!r}: key or value has unsafe characters")
    lines, seen = [], set()
    for line in read_text(path).splitlines():
        key = line.split("=", 1)[0] if "=" in line else None
        if key in values:
            if key not in seen:  # also collapses a duplicated key to one
                lines.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            lines.append(line)
    lines.extend(f"{k}={v}" for k, v in values.items() if k not in seen)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
