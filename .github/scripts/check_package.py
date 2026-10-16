"""Verify installed runtime resources without importing scientific dependencies."""

from importlib.metadata import distribution
from pathlib import Path
import subprocess


source_root = Path(__file__).resolve().parents[2]
tracked = subprocess.check_output(
    ["git", "ls-files", "-z", "abfe/utils"], cwd=source_root
).decode().split("\0")
resource_roots = (
    "abfe/utils/cluster/job_script_template/",
    "abfe/utils/data/mdps/",
    "abfe/utils/data/forcefield_dir/",
    "abfe/utils/data/ligand_only/",
)
expected = {"abfe/utils/water_deletor.pl"}
expected.update(
    path for path in tracked
    if path.startswith(resource_roots)
    and "/__pycache__/" not in path
    and not path.endswith((".pyc", ".pyo"))
)

installed = distribution("abflow")
if Path(installed.locate_file("abfe")).resolve().is_relative_to(source_root):
    raise RuntimeError("Check must use an installed wheel outside the source tree")

for relative_path in sorted(expected):
    packaged_file = Path(installed.locate_file(relative_path))
    if not packaged_file.is_file():
        raise FileNotFoundError(f"Missing package resource: {relative_path}")
    if packaged_file.read_bytes() != (source_root / relative_path).read_bytes():
        raise ValueError(f"Package resource differs from source: {relative_path}")

print(f"Verified {len(expected)} installed package resources for abflow {installed.version}")
