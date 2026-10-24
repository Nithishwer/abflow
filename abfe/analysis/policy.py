import json
from importlib.resources import files

from abfe.mdp import read_settings


def protocol():
    return json.loads((files("abfe") / "analysis_protocol.json").read_text())


def settings(path):
    return read_settings(path, strict=True)
