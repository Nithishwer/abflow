import hashlib
import re
from pathlib import Path

import abfe.execution.transport.ssh as execution_transport_ssh


def snapshot(plan, subdirectory=""):
    body = r"""test ! -L "$root/SUBDIRECTORY"
cd "$root/SUBDIRECTORY"
find . -mindepth 1 -path '*/.partial' -prune -o -print0 |
while IFS= read -r -d '' path; do
    test ! -L "$path"
    if [ -d "$path" ]; then printf 'D\t%s\0' "${path#./}";
    elif [ -f "$path" ]; then
        before=$(stat -c '%s:%Y:%y' "$path")
        printf 'F\t%s\t' "$(stat -c %s "$path")"
        sha256sum -z -- "$path"
        test "$before" = "$(stat -c '%s:%Y:%y' "$path")"
    else exit 1; fi
done""".replace("SUBDIRECTORY", subdirectory)
    files, size = {}, 0
    for record in execution_transport_ssh.execute(plan, body).split("\0"):
        if not record:
            continue
        kind, rest = record.split("\t", 1)
        if kind == "D":
            files[rest + "/"] = None
        else:
            amount, hashed = rest.split("\t", 1)
            digest, name = hashed.split("  ", 1)
            if not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError("Invalid remote checksum")
            files[name.removeprefix("./")] = digest
            size += int(amount)
    return dict(files=files, bytes=size)


def checks(plan, data=False, partial=False):
    names = {
        "plan.json": hashlib.sha256(
            (Path(plan["output"]) / "plan.json").read_bytes()
        ).hexdigest(),
        "job.sh": plan["script_sha256"],
    }
    if plan.get("worker_sha256"):
        names["worker.py"] = plan["worker_sha256"]
    commands = ['cd "$root"']
    for name, digest in names.items():
        commands += [
            "test ! -L " + name,
            'test "$(sha256sum ' + name + ' | cut -d " " -f1)" = ' + digest,
        ]
    execution_transport_ssh.execute(plan, "\n".join(commands))
    if data:
        actual = snapshot(plan, "data")["files"]
        expected = plan["source_snapshot"]["files"]
        if any(k not in expected or expected[k] != v for k, v in actual.items()) or (
            not partial and actual != expected
        ):
            raise ValueError("Transfer conflict or changed input snapshot")
