"""Source changes require review rather than silently accepting old provenance."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abfe.analysis import identity


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "analysis"
        shutil.copytree(Path(identity.__file__).parent, self.source)
        package = Path(identity.__file__).parents[1]
        for name in ("mdp.py", "monitoring/jobs.py"):
            target = self.source.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package / name, target)
        location = patch.object(identity, "__file__", str(self.source / "identity.py"))
        location.start()
        self.addCleanup(location.stop)

    def test_validation_change_does_not_silently_accept_old_provenance(self):
        self.assertFalse(identity.matches(identity.PREVIOUS_CODE_SHA256))
        self.assertTrue(identity.matches(identity.current()))
        self.assertFalse(identity.matches("previous-code"))

    def test_changed_module_invalidates_old_and_new_records(self):
        recorded = identity.current()
        policy = self.source / "policy.py"
        policy.write_text(policy.read_text() + "\n# Changed after migration\n")
        self.assertFalse(identity.matches(recorded))
        self.assertFalse(identity.matches(identity.PREVIOUS_CODE_SHA256))

    def test_added_module_is_part_of_the_source_identity(self):
        recorded = identity.current()
        (self.source / "extra.py").write_text("unexpected = True\n")
        self.assertFalse(identity.matches(recorded))
        self.assertFalse(identity.matches(identity.PREVIOUS_CODE_SHA256))

    def test_shared_helpers_are_part_of_the_source_identity(self):
        for name in ("mdp.py", "monitoring/jobs.py"):
            with self.subTest(name=name):
                recorded = identity.current()
                path = self.source.parent / name
                path.write_text(path.read_text() + "\n# Changed shared dependency\n")
                self.assertFalse(identity.matches(recorded))
                self.assertFalse(identity.matches(identity.PREVIOUS_CODE_SHA256))

    def test_editing_the_manifest_cannot_accept_an_unrelated_old_version(self):
        import json

        path = self.source / "source-migration.json"
        manifest = json.loads(path.read_text())
        manifest["previous_sha256"] = "previous-code"
        path.write_text(json.dumps(manifest))
        self.assertFalse(identity.matches("previous-code"))
        self.assertFalse(identity.matches(identity.PREVIOUS_CODE_SHA256))
