import hashlib
from pathlib import Path


def stamp(path):
    value = Path(path).stat()
    return dict(
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        inode=value.st_ino,
        device=value.st_dev,
    )


def fingerprint(path):
    source_path = str(Path(path).absolute())
    path = Path(path).resolve(strict=True)
    before, sha = stamp(path), hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    if before != stamp(path):
        raise ValueError(f"File changed while hashing: {path}")
    return dict(
        path=str(path), source_path=source_path, stamp=before, sha256=sha.hexdigest()
    )
