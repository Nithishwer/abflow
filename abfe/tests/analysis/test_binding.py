import json
import math
from pathlib import Path

import abfe.analysis.combine as analysis_combine
import abfe.registry.read as registry_read

from .fixtures import AnalysisFixture


class BindingTests(AnalysisFixture):
    def test_shared_solvent_result_uncertainty_and_replacement_invalidation(self):
        c = self.analyse()
        s = self.analyse(self.solvent_id)
        bound = analysis_combine.binding(
            self.registry, self.complex_id, self.review(c, s)
        )
        self.assertEqual(bound["result"]["estimate_kcal_mol"], -3)
        self.assertEqual(bound["result"]["shared_solvent_variance"], 4)
        self.assertAlmostEqual(bound["result"]["uncertainty_kcal_mol"], math.sqrt(8))
        self.analyse(self.solvent_id)
        row = next(
            r
            for r in registry_read.read_registry(self.registry)[1]
            if r["spec"]["id"] == self.complex_id
        )
        self.assertEqual(row["binding_result"]["state"], "outdated")
        self.assertTrue((Path(bound["output"]) / "record.json").exists())

    def test_incompatible_or_unknown_solvent_review_cannot_pass(self):
        c = self.analyse()
        s = self.analyse(self.solvent_id)
        path = self.review(c, s)
        content = json.loads(path.read_text())
        content["solvent"]["charge_treatment"] = "different"
        path.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError, "incompatible"):
            analysis_combine.check_compatibility(path, c, s)
        content["solvent"] = dict(content["complex"])
        content["solvent"]["ligand_parameters"] = "unknown"
        path.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError, "incompatible"):
            analysis_combine.check_compatibility(path, c, s)
