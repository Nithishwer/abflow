import os
import shutil
import time
import pandas as pd
import argparse
from loguru import logger
from typing import List, Dict
from alchemlyb.workflows import ABFE

logger.add("abfe_analysis.log", rotation="10 MB")

class ABFEAnalyzer:
    def __init__(
        self,
        project_root: str,
        abfe_replicates: int,
        ignore_folders: List[str] = None,
        legs_config: Dict[str, int] = None,
        temperature: float = 298.0,
    ):
        self.project_root = project_root
        self.abfe_replicates = abfe_replicates
        self.ignore_folders = ignore_folders or []
        self.legs_config = legs_config or {
            "complex": {"rest": 11, "coul": 10, "vdw": 21},
            "ligand": {"coul": 11, "vdw": 21},
        }
        self.temperature = temperature
        logger.info(f"Initialized ABFEAnalyzer at {project_root} for {abfe_replicates} replicates.")

    @staticmethod
    def check_folder(path: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Folder not found: {path}")

    @staticmethod
    def create_folder(path: str):
        os.makedirs(path, exist_ok=True)
        logger.info(f"Ensured folder exists: {path}")

    @staticmethod
    def copy_dhdl_files(source_base: str, dest_dir: str, legs: Dict[str, int], molecule: str):
        ABFEAnalyzer.create_folder(dest_dir)
        for leg, num_windows in legs.items():
            for i in range(num_windows):
                idx = str(i).zfill(2)
                src = os.path.join(source_base, f"{leg}.{idx}/prod/dhdl.xvg")
                dst = os.path.join(dest_dir, f"dhdl.{leg}.{idx}.xvg")
                if os.path.exists(src):
                    shutil.copy(src, dst)
                    logger.info(f"Copied {molecule} {leg} {idx}: {src} -> {dst}")
                else:
                    logger.warning(f"Missing file: {src}")

    def analyse_dhdl(self, path: str):
        cwd = os.getcwd()
        os.chdir(path)
        logger.info(f"Running alchemlyb workflow in {path}")

        found = any(fname.endswith(".xvg") and fname.startswith("dhdl.") for fname in os.listdir("."))
        if not found:
            logger.warning(f"No DHDL files found in {path}, skipping analysis.")
            os.chdir(cwd)
            return

        start_time = time.time()
        workflow = ABFE(
            units="kcal/mol",
            software="GROMACS",
            dir="./",
            prefix="dhdl",
            suffix="xvg",
            T=self.temperature,
            outdirectory="./",
        )
        workflow.run(
            skiptime=10,
            uncorr="dhdl",
            threshold=50,
            methods=("MBAR", "BAR", "TI"),
            overlap="O_MBAR.pdf",
            breakdown=True,
            n_bootstraps=50,
            forwrev=10,
            n_jobs=1,
        )
        workflow.summary.to_csv("ABFE_summary.csv")
        workflow.convergence.to_csv("ABFE_convergence.csv")
        logger.info(f"Time taken for analysis: {time.time() - start_time:.2f}s")
        logger.info(f"Workflow complete for {path}")
        os.chdir(cwd)

    @staticmethod
    def get_rest_dG(filepath: str) -> float:
        with open(filepath, "r") as f:
            for line in f:
                if line.startswith("dG_off"):
                    return float(line.split(",")[1].split(":")[1])
        raise ValueError("dG_off not found")

    @staticmethod
    def extract_dG_from_summary(filepath: str, leg: str, stage: str) -> List[float]:
        # Skip the first row to avoid the (0,0,1) orientation tuple
        df = pd.read_csv(filepath, skiprows=1)
        df.columns = ["PROCESS", "ID", "MBAR", "MBAR_Error",
                    "BAR", "BAR_Error", "TI", "TI_Error"]
        matching = df[df["ID"] == leg]
        if matching.empty:
            raise ValueError(f"No matching ID='{leg}' found in {filepath}")
        if len(matching) > 1:
            raise ValueError(f"Multiple rows found for ID='{leg}' in {filepath}.")

        row = matching.squeeze()
        return [f"{stage}_{leg}"] + row.iloc[2:].astype(float).tolist()


    def compile_results(self, analysis_path: str):
        output_csv = os.path.join(analysis_path, "analysis", "abfe_results.csv")
        if os.path.exists(output_csv):
            logger.info(f"Results already compiled in {output_csv}, skipping.")
            return

        logger.info(f"Compiling results in {analysis_path}")
        os.chdir(analysis_path)
        os.chdir("analysis")
        rest_dG = self.get_rest_dG("../boresch/resgen_output.txt")

        records = [
            self.extract_dG_from_summary("ligand/ABFE_summary.csv", "coul", "ligand"),
            self.extract_dG_from_summary("ligand/ABFE_summary.csv", "vdw", "ligand"),
            ["anal_rest", rest_dG, 0, rest_dG, 0, rest_dG, 0],
            self.extract_dG_from_summary("complex/ABFE_summary.csv", "bonded", "complex"),
            self.extract_dG_from_summary("complex/ABFE_summary.csv", "coul", "complex"),
            self.extract_dG_from_summary("complex/ABFE_summary.csv", "vdw", "complex"),
        ]
        df = pd.DataFrame(
            records,
            columns=["state", "MBAR", "MBAR_Error", "BAR", "BAR_Error", "TI", "TI_Error"],
        )

        totals = {
            "state": "total",
            "MBAR": df[df.state.str.startswith("ligand")]["MBAR"].sum() + df[df.state == "anal_rest"]["MBAR"].sum() -
                    df[df.state.str.startswith("complex")]["MBAR"].sum(),
            "MBAR_Error": df["MBAR_Error"].sum(),
            "BAR": df[df.state.str.startswith("ligand")]["BAR"].sum() + df[df.state == "anal_rest"]["BAR"].sum() -
                df[df.state.str.startswith("complex")]["BAR"].sum(),
            "BAR_Error": df["BAR_Error"].sum(),
            "TI": df[df.state.str.startswith("ligand")]["TI"].sum() + df[df.state == "anal_rest"]["TI"].sum() -
                df[df.state.str.startswith("complex")]["TI"].sum(),
            "TI_Error": df["TI_Error"].sum(),
        }
        df = pd.concat([df, pd.DataFrame([totals])], ignore_index=True)
        df.to_csv("abfe_results.csv", index=False)
        logger.info(f"Results written to abfe_results.csv")

    def create_symlink(self, src: str, dst: str):
        if not os.path.exists(src):
            raise FileNotFoundError(f"Cannot symlink: source does not exist: {src}")
        if not os.path.exists(dst):
            os.symlink(src, dst)
            logger.info(f"Created symlink: {dst} -> {src}")
        else:
            logger.info(f"Symlink already exists: {dst}")

    def process_complex(self, path: str):
        summary_file = os.path.join(path, "analysis", "complex", "ABFE_summary.csv")
        if os.path.exists(summary_file):
            logger.info(f"Complex analysis already done at {summary_file}, skipping.")
            return

        self.check_folder(path)
        self.check_folder(os.path.join(path, "complex"))
        self.create_folder(os.path.join(path, "analysis"))
        self.copy_dhdl_files(
            os.path.join(path, "complex"),
            os.path.join(path, "analysis", "complex"),
            self.legs_config["complex"],
            "complex"
        )
        self.analyse_dhdl(os.path.join(path, "analysis", "complex"))

    def process_ligand(self, path: str):
        summary_file = os.path.join(path, "analysis", "ligand", "ABFE_summary.csv")
        if os.path.exists(summary_file):
            logger.info(f"Ligand analysis already done at {summary_file}, skipping.")
            return

        self.check_folder(path)
        self.check_folder(os.path.join(path, "ligand"))
        self.create_folder(os.path.join(path, "analysis"))
        self.copy_dhdl_files(
            os.path.join(path, "ligand"),
            os.path.join(path, "analysis", "ligand"),
            self.legs_config["ligand"],
            "ligand"
        )
        self.analyse_dhdl(os.path.join(path, "analysis", "ligand"))

    def run(self):
        abfe_paths = sorted([
            os.path.join(self.project_root, d)
            for d in os.listdir(self.project_root)
            if d.startswith("abfe_van") and os.path.isdir(os.path.join(self.project_root, d))
        ])
        logger.info(f"ABFE replicate folders to process: {abfe_paths}")

        abfe_paths = [p for p in abfe_paths if any(p.endswith(f"_r{i+1}") for i in range(self.abfe_replicates))]

        reference_path = os.path.join(self.project_root, "abfe_van1_hrex_r1")
        self.check_folder(reference_path)
        self.process_ligand(reference_path)

        ref_lig_path = os.path.join(reference_path, "analysis", "ligand")
        for path in abfe_paths:
            if path != reference_path:
                dst_lig = os.path.join(path, "analysis", "ligand")
                self.create_folder(os.path.join(path, "analysis"))
                self.create_symlink(ref_lig_path, dst_lig)

        for path in abfe_paths:
            self.process_complex(path)

        for path in abfe_paths:
            self.compile_results(path)

def main():
    parser = argparse.ArgumentParser(description="Run ABFE analysis on multiple replicates.")
    parser.add_argument("--root", required=True, help="Root directory containing abfe_van* folders")
    parser.add_argument("--nrep", required=True, type=int, help="Number of ABFE replicates")
    args = parser.parse_args()

    analyzer = ABFEAnalyzer(project_root=args.root, abfe_replicates=args.nrep)
    analyzer.run()

if __name__ == "__main__":
    main()
