import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import merge_3mf as merger
import merge_3mf_colors as colors
import merge_3mf_mixed as mixed
from filament_report import identical_mixed_groups
from test_merge_3mf import update_project, write_project


class MixedMergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.source = self.path / "source.3mf"
        self.output = self.path / "output.3mf"
        write_project(self.source, ["#FFFFFF", "#000000"] + ["#888888"] * 6,
                      ["0", "0"] + ["1"] * 6,
                      ["", "", "1,2", "1,2", "2,1", "1,2", "1,2", "1,2"],
                      ["", "", "0.5,0.5", "0.5000,0.5000", "0.5,0.5",
                       "0.5,0.5", "0.5,0.5", "0.5,0.5"], "A")
        with zipfile.ZipFile(self.source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
            mesh = z.read("3D/Objects/object.model").replace(b'paint_color="8"', b'paint_color="85C1C0C3"')
        settings["nozzle_temperature"] = ["200"] * 16
        settings["filament_type"][5] = "PETG"
        settings["flush_volumes_matrix"] = ["0"] * 64
        settings["flush_volumes_matrix"][7 * 8] = "90"
        settings["support_interface_filament"] = "4"
        metadata.find("./object/metadata[@key='extruder']").set("value", "4")
        ET.SubElement(metadata.find("./object/part"), "metadata", {"key": "extruder", "value": "8"})
        metadata.find("./plate/metadata[@key='filament_maps']").set("value", "1 1 1 1 1 1 2 1")
        update_project(self.source, {
            merger.SETTINGS: json.dumps(settings), merger.MODEL_SETTINGS: ET.tostring(metadata),
            "3D/Objects/object.model": mesh,
            "Metadata/process_settings_1.config": json.dumps({"support_filament": "4"}),
            "Metadata/plate_1.gcode": "obsolete",
        })

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", mixed.__file__] + [str(x) for x in args],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_strict_matching_and_report(self):
        with zipfile.ZipFile(self.source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
        self.assertEqual(identical_mixed_groups(settings, metadata), [[3, 4]])
        result = self.run_cli(self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Slots 3, 4: ingredients 1,2; ratios 0.5,0.5", result.stdout)
        self.assertIn("source_merged_mixed.3mf", result.stdout)
        self.assertFalse(self.output.exists())

    def test_different_ratios_or_gradients_are_not_identical(self):
        with zipfile.ZipFile(self.source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
        settings["filament_mixed_sublayer_ratios"][3] = "0.6,0.4"
        self.assertEqual(identical_mixed_groups(settings, metadata), [])
        settings["filament_mixed_sublayer_ratios"][3] = "0.5,0.5"
        settings["filament_mixed_gradient"] = ["0"] * 8
        settings["filament_mixed_gradient"][3] = "1"
        self.assertEqual(identical_mixed_groups(settings, metadata), [])

    def test_merges_and_remaps_paint_settings_and_metadata(self):
        original = self.source.read_bytes()
        result = self.run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Merged 1 duplicate mixed slots in 1 groups.", result.stdout)
        self.assertEqual(original, self.source.read_bytes())
        with zipfile.ZipFile(self.output) as z:
            self.assertIsNone(z.testzip())
            settings = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
            self.assertIn(b'paint_color="84C0C0C3"', z.read("3D/Objects/object.model"))
            self.assertEqual(json.loads(z.read("Metadata/process_settings_1.config"))["support_filament"], "3")
            self.assertNotIn("Metadata/plate_1.gcode", z.namelist())
        self.assertEqual(settings["filament_is_mixed"], ["0", "0"] + ["1"] * 5)
        self.assertEqual(settings["filament_mixed_components"], ["", "", "1,2", "2,1", "1,2", "1,2", "1,2"])
        self.assertEqual(settings["support_interface_filament"], "3")
        self.assertEqual(settings["filament_self_index"], [str(i) for i in range(1, 8) for _ in range(2)])
        self.assertEqual(metadata.find("./object/metadata[@key='extruder']").get("value"), "3")
        self.assertEqual(metadata.find("./object/part/metadata[@key='extruder']").get("value"), "7")
        self.assertEqual(metadata.find("./plate/metadata[@key='filament_maps']").get("value"), "1 1 1 1 1 2 1")

    def test_rejects_overwrite_and_unexpected_arguments(self):
        self.output.write_bytes(b"existing")
        result = self.run_cli(self.source, self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.assertNotEqual(self.run_cli(self.source, self.source).returncode, 0)
        self.assertNotEqual(self.run_cli(self.source, self.path / "new.3mf", "3,4").returncode, 0)

    def test_color_merge_reports_new_mixed_duplicates(self):
        source = self.path / "colors.3mf"
        output = self.path / "combined.3mf"
        write_project(source, ["#FFFFFF", "#000000", "#FFFFFF", "#000000", "#888888", "#888888"],
                      ["0"] * 4 + ["1", "1"], [""] * 4 + ["1,2", "3,4"],
                      [""] * 4 + ["0.5,0.5", "0.5,0.5"], "A")
        with zipfile.ZipFile(source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
        settings["nozzle_temperature"] = ["200"] * 12
        settings["flush_volumes_matrix"] = ["0"] * 36
        update_project(source, {merger.SETTINGS: json.dumps(settings)})
        result = subprocess.run([sys.executable, "-B", colors.__file__, str(source), str(output), "1,3;2,4"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Slots 3, 4: ingredients 1,2; ratios 0.5,0.5", result.stdout)
        self.assertIn("merge_3mf_mixed.py", result.stdout)

    def test_project_merge_reports_existing_mixed_duplicates(self):
        a, b = self.path / "A.3mf", self.path / "B.3mf"
        write_project(a, ["#FFFFFF", "#000000", "#888888", "#888888"],
                      ["0", "0", "1", "1"], ["", "", "1,2", "1,2"],
                      ["", "", "0.5,0.5", "0.5,0.5"], "A")
        write_project(b, ["#FFFFFF", "#000000"], ["0", "0"], ["", ""], ["", ""], "B")
        for path in (a, b):
            with zipfile.ZipFile(path) as z:
                settings = json.loads(z.read(merger.SETTINGS))
            count = len(settings["filament_colour"])
            settings["nozzle_temperature"] = ["200"] * (count * 2)
            settings["flush_volumes_matrix"] = ["0"] * (count * count)
            update_project(path, {merger.SETTINGS: json.dumps(settings)})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            merger.merge(a, b, self.output)
        self.assertIn("Slots 5, 6: ingredients 1,2; ratios 0.5,0.5", stdout.getvalue())
        self.assertIn("merge_3mf_mixed.py", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
