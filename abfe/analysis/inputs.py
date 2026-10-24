import math
import re
from decimal import Decimal
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.policy as analysis_policy
import abfe.analysis.values as analysis_values
from abfe.monitoring.progress import tail


def contains_cutoff(stream, start, end, cutoff_ps):
    """Search the GROMACS time column without auditing every production row."""
    while start < end:
        middle = (start + end) // 2
        stream.seek(middle)
        if middle > start:
            stream.readline()
        line = stream.readline()
        if not line:
            end = middle
            continue
        time = Decimal(line.split(maxsplit=1)[0].decode())
        if time == cutoff_ps:
            return True
        if time < cutoff_ps:
            start = stream.tell()
        else:
            end = middle
    stream.seek(start)
    line = stream.readline()
    return bool(line) and Decimal(line.split(maxsplit=1)[0].decode()) == cutoff_ps


def read_window(path, mdp, state, count, cutoff_ps):
    """Check file identity, headers and cutoff; leave data parsing to analysis."""
    identity = analysis_fingerprints.fingerprint(path)
    dt = Decimal(mdp["dt"])
    stride = dt * Decimal(mdp["nstdhdl"])
    if not stride.is_finite() or stride <= 0 or Decimal(mdp["nstdhdl"]) % 1:
        raise ValueError("Invalid DHDL output stride")
    header = []
    with Path(path).open("rb") as stream:
        while raw := stream.readline():
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            if line.startswith(("#", "@")):
                header.append(line)
                continue
            start = stream.tell() - len(raw)
            break
        else:
            raise ValueError(f"No production samples in DHDL: {path}")
        lines = [
            line
            for line in tail(path, 262144).splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "@"))
        ]
        endpoint = Decimal(lines[-1].split(maxsplit=1)[0])
        if (
            not endpoint.is_finite()
            or endpoint < cutoff_ps
            or cutoff_ps % stride
            or (
                endpoint != cutoff_ps
                and not contains_cutoff(
                    stream, start, identity["stamp"]["size"], cutoff_ps
                )
            )
        ):
            raise ValueError(
                f"DHDL does not contain the exact requested cutoff {cutoff_ps} ps: {path}"
            )
    if identity["stamp"] != analysis_fingerprints.stamp(path):
        raise ValueError(f"DHDL changed during validation: {path}")
    if endpoint > analysis_policy.protocol()["maximum_ps"]:
        raise ValueError(f"Data exceed the 50 ns cap; preserve and review: {path}")
    text = "\n".join(header)
    if not re.search(r'xaxis\s+label\s+"Time\s*\(ps\)"', text, re.I):
        raise ValueError(f"DHDL time unit must be explicitly ps: {path}")
    match = re.search(
        r"T\s*=\s*([\d.eE+-]+)\s*\(K\).*state\s+(\d+):\s*\(([^)]+)\)\s*=\s*\(([^)]+)\)",
        text,
    )
    if not match or int(match[2]) != state:
        raise ValueError(f"DHDL lambda-state does not match its window: {path}")
    temperature = analysis_values.finite(match[1])
    components = [name.strip().removesuffix("-lambda") for name in match[3].split(",")]
    arrays = [
        [analysis_values.finite(x) for x in mdp[name + "-lambdas"].split()]
        for name in components
    ]
    if any(len(array) != count for array in arrays):
        raise ValueError(
            f"MDP lambda schedule does not cover all {count} windows: {path}"
        )
    vectors = [list(row) for row in zip(*arrays)]
    if not analysis_values.close_values(
        [analysis_values.finite(x) for x in match[4].split(",")], vectors[state]
    ):
        raise ValueError(f"DHDL lambda vector differs from MDP: {path}")
    energies = re.findall(r'legend\s+"[^"\n]*H[^"\n]*\bto\s+\(([^)]+)\)"', text)
    if len(energies) != count or any(
        not analysis_values.close_values(
            [analysis_values.finite(x) for x in row.split(",")], vector
        )
        for row, vector in zip(energies, vectors)
    ):
        raise ValueError(
            f"DHDL energy columns omit/misidentify lambda endpoints: {path}"
        )
    legends = re.findall(r"@\s+s(\d+)\s+legend", text)
    if set(map(int, legends)) != set(range(len(legends))):
        raise ValueError(f"DHDL legend indices are inconsistent: {path}")
    if temperature <= 0 or any(
        not math.isclose(analysis_values.finite(t), temperature, abs_tol=1e-6)
        for t in mdp["ref-t"].split()
    ):
        raise ValueError(f"MDP/DHDL temperatures differ: {path}")
    return dict(
        **identity,
        endpoint_ps=float(endpoint),
        state=state,
        temperature_K=temperature,
        lambda_components=components,
        lambda_vectors=vectors,
    )


def validate_inputs(spec, cutoff_ps):
    from abfe.monitoring.progress import target_duration

    p = analysis_policy.protocol()
    cutoff_ps = analysis_values.finite(cutoff_ps)
    if cutoff_ps <= 0 or cutoff_ps > p["maximum_ps"]:
        raise ValueError("Analysis cutoff must be positive and at most 50000 ps")
    if spec["leg"] not in ("complex", "solvent") or not spec.get("path"):
        raise ValueError("Analysis needs an existing alchemical leg")
    root = Path(spec["path"])
    if set(spec["windows"]) - set(p["phase_order"]):
        raise ValueError("Unrecognized window phase; provide a reviewed protocol")
    expected = [
        f"{phase}.{i:02d}"
        for phase in p["phase_order"]
        for i in range(spec["windows"].get(phase, 0))
    ]
    observed = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and re.fullmatch(r"[a-z]+\.\d+", path.name)
    }
    if not expected or observed != set(expected):
        raise ValueError(
            f"Window inventory mismatch: missing={sorted(set(expected) - observed)}, extra={sorted(observed - set(expected))}"
        )
    inputs = [analysis_fingerprints.fingerprint(path) for path in spec["input_files"]]
    windows, targets = [], []
    for state, name in enumerate(expected):
        prod = root / name / "prod"
        mdp_path = prod / f"{name.split('.')[0]}.prod.{name.split('.')[1]}.mdp"
        mdp_identity = analysis_fingerprints.fingerprint(mdp_path)
        mdp = analysis_policy.settings(mdp_path)
        if int(mdp["init-lambda-state"]) != state:
            raise ValueError(f"Incorrect MDP state assignment: {name}")
        if any(Decimal(mdp.get(key, "0")) != 0 for key in ("tinit", "init-step")):
            raise ValueError(
                "Nonzero production origin needs explicit reviewed mapping"
            )
        targets.append(target_duration(mdp_path) * 1000)
        if targets[-1] > p["maximum_ps"]:
            raise ValueError(
                "Configured target exceeds the 50 ns cap; preserve and review"
            )
        inputs.extend(
            [mdp_identity, analysis_fingerprints.fingerprint(prod / "topol.tpr")]
        )
        window = read_window(
            prod / "dhdl.xvg", mdp, state, len(expected), Decimal(str(cutoff_ps))
        )
        window["name"] = name
        windows.append(window)
    if len(set(targets)) != 1 or cutoff_ps > targets[0]:
        raise ValueError(
            "Missing/inconsistent targets or analysis cutoff beyond configured target"
        )
    first = windows[0]
    if any(
        any(
            w[key] != first[key]
            for key in ("temperature_K", "lambda_components", "lambda_vectors")
        )
        for w in windows
    ):
        raise ValueError("Inconsistent temperature or lambda schedule across windows")
    for item in inputs + windows:
        if analysis_fingerprints.stamp(item["path"]) != item["stamp"]:
            raise ValueError("Inputs changed while other windows were being validated")
    return dict(
        spec_sha256=analysis_values.digest(spec),
        protocol=p,
        protocol_sha256=analysis_values.digest(p),
        cutoff_ps=cutoff_ps,
        target_ps=targets[0],
        temperature_K=first["temperature_K"],
        windows=windows,
        inputs=inputs,
    )
