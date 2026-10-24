"""Local execution fingerprints and file checks; never deployed to a cluster."""

import hashlib
import json
import math
import os
from pathlib import Path

from abfe.mdp import read_settings as mdp


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha(path):
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("File changed while hashing: " + str(path))
    return result.hexdigest()


def snapshot(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a real directory: " + str(root))
    result, size = {}, 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ".partial" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise ValueError("Symlinks require reconciliation: " + str(path))
        if path.is_dir():
            result[relative + "/"] = None
        elif path.is_file():
            result[relative] = sha(path)
            size += path.stat().st_size
        else:
            raise ValueError("Unsupported file: " + str(path))
    return dict(files=result, bytes=size)


def write(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x") as stream:
        stream.write(encoded(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    descriptor = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def duration(path):
    values = mdp(path)
    if float(values.get("tinit", 0)) != 0 or int(values.get("init-step", 0)) != 0:
        raise ValueError("Nonzero simulation origin requires explicit reconciliation")
    target = float(values["dt"]) * int(values["nsteps"])
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Invalid target duration")
    return target
