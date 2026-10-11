"""Put GACA's Hermes skills (the repo's `skills/` directory) into a Hermes skills folder.

Each Hermes profile has its own `skills/` directory, so every profile needs its own copy. They go
in a category folder of their own, `<profile>/skills/gaca/<skill>/…`, and nothing outside that
folder is ever touched.

Hermes agents are allowed to edit skills they find, so this never blindly overwrites. A manifest
records the hash of every file it wrote:
  * a file that is missing is written;
  * a file that still matches what was written last time is updated to the new version;
  * a file that has been changed since (by the agent or the user) is left alone and reported;
  * a file that no longer exists upstream is removed only if it is still unmodified.

Also run inside the sandboxed service when a crew member is created, so the source is read with the
same care as the target is written: regular files only, no symlinks, size limits, plain names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CATEGORY = "gaca"
MANIFEST = ".gaca-managed.json"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_MAX_FILE = 512 * 1024
_MAX_TOTAL = 4 * 1024 * 1024
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


class SkillSyncError(Exception):
    """The source or target is not something we will read from or write to."""


@dataclass
class SyncResult:
    written: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    left_alone: list[str] = field(default_factory=list)  # changed since we wrote them

    @property
    def changed(self) -> bool:
        return bool(self.written or self.updated or self.removed)


def default_source() -> Path:
    """`skills/` next to the gaca-api directory in the repo this was installed from."""
    return Path(__file__).resolve().parents[3] / "skills"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(source: Path) -> dict[str, tuple[bytes, int]]:
    """`{"gaca-alerts/SKILL.md": (bytes, mode)}` for every file under `source`'s skill folders."""
    try:
        if source.is_symlink() or not source.is_dir():
            raise SkillSyncError(f"skills source {source} is not a directory")
    except OSError as error:
        raise SkillSyncError(f"cannot read skills source {source}: {error}") from None
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    for skill in sorted(source.iterdir()):
        if skill.is_symlink() or not skill.is_dir():
            continue  # a stray README next to the skills is not a skill
        if not _NAME.fullmatch(skill.name):
            raise SkillSyncError(f"not a valid skill name: {skill.name!r}")
        if not (skill / "SKILL.md").is_file():
            continue
        for current, dirs, names in os.walk(skill, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for name in sorted(names):
                path = Path(current) / name
                if path.suffix in _SKIP_SUFFIXES:
                    continue
                relative = path.relative_to(source)
                if not all(_PART.fullmatch(part) for part in relative.parts):
                    raise SkillSyncError(f"not a valid skill file name: {relative}")
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                except OSError as error:
                    raise SkillSyncError(f"cannot read {relative}: {error}") from None
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise SkillSyncError(f"{relative} is not a regular file")
                    if info.st_size > _MAX_FILE:
                        raise SkillSyncError(f"{relative} is too large")
                    data = os.read(fd, _MAX_FILE + 1)
                finally:
                    os.close(fd)
                total += len(data)
                if total > _MAX_TOTAL:
                    raise SkillSyncError("the skills are too large")
                files[relative.as_posix()] = (data, 0o755 if info.st_mode & 0o100 else 0o644)
    return files


def _real_dir(path: Path, *, create: bool) -> None:
    """`path` must be a real directory (never a symlink); make it if allowed and absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise SkillSyncError(f"{path} does not exist") from None
        os.mkdir(path, 0o755)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise SkillSyncError(f"{path} is not a real directory")


def _read_manifest(directory: Path) -> dict[str, str]:
    try:
        fd = os.open(directory / MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        raw = os.read(fd, 1_000_000)
    finally:
        os.close(fd)
    try:
        files = json.loads(raw).get("files", {})
    except (ValueError, AttributeError):
        return {}
    return {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(files, dict) else {}


def _current_hash(path: Path) -> str | None:
    """Hash of the regular file at `path`, or None if absent. A non-regular file (symlink, directory)
    raises: it is not ours and not safe to replace."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SkillSyncError(f"{path} cannot be read safely: {error}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SkillSyncError(f"{path} is not a regular file")
        return _sha(os.read(fd, _MAX_FILE + 1))
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gaca-skill.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sync(source: Path, skills_dir: Path, *, create_skills_dir: bool = False) -> SyncResult:
    """Install the skills under `source` into `<skills_dir>/gaca/`. `skills_dir` is a Hermes
    profile's own skills folder: it must already exist unless `create_skills_dir` (a profile that
    was only just made may not have one yet)."""
    wanted = read_source(source)
    _real_dir(skills_dir, create=create_skills_dir)
    root = skills_dir / CATEGORY
    _real_dir(root, create=True)
    manifest = _read_manifest(root)
    result = SyncResult()
    recorded: dict[str, str] = {}

    for relative, (data, mode) in wanted.items():
        target = root / relative
        folder = root
        for part in Path(relative).parent.parts:
            folder = folder / part
            _real_dir(folder, create=True)
        desired = _sha(data)
        current = _current_hash(target)
        if current is None:
            _write_atomic(target, data, mode)
            result.written.append(relative)
            recorded[relative] = desired
        elif current == desired:
            result.unchanged += 1
            recorded[relative] = desired
        elif manifest.get(relative) == current:  # untouched since we put it there: safe to update
            _write_atomic(target, data, mode)
            result.updated.append(relative)
            recorded[relative] = desired
        else:  # edited since (or never ours): leave it, and keep remembering what we last wrote
            result.left_alone.append(relative)
            if relative in manifest:
                recorded[relative] = manifest[relative]

    for relative, old_hash in manifest.items():
        if relative in wanted:
            continue
        target = root / relative
        try:
            current = _current_hash(target)
        except SkillSyncError:
            continue
        if current is None:
            continue
        if current == old_hash:
            os.unlink(target)
            result.removed.append(relative)
        else:
            result.left_alone.append(relative)
            recorded[relative] = old_hash

    _write_atomic(root / MANIFEST, json.dumps({"files": recorded}, indent=2, sort_keys=True).encode(), 0o644)
    return result
