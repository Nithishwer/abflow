import math
import re

from abfe.mdp import read_settings


def issue(observation, code, message):
    observation["issues"].append({"code": code, "message": message})


def evidence(observation, path, role):
    if path.is_file():
        stat = path.stat()
        observation["evidence"].append(
            {
                "path": str(path),
                "role": role,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )


def tail(path, size=65536):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = max(0, stream.tell() - size)
        stream.seek(position)
        data = stream.read(size)
    if position:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace")


def production_end(path):
    before = path.stat()
    with path.open("rb") as stream:
        if before.st_size:
            stream.seek(-1, 2)
            if stream.read(1) != b"\n":
                raise ValueError(
                    "DHDL has an incomplete final line; refresh after the writer finishes"
                )
    with path.open() as stream:
        header = stream.read(16384)
    if not re.search(r'xaxis\s+label\s+"Time\s*\(ps\)"', header, re.IGNORECASE):
        raise ValueError("DHDL time unit is not explicitly ps")
    lines = [
        line
        for line in tail(path).splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "@"))
    ]
    if not lines:
        raise ValueError("No production samples in DHDL tail")
    fields = lines[-1].split()
    values = [float(field) for field in fields]
    if (
        len(values) < 2
        or not all(math.isfinite(value) for value in values)
        or values[0] < 0
    ):
        raise ValueError("Invalid final DHDL sample")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(
            "DHDL changed while it was being observed; refresh the snapshot"
        )
    return values[0] / 1000


def target_duration(path):
    settings = read_settings(path)
    steps, dt = float(settings["nsteps"]), float(settings["dt"])
    if (
        not all(math.isfinite(value) for value in (steps, dt))
        or steps < 0
        or not steps.is_integer()
        or dt <= 0
    ):
        raise ValueError("Invalid production nsteps/dt")
    if float(settings.get("tinit", 0)) != 0 or float(settings.get("init-step", 0)) != 0:
        raise ValueError(
            "Nonzero production time origin requires explicit duration mapping"
        )
    return steps * dt / 1000
