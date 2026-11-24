import contextlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import merge_3mf as merger
from filament_report import format_filament_report


def write_project(path, colors, mixed, components, ratios, label, plates=1):
    settings = {
        "printer_model": "Test printer", "nozzle_diameter": ["0.4"],
        "filament_colour": colors, "filament_is_mixed": mixed,
        "filament_mixed_components": components, "filament_mixed_sublayer_ratios": ratios,
        "filament_type": ["PLA"] * len(colors),
        "filament_self_index": [str(i) for i in range(1, len(colors) + 1) for _ in range(2)],
        "filament_map": ["1"] * len(colors), "filament_volume_map": ["0"] * len(colors),
        "filament_nozzle_map": ["0"] * len(colors),
        "nozzle_temperature": [str(190 + i) for i in range(len(colors)) for _ in range(2)],
        "flush_volumes_matrix": [str(i) for i in range(len(colors) ** 2)],
        "layer_height": "0.2" if label == "A" else "0.16",
    }
    root = """<model xmlns="{core}" xmlns:p="{prod}" unit="millimeter">
      <resources><object id="2" p:UUID="same-object"><components>
      <component objectid="1" p:path="/3D/Objects/object.model" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
      </components></object></resources><build>
      <item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 20 0" printable="1"/>
      </build></model>""".format(core=merger.CORE, prod=merger.PROD)
    # Same subfile name, numeric IDs, UUIDs, and transforms across both inputs.
    mesh = """<model xmlns="{core}" xmlns:p="{prod}" unit="millimeter">
      <resources><object id="1" p:UUID="same-mesh"><mesh><vertices>
      <vertex x="{x}" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>
      </vertices><triangles><triangle v1="0" v2="1" v3="2" paint_color="8"/>
      </triangles></mesh></object></resources></model>""".format(
          core=merger.CORE, prod=merger.PROD, x=0 if label == "A" else 5)
    metadata = ET.Element("config")
    obj = ET.SubElement(metadata, "object", {"id": "2"})
    ET.SubElement(obj, "metadata", {"key": "name", "value": label})
    ET.SubElement(obj, "metadata", {"key": "extruder", "value": "1"})
    part = ET.SubElement(obj, "part", {"id": "1", "subtype": "normal_part"})
    ET.SubElement(part, "metadata", {"key": "name", "value": label})
    ET.SubElement(part, "metadata", {"key": "mesh_shared", "value": "1"})
    for i in range(1, plates + 1):
        plate = ET.SubElement(metadata, "plate")
        ET.SubElement(plate, "metadata", {"key": "plater_id", "value": str(i)})
        ET.SubElement(plate, "metadata", {"key": "filament_maps", "value": " ".join(["1"] * len(colors))})
        ET.SubElement(plate, "metadata", {"key": "filament_volume_maps", "value": " ".join(["0"] * len(colors))})
        instance = ET.SubElement(plate, "model_instance")
        for key, value in (("object_id", "2"), ("instance_id", "0"), ("identify_id", "1")):
            ET.SubElement(instance, "metadata", {"key": key, "value": value})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(merger.SETTINGS, json.dumps(settings))
        archive.writestr(merger.MODEL_SETTINGS, ET.tostring(metadata))
        archive.writestr(merger.ROOT_MODEL, root)
        archive.writestr("3D/Objects/object.model", mesh)
        archive.writestr("_rels/.rels", merger.relationships([merger.ROOT_MODEL]))
        archive.writestr("[Content_Types].xml",
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')


def update_project(path, changes):
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries.update(changes)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.a, self.b, self.out = [self.base / n for n in ("A.3mf", "B.3mf", "output.3mf")]

    def merge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return merger.merge(self.a, self.b, self.out)

    def test_concatenates_palettes_and_prevents_mesh_aliasing(self):
        write_project(self.a, ["R", "mixA", "G", "K"], ["0", "1", "0", "0"],
                      ["", "1,3", "", ""], ["", "0.5,0.5", "", ""], "A")
        write_project(self.b, ["C", "Y", "mixB", "K"], ["0", "0", "1", "0"],
                      ["", "", "1,2", ""], ["", "", "0.25,0.75", ""], "B")
        maps = self.merge()
        self.assertEqual(maps, [(0, 1, 7, 2, 3), (0, 4, 5, 8, 6)])
        with zipfile.ZipFile(self.out) as archive:
            settings = json.loads(archive.read(merger.SETTINGS))
            self.assertEqual(settings["filament_colour"], ["R", "G", "K", "C", "Y", "K", "mixA", "mixB"])
            self.assertEqual(settings["filament_mixed_components"][-2:], ["1,2", "4,5"])
            self.assertEqual(settings["filament_mixed_sublayer_ratios"][-2:], ["0.5,0.5", "0.25,0.75"])
            self.assertEqual(settings["filament_self_index"], [str(i) for i in range(1, 9) for _ in range(2)])
            self.assertEqual(settings["nozzle_diameter"], ["0.4"])
            self.assertEqual(settings["layer_height"], "0.2")
            self.assertEqual(settings["nozzle_temperature"][:6], ["190", "190", "192", "192", "193", "193"])
            files = {n: archive.read(n) for n in archive.namelist()}
        metadata = ET.fromstring(files[merger.MODEL_SETTINGS])
        objects = metadata.findall("object")
        self.assertEqual([o.find("./metadata[@key='extruder']").get("value") for o in objects], ["1", "4"])
        self.assertNotEqual(objects[0].find("part").get("id"), objects[1].find("part").get("id"))
        for obj in objects:
            part = obj.find("part")
            self.assertEqual(part.find("./metadata[@key='mesh_shared']").get("value"), part.get("id"))
        self.assertIn(b'paint_color="4C"', files["3D/Objects/object.model"])
        self.assertIn(b'paint_color="2C"', files["inputB/3D/Objects/object.model"])
        self.assertIn(b'x="0"', files["3D/Objects/object.model"])
        self.assertIn(b'x="5"', files["inputB/3D/Objects/object.model"])
        merger.validate_output(files, 8)
        plate = metadata.find("plate")
        self.assertEqual(len(plate.findall("model_instance")), 2)
        self.assertEqual([m.get("value") for m in plate.findall("./model_instance/metadata[@key='identify_id']")],
                         ["1", "2"])
        second_output = self.base / "three_projects.3mf"
        with contextlib.redirect_stdout(io.StringIO()):
            merger.merge(self.out, self.b, second_output)
        with zipfile.ZipFile(second_output) as archive:
            self.assertIn("inputB2/3D/Objects/object.model", archive.namelist())
            metadata = ET.fromstring(archive.read(merger.MODEL_SETTINGS))
            self.assertEqual(len(metadata.findall("object")), 3)

    def test_base_only_projects_and_additional_plates(self):
        write_project(self.a, ["K"], ["0"], [""], [""], "A")
        write_project(self.b, ["K", "W"], ["0", "0"], ["", ""], ["", ""], "B", plates=2)
        # A's fixture must not paint slot 2 when it has only one filament.
        with zipfile.ZipFile(self.a) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        entries["3D/Objects/object.model"] = entries["3D/Objects/object.model"].replace(b'paint_color="8"', b'paint_color="4"')
        with zipfile.ZipFile(self.a, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.merge()
        with zipfile.ZipFile(self.out) as archive:
            settings = json.loads(archive.read(merger.SETTINGS))
            metadata = ET.fromstring(archive.read(merger.MODEL_SETTINGS))
        self.assertEqual(settings["filament_colour"], ["K", "K", "W"])
        self.assertEqual(settings["filament_is_mixed"], ["0"] * 3)
        self.assertEqual(len(metadata.findall("plate")), 2)

    def test_short_default_mixed_arrays_are_expanded(self):
        write_project(self.a, ["#FFFFFF", "#000000", "#FF0000"], ["0"] * 3,
                      [""] * 3, [""] * 3, "A")
        write_project(self.b, ["#FFFFFF", "#000000"], ["0"] * 2,
                      [""] * 2, [""] * 2, "B")
        with zipfile.ZipFile(self.a) as archive:
            settings = json.loads(archive.read(merger.SETTINGS))
        for key in ("filament_is_mixed", "filament_mixed_components", "filament_mixed_sublayer_ratios"):
            settings[key] = settings[key][:1]
        settings["filament_mixed_gradient"] = ["0"]
        settings["filament_mixed_gradient_curve"] = [""]
        update_project(self.a, {merger.SETTINGS: json.dumps(settings)})
        with zipfile.ZipFile(self.b) as archive:
            other = json.loads(archive.read(merger.SETTINGS))
        other["filament_mixed_gradient"] = ["0", "0"]
        other["filament_mixed_gradient_curve"] = ["", ""]
        update_project(self.b, {merger.SETTINGS: json.dumps(other)})
        self.merge()
        with zipfile.ZipFile(self.out) as archive:
            result = json.loads(archive.read(merger.SETTINGS))
        self.assertEqual(result["filament_is_mixed"], ["0"] * 5)
        self.assertEqual(result["filament_mixed_components"], [""] * 5)
        self.assertEqual(result["filament_mixed_gradient"], ["0"] * 5)

    def test_short_nondefault_mixed_arrays_are_rejected(self):
        settings = {"filament_colour": ["A", "B", "C"], "filament_is_mixed": ["0", "1"],
                    "filament_mixed_components": ["", "1,3", ""],
                    "filament_mixed_sublayer_ratios": ["", "0.5,0.5", ""]}
        with self.assertRaisesRegex(ValueError, "filament_is_mixed"):
            merger.palette(settings)
        settings["filament_is_mixed"] = ["0", "1", "0"]
        settings["filament_mixed_components"] = ["", "1,3"]
        with self.assertRaisesRegex(ValueError, "recipe arrays"):
            merger.palette(settings)

    def test_split_tree_and_extended_states(self):
        # Four leaves, read right-to-left: slot 1, unpainted, slot 2, slot 3.
        mapping = tuple([0, 19, 2, 34] + list(range(4, 35)))
        self.assertEqual(merger.remap_paint("0C8043", mapping), "1FFC801FC3")
        with self.assertRaisesRegex(ValueError, "Truncated"):
            merger.remap_paint("C", mapping)

    def test_report_lists_only_base_slots_and_counts_distinct_mixes(self):
        settings = {
            "filament_colour": ["#000000", "#123456", "#0090DA", "#E24585", "#FFFFFF"],
            "filament_is_mixed": ["0", "1", "0", "0", "0"],
            "filament_mixed_components": ["", "1,3,3", "", "", ""],
            "filament_type": ["PLA", "PLA", "PLA", "PLA", "PETG"],
            "filament_vendor": ["Bambu Lab", "Bambu Lab", "Other", "Other", "", "",
                                "Bambu Lab", "Bambu Lab", "", ""],
        }
        lines = format_filament_report(settings).splitlines()
        self.assertEqual(lines[1], "Slot 1: black (#000000), PLA, Bambu Lab (used in 1 mixed filaments)")
        self.assertEqual(lines[2], "Slot 3: blue/cyan (#0090DA), PLA, vendor unavailable (used in 1 mixed filaments)")
        self.assertEqual(lines[3], "Slot 4: pink/magenta (#E24585), PLA, Bambu Lab (used in 0 mixed filaments)")
        self.assertEqual(lines[4], "Slot 5: white (#FFFFFF), PETG, vendor unavailable (used in 0 mixed filaments)")
        self.assertEqual(len(lines), 5)

    def test_merge_prints_report_for_remapped_palette(self):
        write_project(self.a, ["#000000", "#0090DA"], ["0", "0"], ["", ""], ["", ""], "A")
        write_project(self.b, ["#FFFFFF", "#123456"], ["0", "1"], ["", "1,1"],
                      ["", "0.5,0.5"], "B")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            merger.merge(self.a, self.b, self.out)
        text = output.getvalue()
        self.assertIn("Slot 1: black (#000000), PLA, vendor unavailable (used in 0 mixed filaments)", text)
        self.assertIn("Slot 3: white (#FFFFFF), PLA, vendor unavailable (used in 1 mixed filaments)", text)
        self.assertNotIn("Slot 4:", text)
        self.assertIn("Consider using merge_3mf_colors.py to merge colors for more efficient printing.", text)

    def test_suggests_only_matching_base_slots_with_quoted_paths(self):
        settings = {
            "filament_colour": ["#0090da", "#0090DA", "#FFFFFF", "#0090DA", "#ffffff", "invalid"],
            "filament_is_mixed": ["0", "0", "0", "1", "0", "0"],
            "filament_mixed_components": ["", "", "", "1,3", "", ""],
        }
        text = format_filament_report(settings, Path("Bob's Project.3mf"))
        self.assertIn("'Bob''s Project.3mf' 'Bob''s Project_merged_colors.3mf' '1,2;3,5'", text)
        self.assertIn("unrecognized color values in base slots: 6", text)
        self.assertIn("Matching colors do not guarantee compatible materials or settings.", text)
        settings["filament_colour"][1] = "#000000"
        settings["filament_colour"][4] = "#AAAAAA"
        self.assertIn("No identical base filament colors found.", format_filament_report(settings, "input.3mf"))

    def test_merge_suggests_using_created_output(self):
        for path, label in ((self.a, "A"), (self.b, "B")):
            write_project(path, ["#FFFFFF", "#000000"], ["0", "0"], ["", ""], ["", ""], label)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            merger.merge(self.a, self.b, self.out)
        self.assertIn("'%s' '%s' '1,3;2,4'" %
                      (self.out, self.base / "output_merged_colors.3mf"), stdout.getvalue())

    def test_map_tails_and_optional_inheritance(self):
        a = {"printer_model": "printer", "filament_colour": ["A", "B"],
             "filament_nozzle_map": ["3", "4", "0", "7"],
             "filament_volume_map": ["1", "2"],
             "inherits_group": ["A-process", "A1", "A2", "A-printer"]}
        b = {"printer_model": "printer", "filament_colour": ["C"],
             "filament_nozzle_map": ["5"],
             "filament_volume_map": ["3", "0", "0"],
             "different_settings_to_system": ["B-process", "B1", "B-printer"]}
        merged, _, _ = merger.merge_palettes(a, b)
        self.assertEqual(merged["filament_nozzle_map"], ["3", "4", "5"])
        self.assertEqual(merged["filament_volume_map"], ["1", "2", "3"])
        self.assertEqual(merged["inherits_group"], ["A-process", "A1", "A2", "", "A-printer"])
        self.assertEqual(merged["different_settings_to_system"], ["", "", "", "B1", ""])

    def test_cut_indices_survive_repeated_merges(self):
        for path, label in ((self.a, "A"), (self.b, "B")):
            write_project(path, ["R", "G"], ["0", "0"], ["", ""], ["", ""], label)
            update_project(path, {"Metadata/cut_information.xml":
                b'<objects><object id="1"><cut_id id="0" check_sum="1" connectors_cnt="0"/></object></objects>'})
        self.merge()
        second = self.base / "three.3mf"
        with contextlib.redirect_stdout(io.StringIO()):
            merger.merge(self.out, self.a, second)
        with zipfile.ZipFile(second) as archive:
            cut = ET.fromstring(archive.read("Metadata/cut_information.xml"))
        self.assertEqual([obj.get("id") for obj in cut], ["1", "2", "3"])

    def test_short_plate_map_uses_source_global_values_with_warning(self):
        for path, label in ((self.a, "A"), (self.b, "B")):
            write_project(path, ["R", "G"], ["0", "0"], ["", ""], ["", ""], label)
        with zipfile.ZipFile(self.a) as archive:
            settings = json.loads(archive.read(merger.SETTINGS))
            metadata = ET.fromstring(archive.read(merger.MODEL_SETTINGS))
        settings["filament_volume_map"] = ["2", "3"]
        metadata.find("./plate/metadata[@key='filament_volume_maps']").set("value", "1")
        update_project(self.a, {merger.SETTINGS: json.dumps(settings), merger.MODEL_SETTINGS: ET.tostring(metadata)})
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            self.merge()
        self.assertIn("missing entries use the project's global map", warnings.getvalue())
        with zipfile.ZipFile(self.out) as archive:
            metadata = ET.fromstring(archive.read(merger.MODEL_SETTINGS))
        self.assertEqual(metadata.find("./plate/metadata[@key='filament_volume_maps']").get("value"), "1 3 0 0")

    def test_invalid_cut_index_is_rejected(self):
        for path, label in ((self.a, "A"), (self.b, "B")):
            write_project(path, ["R", "G"], ["0", "0"], ["", ""], ["", ""], label)
        update_project(self.a, {"Metadata/cut_information.xml":
            b'<objects><object id="2"><cut_id id="0" check_sum="1" connectors_cnt="0"/></object></objects>'})
        with self.assertRaisesRegex(ValueError, "Invalid cut-information object index"):
            self.merge()
        self.assertFalse(self.out.exists())

    def test_rejects_invalid_inputs_without_creating_output(self):
        write_project(self.a, ["R", "G"], ["0", "0"], ["", ""], ["", ""], "A")
        write_project(self.b, ["C", "mixB"], ["0", "1"], ["", "0,1"], ["", "0.5,0.5"], "B")
        with self.assertRaisesRegex(ValueError, "valid base"):
            self.merge()
        self.assertFalse(self.out.exists())
        write_project(self.b, ["C", "Y"], ["0", "0"], ["", ""], ["", ""], "B")
        with zipfile.ZipFile(self.b) as archive:
            entries = {n: archive.read(n) for n in archive.namelist()}
        settings = json.loads(entries[merger.SETTINGS])
        settings["nozzle_diameter"] = ["0.6"]
        entries[merger.SETTINGS] = json.dumps(settings).encode()
        with zipfile.ZipFile(self.b, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, "Incompatible printer"):
            self.merge()
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
