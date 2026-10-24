import hashlib
import json
import math
from datetime import datetime, timezone


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonnumeric/nonfinite analysis value")
    return number


def close_values(actual, expected):
    # GROMACS XVG lambda vectors are printed to four decimal places.
    return len(actual) == len(expected) and all(
        abs(a - b) <= 0.000051 for a, b in zip(actual, expected)
    )
