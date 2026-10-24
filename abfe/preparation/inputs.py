import hashlib
from pathlib import Path

import abfe.registry.values as registry_values


def digest(value):
    return hashlib.sha256(registry_values.encode(value).encode()).hexdigest()


def snapshot(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(
            f"Symlink input/output requires explicit reconciliation: {path}"
        )
    if not path.exists():
        raise ValueError(f"Missing preparation input/output: {path}")
    result = {}
    for item in sorted(path.rglob("*")) if path.is_dir() else [path]:
        if "__pycache__" in item.parts:
            continue
        if item.is_symlink():
            raise ValueError(
                f"Symlink input/output requires explicit reconciliation: {item}"
            )
        if item.is_dir():
            result[str(item.relative_to(path)) + "/"] = None
        elif item.is_file():
            sha = hashlib.sha256()
            before = item.stat()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(chunk)
            after = item.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError(f"Input changed while hashing: {item}")
            result[str(item.relative_to(path)) if path.is_dir() else item.name] = (
                sha.hexdigest()
            )
        else:
            raise ValueError(f"Unsupported preparation file: {item}")
    if not result:
        raise ValueError(f"Empty preparation input/output: {path}")
    return {"path": str(path), "files": result}


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"Preparation paths must be absolute: {value}")
    return path


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {label}")
