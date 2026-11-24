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
from test_merge_3mf import update_project, write_project


class ColorMergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.source = self.path / "source.3mf"
        self.output = self.path / "output.3mf"
        write_project(self.source, ["#%06X" % i for i in range(1, 12)],
                      ["0"] * 9 + ["1", "1"],
                      [""] * 9 + ["1,2,4", "1,2"],
                      [""] * 9 + ["0.2,0.3,0.5", "0.5,0.5"], "A")

    def run_cli(self, *args):
        script = Path(colors.__file__)
        return subprocess.run([sys.executable, "-B", str(script)] + [str(x) for x in args],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_report_only_does_not_touch_output(self):
        self.output.write_bytes(b"existing")
        for args in ((self.source,), (self.source, self.output)):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Slot 1:", result.stdout)
            self.assertNotIn("Slot 10:", result.stdout)
            self.assertEqual(self.output.read_bytes(), b"existing")

    def test_report_suggests_original_input_not_unused_output(self):
        with zipfile.ZipFile(self.source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
        settings["filament_colour"][2] = settings["filament_colour"][0]
        update_project(self.source, {merger.SETTINGS: json.dumps(settings)})
        result = self.run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'%s' '%s' '1,3'" %
                      (self.source, self.path / "source_merged_colors.3mf"), result.stdout)
        self.assertFalse(self.output.exists())

    def test_remaps_simultaneous_sets_recipes_paint_and_settings(self):
        with zipfile.ZipFile(self.source) as z:
            settings = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
            mesh = z.read("3D/Objects/object.model").replace(b'paint_color="8"', b'paint_color="8C7C843"')
        settings["support_interface_filament"] = "11"
        settings["filament_map"] = [str(i) for i in range(1, 12)]
        metadata.find("./object/metadata[@key='extruder']").set("value", "2")
        ET.SubElement(metadata.find("./object/part"), "metadata", {"key": "extruder", "value": "9"})
        metadata.find("./plate/metadata[@key='filament_maps']").set("value", " ".join(str(i) for i in range(1, 12)))
        ET.SubElement(metadata.find("plate"), "metadata", {"key": "gcode_file", "value": "Metadata/plate_1.gcode"})
        update_project(self.source, {
            merger.SETTINGS: json.dumps(settings),
            merger.MODEL_SETTINGS: ET.tostring(metadata),
            "3D/Objects/object.model": mesh,
            "Metadata/process_settings_1.config": json.dumps({"support_interface_filament": "9"}),
            "Metadata/plate_1.json": "{}",
            "Metadata/plate_1.gcode": "obsolete",
            "3D/_rels/3dmodel.model.rels": merger.relationships(
                ["3D/Objects/object.model", "Metadata/plate_1.gcode"]),
        })
        original = self.source.read_bytes()
        result = self.run_cli(self.source, self.output, "1,2,3;4,5;7,9")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Mixed slot 11 now uses only one base filament", result.stderr)
        self.assertIn("sublayer order may change", result.stderr)
        self.assertEqual(self.source.read_bytes(), original)
        with zipfile.ZipFile(self.output) as z:
            self.assertIsNone(z.testzip())
            output = json.loads(z.read(merger.SETTINGS))
            metadata = ET.fromstring(z.read(merger.MODEL_SETTINGS))
            self.assertNotIn("Metadata/plate_1.json", z.namelist())
            self.assertNotIn("Metadata/plate_1.gcode", z.namelist())
            self.assertNotIn(b"plate_1.gcode", z.read("3D/_rels/3dmodel.model.rels"))
            self.assertIn(b'paint_color="43C443"', z.read("3D/Objects/object.model"))
            self.assertEqual(json.loads(z.read("Metadata/process_settings_1.config"))["support_interface_filament"], "4")
        self.assertEqual(output["filament_colour"], [settings["filament_colour"][i - 1] for i in (1, 4, 6, 7, 8, 10)])
        self.assertEqual(output["filament_mixed_components"][-1], "1,2")
        self.assertEqual(output["filament_mixed_sublayer_ratios"][-1], "0.5,0.5")
        self.assertEqual(output["support_interface_filament"], "1")
        self.assertEqual(output["filament_self_index"], [str(i) for i in range(1, 7) for _ in range(2)])
        self.assertEqual(output["filament_map"], ["1", "4", "6", "7", "8", "10"])
        self.assertEqual(metadata.find("./plate/metadata[@key='filament_maps']").get("value"), "1 4 6 7 8 10")
        self.assertIsNone(metadata.find("./plate/metadata[@key='gcode_file']"))
        self.assertEqual(metadata.find("./object/metadata[@key='extruder']").get("value"), "1")
        self.assertEqual(metadata.find("./object/part/metadata[@key='extruder']").get("value"), "4")
        old_matrix = settings["flush_volumes_matrix"]
        order = [1, 4, 6, 7, 8, 10]
        self.assertEqual(output["flush_volumes_matrix"], [old_matrix[(a - 1) * 11 + b - 1] for a in order for b in order])

    def test_first_listed_slot_settings_win_even_when_not_lowest(self):
        result = self.run_cli(self.source, self.output, "3,1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained settings from slot 3", result.stderr)
        with zipfile.ZipFile(self.output) as z:
            settings = json.loads(z.read(merger.SETTINGS))
        self.assertEqual(settings["filament_colour"][0], "#000003")

    def test_rejects_bad_sets_and_existing_output(self):
        for sets in ("1,2;2,3", "1,10", "0,1", "1,12", "1", "1,2;", "1,2.3", "1,1"):
            result = self.run_cli(self.source, self.output, sets)
            self.assertNotEqual(result.returncode, 0, sets)
            self.assertFalse(self.output.exists(), sets)
        self.output.write_bytes(b"existing")
        result = self.run_cli(self.source, self.output, "1,2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
