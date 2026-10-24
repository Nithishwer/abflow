import sys
from pathlib import Path

import abfe.analysis.fingerprints as analysis_fingerprints
import abfe.analysis.values as analysis_values


def run_analysis(evidence, output):
    """Use alchemlyb's existing preprocessing, loading the validated files in place."""
    from functools import partial

    import matplotlib
    import numpy as np

    matplotlib.use("Agg")
    from alchemlyb.parsing.gmx import extract_dHdl, extract_u_nk
    from alchemlyb.workflows import ABFE
    from matplotlib import pyplot as plt

    p = evidence["protocol"]
    workflow = ABFE(
        T=evidence["temperature_K"],
        units="kcal/mol",
        software="GROMACS",
        dir=str(Path(evidence["windows"][0]["path"]).parent),
        prefix="dhdl",
        suffix="xvg",
        outdirectory=str(output),
    )
    workflow.file_list = [w["path"] for w in evidence["windows"]]
    workflow._extract_u_nk = partial(extract_u_nk, filter=False)
    workflow._extract_dHdl = partial(extract_dHdl, filter=False)
    workflow.read()
    for attribute in ("u_nk_list", "dHdl_list"):
        setattr(
            workflow,
            attribute,
            [
                frame.loc[frame.index.get_level_values("time") <= evidence["cutoff_ps"]]
                for frame in getattr(workflow, attribute)
            ],
        )
    workflow.preprocess(
        skiptime=p["discard_ps"], uncorr=p["uncorr"], threshold=p["threshold"]
    )
    random_state = np.random.get_state()
    try:
        np.random.seed(p["seed"])
        estimates = {}
        for method in p["methods"]:
            workflow.estimate(
                (method,),
                **({"n_bootstraps": p["n_bootstraps"]} if method == "MBAR" else {}),
            )
            estimates.update(workflow.estimator)
        workflow.estimator = estimates
        workflow.generate_result()
        workflow.summary.to_csv(output / "ABFE_summary.csv")
        workflow.check_convergence(
            p["fractions"], estimator="MBAR", n_bootstraps=p["n_bootstraps"]
        )
        workflow.convergence.to_csv(output / "ABFE_convergence.csv")
        workflow.plot_overlap_matrix()
        workflow.plot_ti_dhdl()
        workflow.plot_dF_state()
        total = workflow.summary.loc[("Stages", "TOTAL")]
        sources = [
            analysis_fingerprints.fingerprint(sys.modules[name].__file__)
            for name in (
                "alchemlyb.workflows.abfe",
                "alchemlyb.parsing.gmx",
                "alchemlyb.estimators.mbar_",
                "alchemlyb.estimators.bar_",
                "alchemlyb.estimators.ti_",
                "alchemlyb.preprocessing.subsampling",
                "alchemlyb.convergence.convergence",
                "pymbar.mbar",
            )
        ]
        return dict(
            software_sources=sources,
            estimate_kcal_mol=analysis_values.finite(total["MBAR"]),
            uncertainty_kcal_mol=analysis_values.finite(total["MBAR_Error"]),
            retained_samples=[len(frame) for frame in workflow.u_nk_sample_list],
        )
    finally:
        np.random.set_state(random_state)
        plt.close("all")
