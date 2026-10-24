import fcntl
import os
import tempfile
from pathlib import Path

import abfe.console.runs as console_runs
import abfe.registry.manifest as registry_manifest
import abfe.registry.read as registry_read


def publish_reports(registry, publish_dir):
    """Publish local evidence without collecting telemetry or operating simulations."""
    with Path(str(registry) + ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _publish_reports(registry, publish_dir)


def _publish_reports(registry, publish_dir):
    metadata, rows = registry_read.read_registry(registry)
    outputs = [Path(publish_dir) / name for name in ("campaign.html", "preview.html")]
    for output in outputs:
        registry_manifest.check_destination(
            output, metadata, [row["spec"] for row in rows]
        )
        if output.resolve() == Path(registry).resolve():
            raise ValueError("Cannot overwrite the registry")
    content = console_runs.report_html(metadata, rows)
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=output.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
