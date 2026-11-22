import contextlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import merge_3mf as merger


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

    def test_split_tree_and_extended_states(self):
        # Four leaves, read right-to-left: slot 1, unpainted, slot 2, slot 3.
        mapping = tuple([0, 19, 2, 34] + list(range(4, 35)))
        self.assertEqual(merger.remap_paint("0C8043", mapping), "1FFC801FC3")
        with self.assertRaisesRegex(ValueError, "Truncated"):
            merger.remap_paint("C", mapping)

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
