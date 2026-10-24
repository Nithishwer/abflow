import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from abfe.preparation import (
    coordinates,
    extract_membrane,
    ligand_topology,
    membrane,
    windows,
)
from abfe.preparation.directory import working_directory


class HelperFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_missing_membrane_raises_without_success_message(self):
        protein = self.root / "protein.gro"
        protein.write_text("protein\n0\n1 1 1\n")
        messages = io.StringIO()
        with working_directory(self.root), redirect_stdout(messages):
            with self.assertRaises(FileNotFoundError):
                coordinates.copy_complex2membrane()
        self.assertNotIn("successfully", messages.getvalue())
        self.assertEqual(
            (self.root / "complex_membrane.gro").read_bytes(), protein.read_bytes()
        )

    def test_invalid_coordinate_count_preserves_destination(self):
        source, destination = self.root / "source.gro", self.root / "protein.gro"
        source.write_text("ligand\n0\n1 1 1\n")
        destination.write_text("protein\ninvalid count\n1 1 1\n")
        before = destination.read_bytes()
        with self.assertRaises(ValueError):
            coordinates.addgro2gro(source, destination)
        self.assertEqual(destination.read_bytes(), before)

    def test_missing_topology_marker_raises_without_writing(self):
        topology = self.root / "topol.top"
        topology.write_text("; no include marker\n")
        before = topology.read_bytes()
        with self.assertRaisesRegex(ValueError, "Pattern"):
            ligand_topology.add_line_after_pattern(
                topology, "forcefield.itp", "include"
            )
        self.assertEqual(topology.read_bytes(), before)

    def test_missing_or_invalid_popc_count_stops_before_append(self):
        topology = self.root / "topol.top"
        topology.write_text("; existing topology\n")
        charmm = self.root / "charmm_gmx"
        charmm.mkdir()
        for text in ("; no POPC\n", "POPC\n", "POPC invalid\n"):
            with self.subTest(text=text):
                (charmm / "topol.top").write_text(text)
                with working_directory(self.root):
                    with self.assertRaises(ValueError):
                        membrane.includememtop([], [])
                self.assertEqual(topology.read_text(), "; existing topology\n")

    def test_required_file_errors_propagate(self):
        missing = self.root / "missing"
        calls = (
            (coordinates.copy_lig_gro_2_protein, (missing, missing)),
            (extract_membrane.replace_second_line, (missing, 1)),
            (ligand_topology.add_ligposres_to_topol, ("missing",)),
            (ligand_topology.add_toppar2protein_include, (missing,)),
            (membrane.get_popc_number, (missing,)),
        )
        with working_directory(self.root):
            for helper, args in calls:
                with self.subTest(helper=helper.__name__):
                    with self.assertRaises(FileNotFoundError):
                        helper(*args)

    def test_missing_templates_stop_window_generation_immediately(self):
        empty = self.root / "templates"
        empty.mkdir()
        for helper in (
            windows.create_directories_with_MDP_pertuball,
            windows.create_directories_with_MDP_pertuball_smaller_dumps,
            windows.create_directories_with_soluble_MDP,
        ):
            with self.subTest(helper=helper.__name__):
                output = self.root / helper.__name__
                output.mkdir()
                with (
                    working_directory(output),
                    patch.object(windows.pkg_resources, "files", return_value=empty),
                ):
                    with self.assertRaises(FileNotFoundError):
                        helper("coul", "Protein", "Water")
                self.assertFalse((output / "coul.01").exists())
                self.assertEqual(list(output.rglob("*.mdp")), [])
