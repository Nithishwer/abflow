"""Read MDP settings with the existing strict and permissive parsing rules."""

from pathlib import Path


def read_settings(path, *, strict=False):
    """Strict callers lowercase keys and reject duplicates; others keep the last value."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.split(";", 1)[0]
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip().replace("_", "-")
            if strict:
                key = key.lower()
                if key in result:
                    raise ValueError(f"Duplicate MDP setting {key}: {path}")
            result[key] = value.strip()
    return result
