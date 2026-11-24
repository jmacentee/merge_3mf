"""Merge two Bambu Studio 3MF projects without deduplicating filaments.

Usage: python merge_3mf.py inputA.3mf inputB.3mf output.3mf
Python 3.7+, standard library only. Never launches a slicer.
"""

import argparse
import copy
import json
import math
import posixpath
import re
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

from filament_report import format_filament_report


CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
ROOT_MODEL = "3D/3dmodel.model"
SETTINGS = "Metadata/project_settings.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
SELECTORS = {
    "extruder", "wall_filament", "solid_infill_filament", "sparse_infill_filament",
    "support_filament", "support_interface_filament",
}
PAINT = re.compile(rb'\bpaint_color=["\']([0-9A-Fa-f]+)["\']')
# Explicit setting names avoid mistaking a printer array for a filament array
# when, for example, a project contains just one or two filaments.
SLOT_KEYS = set("""
activate_air_filtration additional_cooling_fan_speed additional_fan_full_speed_layer
chamber_temperatures circle_compensation_speed close_additional_fan_first_x_layers
close_fan_the_first_x_layers close_optional_fan_first_x_layers complete_print_exhaust_fan_speed
cool_plate_temp cool_plate_temp_initial_layer cooling_perimeter_transition_distance
cooling_slowdown_logic counter_coef_1 counter_coef_2 counter_coef_3 counter_limit_max
counter_limit_min default_filament_colour diameter_limit during_print_exhaust_fan_speed
enable_overhang_bridge_fan enable_pressure_advance eng_plate_temp eng_plate_temp_initial_layer
fan_cooling_layer_time fan_max_speed fan_min_speed fins_extrude_safe_temp
first_x_layer_fan_speed first_x_layer_optional_fan_speed first_x_layer_part_fan_speed
full_fan_speed_layer hole_coef_1 hole_coef_2 hole_coef_3 hole_limit_max hole_limit_min
hot_plate_temp hot_plate_temp_initial_layer impact_strength_z ironing_fan_speed
long_retractions_when_ec min_temperature_when_ec no_slow_down_for_cooling_on_outwalls
nozzle_temperature nozzle_temperature_initial_layer nozzle_temperature_range_high
nozzle_temperature_range_low optional_cooling_fan_speed optional_fan_full_speed_layer
overhang_fan_speed overhang_fan_threshold overhang_threshold_participating_cooling
override_process_overhang_speed pre_start_fan_time pressure_advance reduce_fan_stop_start_freq
required_nozzle_HRC retraction_distances_when_ec slow_down_for_layer_cooling
slow_down_layer_time slow_down_min_speed supertack_plate_temp supertack_plate_temp_initial_layer
temperature_vitrification textured_plate_temp textured_plate_temp_initial_layer
volumetric_speed_coefficients flush_volumes_vector
""".split())
PRINTER_KEYS = (
    "printer_model", "printer_variant", "printer_technology", "printer_structure",
    "nozzle_diameter", "printable_area", "printable_height", "extruder_offset",
    "extruder_variant_list", "printer_extruder_id", "printer_extruder_variant",
    "extruder_nozzle_count", "extruder_nozzle_diameter", "extruder_nozzle_volume_type",
)
UNSUPPORTED_METADATA = {
    "Metadata/layer_heights_profile.txt", "Metadata/layer_config_ranges.xml",
    "Metadata/custom_gcode_per_layer.xml", "Metadata/brim_ear_points.txt",
    "Metadata/variable_layer_height.txt",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def xml_bytes(root):
    namespace = REL if root.tag == "{%s}Relationships" % REL else CORE
    ET.register_namespace("", namespace)
    ET.register_namespace("p", PROD)
    if root.tag == "{%s}model" % CORE:
        root.set("xmlns:BambuStudio", "http://schemas.bambulab.com/package/2021")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def selector(value, mapping, context):
    number = int(value)
    require(0 <= number < len(mapping), "Invalid filament reference in " + context)
    return str(mapping[number])


def palette(settings):
    colors = settings.get("filament_colour")
    require(isinstance(colors, list) and colors, "Missing filament_colour palette")
    count = len(colors)
    flags = settings.get("filament_is_mixed", ["0"] * count)
    require(len(flags) == count and all(str(x) in ("0", "1") for x in flags),
            "Invalid filament_is_mixed flags")
    recipes = settings.get("filament_mixed_components", [""] * count)
    ratios = settings.get("filament_mixed_sublayer_ratios", [""] * count)
    require(len(recipes) == len(ratios) == count, "Mixed recipe arrays have incorrect lengths")
    for i, flag in enumerate(flags):
        if str(flag) == "0":
            require(not recipes[i] and not ratios[i], "Base filament has unexpected mixed recipe")
            continue
        ingredients = recipes[i].split(",")
        require(len(ingredients) >= 2, "Mixed filament %d has no valid recipe" % (i + 1))
        for ingredient in ingredients:
            require(ingredient.strip().isdigit(), "Invalid mixed ingredient")
            slot = int(ingredient)
            require(1 <= slot <= count and str(flags[slot - 1]) == "0",
                    "Mixed ingredients must reference valid base filaments")
        weights = [float(x) for x in ratios[i].split(",")]
        require(len(weights) == len(ingredients) and
                all(math.isfinite(x) and 0 <= x <= 1 for x in weights) and
                abs(sum(weights) - 1) <= 0.001,
                "Invalid mixing ratios for slot %d" % (i + 1))
    return [i for i, x in enumerate(flags) if str(x) == "0"], [
        i for i, x in enumerate(flags) if str(x) == "1"]


def merge_palettes(a, b):
    require(a.get("printer_model") and b.get("printer_model"),
            "Both projects must identify their printer model")
    for key in PRINTER_KEYS:
        require(a.get(key) == b.get(key), "Incompatible printer configurations: " + key)
    bases_a, mixes_a = palette(a)
    bases_b, mixes_b = palette(b)
    configs = tuple(copy.deepcopy(c) for c in (a, b))
    counts = [len(c["filament_colour"]) for c in configs]
    for c, count in zip(configs, counts):
        c.setdefault("filament_is_mixed", ["0"] * count)
        c.setdefault("filament_mixed_components", [""] * count)
        c.setdefault("filament_mixed_sublayer_ratios", [""] * count)
    a, b = configs
    order = ([(0, i) for i in bases_a] + [(1, i) for i in bases_b] +
             [(0, i) for i in mixes_a] + [(1, i) for i in mixes_b])
    require(len(order) <= 127, "Combined palette exceeds the supported 127 paint states")
    maps = [[0] * (n + 1) for n in counts]
    for new, (source, old) in enumerate(order, 1):
        maps[source][old + 1] = new
    result = copy.deepcopy(a)
    keys = SLOT_KEYS | {
        k for c in configs for k, v in c.items() if k.startswith("filament_") and isinstance(v, list)
    }
    for key in sorted(keys):
        if key not in a and key not in b:
            continue
        values = [c.get(key) for c in configs]
        require(all(isinstance(v, list) for v in values),
                "Both inputs need compatible per-filament setting: " + key)
        if key in ("filament_volume_map", "filament_nozzle_map"):
            for source in range(2):
                require(len(values[source]) >= counts[source], "Short filament map: " + key)
                values[source] = values[source][:counts[source]]
        widths = [len(v) // n for v, n in zip(values, counts)]
        require(all(len(v) % n == 0 for v, n in zip(values, counts)) and
                widths[0] == widths[1] and widths[0] > 0,
                "Incompatible per-filament array layout: " + key)
        width = widths[0]
        result[key] = []
        for source, old in order:
            chunk = values[source][old * width:(old + 1) * width]
            if key == "filament_self_index":
                chunk = [str(maps[source][old + 1])] * width
            result[key].extend(chunk)
    result["filament_is_mixed"] = ["0"] * (len(bases_a) + len(bases_b)) + ["1"] * (
        len(mixes_a) + len(mixes_b))
    result["filament_mixed_components"] = []
    result["filament_mixed_sublayer_ratios"] = []
    for source, old in order:
        recipe = configs[source].get("filament_mixed_components", [""] * counts[source])[old]
        result["filament_mixed_components"].append(",".join(
            selector(x, maps[source], "mixed components") for x in recipe.split(",")) if recipe else "")
        result["filament_mixed_sublayer_ratios"].append(
            configs[source].get("filament_mixed_sublayer_ratios", [""] * counts[source])[old])
    for key in SELECTORS - {"extruder"}:
        if key in result:
            result[key] = selector(result[key], maps[0], key)
    for key in ("different_settings_to_system", "inherits_group"):
        if key in a or key in b:
            values = [c.get(key, [""] * (n + 2)) for c, n in zip(configs, counts)]
            require(all(isinstance(v, list) and len(v) == n + 2 for v, n in zip(values, counts)),
                    "Unsupported inheritance metadata: " + key)
            result[key] = [values[0][0]] + [values[s][i + 1] for s, i in order] + [values[0][-1]]
    if "flush_volumes_matrix" in a or "flush_volumes_matrix" in b:
        matrices = [c.get("flush_volumes_matrix") for c in configs]
        require(all(isinstance(v, list) and len(v) == n * n for v, n in zip(matrices, counts)),
                "Invalid purge-volume matrix")
        result["flush_volumes_matrix"] = [
            matrices[s][i * counts[s] + j] if s == t else "280"
            for s, i in order for t, j in order]
    palette(result)
    return result, [tuple(x) for x in maps], order


def encoded_leaf(state):
    require(0 <= state <= 127, "Unsupported paint state %d" % state)
    if state < 3:
        return [state * 4]
    value = state - 3
    return [12] + [15] * (value // 15) + [value % 15]


@lru_cache(maxsize=65536)
def remap_paint(encoded, mapping):
    digits = [int(c, 16) for c in encoded[::-1]]
    out, pos, pending = [], 0, 1
    while pending:
        require(pos < len(digits), "Truncated paint tree: " + encoded)
        code = digits[pos]
        pos += 1
        pending -= 1
        split = code & 3
        if split:
            out.append(code)
            pending += split + 1
        else:
            state = code >> 2
            if state == 3:
                while True:
                    require(pos < len(digits), "Truncated extended paint state")
                    extra = digits[pos]
                    pos += 1
                    state += extra
                    if extra != 15:
                        break
            require(state < len(mapping), "Paint references an unknown filament slot")
            out.extend(encoded_leaf(mapping[state]))
    require(pos == len(digits), "Extra digits in paint tree")
    return "".join(format(n, "X") for n in reversed(out))


class Project:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(path) as archive:
            require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP entry")
            require(archive.testzip() is None, "Corrupted input ZIP: " + str(path))
            self.files = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
        require(not (set(self.files) & UNSUPPORTED_METADATA),
                "Layer-specific G-code/settings are not supported; remove them in a copy first")
        require(ROOT_MODEL in self.files and SETTINGS in self.files and MODEL_SETTINGS in self.files,
                "Input must be a Bambu Studio project, not a mesh-only or sliced-only 3MF")
        self.settings = json.loads(self.files[SETTINGS].decode("utf-8-sig"))
        self.models = {n: ET.fromstring(data) for n, data in self.files.items() if n.endswith(".model")}
        self.metadata = ET.fromstring(self.files[MODEL_SETTINGS])
        self.ids = {}
        self.names = {}
        for name, root in self.models.items():
            require(root.tag == "{%s}model" % CORE, "Unsupported model XML namespace")
            require(root.get("unit", "millimeter") == "millimeter", "Only millimeter models are supported")
            resources = root.find("{%s}resources" % CORE)
            require(resources is not None, "Missing model resources")
            for obj in resources:
                require(obj.tag == "{%s}object" % CORE,
                        "Material/texture resources are not supported; use Bambu filament paint")
                key = (name, obj.get("id"))
                require(key not in self.ids and key[1] is not None, "Duplicate or missing object ID")
                self.ids[key] = None
        self.uuids = {}

    def target(self, current_file, element):
        raw = element.get("{%s}path" % PROD)
        name = current_file if not raw else (
            raw.lstrip("/") if raw.startswith("/") else posixpath.normpath(
                posixpath.join(posixpath.dirname(current_file), raw)))
        key = (name, element.get("objectid"))
        require(key in self.ids, "Unresolved model reference: " + repr(key))
        return key

    def leaves(self, key, ancestors=()):
        require(key not in ancestors, "Cyclic model components")
        root = self.models[key[0]]
        obj = next(o for o in root.find("{%s}resources" % CORE) if o.get("id") == key[1])
        if obj.find("{%s}mesh" % CORE) is not None:
            return [key]
        components = obj.findall("./{%s}components/{%s}component" % (CORE, CORE))
        require(components, "Object has neither mesh nor components")
        return [leaf for c in components for leaf in self.leaves(self.target(key[0], c), ancestors + (key,))]

    def prepare(self, source, start, occupied=()):
        self.source = source
        for offset, key in enumerate(self.ids, start):
            self.ids[key] = str(offset)
        prefix = "inputB/"
        index = 2
        while source == 1 and any(prefix + name in occupied for name in self.files):
            prefix = "inputB%d/" % index
            index += 1
        for name in self.files:
            self.names[name] = name if source == 0 else prefix + name
        # B's root objects are incorporated into A's root model.
        self.names[ROOT_MODEL] = ROOT_MODEL
        return start + len(self.ids)

    def rewrite_uuid(self, value, context):
        if value not in self.uuids:
            self.uuids[value] = str(uuid.uuid5(uuid.NAMESPACE_URL, "%s:%s:%s:%s" % (
                self.source, self.path.resolve(), context, value)))
        return self.uuids[value]

    def rewrite_model(self, name, mapping):
        root = ET.fromstring(self.files[name])
        for obj in root.find("{%s}resources" % CORE):
            obj.set("id", self.ids[(name, obj.get("id"))])
        for node in root.iter():
            if node.tag in ("{%s}component" % CORE, "{%s}item" % CORE):
                key = self.target(name, node)
                node.set("objectid", self.ids[key])
                if key[0] != name or node.get("{%s}path" % PROD) is not None:
                    node.set("{%s}path" % PROD, "/" + self.names[key[0]])
            for attr in list(node.attrib):
                if attr.lower().endswith("uuid"):
                    node.set(attr, self.rewrite_uuid(node.get(attr), self.names[name]))
        # Patch the original text instead of serializing millions of mesh nodes.
        # Vertices, triangle indices, support/seam paint, and transforms stay intact.
        data = self.files[name]
        pattern = rb"<(?:object|component|item|build)\b[^>]*>"
        nodes = iter(n for n in root.iter() if n.tag in {
            "{%s}%s" % (CORE, tag) for tag in ("object", "component", "item", "build")})
        tag_count = sum(1 for n in root.iter() if n.tag in {
            "{%s}%s" % (CORE, tag) for tag in ("object", "component", "item", "build")})
        require(len(re.findall(pattern, data)) == tag_count,
                "Unsupported XML prefixes on model tags: " + name)
        def replace_tag(match):
            node = next(nodes)
            attrs = dict(node.attrib)
            def replace_attr(attr):
                label = attr.group(1).decode("utf-8")
                expanded = "{%s}%s" % (PROD, label[2:]) if label.startswith("p:") else label
                if expanded not in attrs:
                    return attr.group(0)
                value = attrs.pop(expanded)
                escaped = value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
                return attr.group(1) + b'="' + escaped.encode("utf-8") + b'"'
            updated = re.sub(rb'([\w:]+)\s*=\s*["\']([^"\']*)["\']', replace_attr, match.group(0))
            if "{%s}path" % PROD in attrs:
                updated = updated.replace(b"/>", b' p:path="' + attrs["{%s}path" % PROD].encode() + b'"/>')
            return updated
        data = re.sub(pattern, replace_tag, data)
        data = PAINT.sub(lambda match: b'paint_color="' + remap_paint(
            match.group(1).decode("ascii").upper(), mapping).encode("ascii") + b'"', data)
        original_meshes = re.findall(rb"<mesh\b[^>]*>.*?</mesh>", self.files[name], re.DOTALL)
        new_meshes = re.findall(rb"<mesh\b[^>]*>.*?</mesh>", data, re.DOTALL)
        expected_meshes = [PAINT.sub(lambda match: b'paint_color="' + remap_paint(
            match.group(1).decode("ascii").upper(), mapping).encode("ascii") + b'"', mesh)
            for mesh in original_meshes]
        require(expected_meshes == new_meshes, "Geometry or paint changed unexpectedly: " + name)
        return data

    def rewrite_metadata(self, mapping, combined_settings):
        for obj in self.metadata.findall("object"):
            old_id = obj.get("id")
            key = (ROOT_MODEL, old_id)
            require(key in self.ids, "Object settings reference an unknown root object")
            leaves = self.leaves(key)
            old_ids = [leaf[1] for leaf in leaves]
            require(len(old_ids) == len(set(old_ids)), "Ambiguous part IDs within a source object")
            parts = {leaf[1]: self.ids[leaf] for leaf in leaves}
            obj.set("id", self.ids[key])
            default_extruder = obj.find("./metadata[@key='extruder']")
            if default_extruder is None:
                ET.SubElement(obj, "metadata", {"key": "extruder", "value": "1"})
            elif default_extruder.get("value") == "0":
                default_extruder.set("value", "1")
            for part in obj.findall("part"):
                require(part.get("id") in parts, "Part settings reference an unknown mesh")
                part.set("id", parts[part.get("id")])
                shared = part.find("./metadata[@key='mesh_shared']")
                if shared is not None and shared.get("value") != "-1":
                    old_shared = shared.get("value")
                    candidates = [k for k in self.ids if k[1] == old_shared and
                                  self.models[k[0]].find(
                                      "./{%s}resources/{%s}object[@id='%s']/{%s}mesh" % (
                                          CORE, CORE, old_shared, CORE)) is not None]
                    require(len(candidates) == 1, "Ambiguous shared-mesh reference")
                    shared.set("value", self.ids[candidates[0]])
        for node in self.metadata.iter():
            if node.tag == "metadata":
                key = node.get("key")
                if key in SELECTORS:
                    node.set("value", selector(node.get("value"), mapping, key))
                elif key == "object_id":
                    node.set("value", self.ids[(ROOT_MODEL, node.get("value"))])
                elif key in ("thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file"):
                    value = node.get("value")
                    if value in self.names:
                        node.set("value", self.names[value])
                elif key in ("filament_maps", "filament_volume_maps"):
                    old = node.get("value").split()
                    setting = "filament_map" if key == "filament_maps" else "filament_volume_map"
                    values = list(combined_settings[setting][:len(combined_settings["filament_colour"])])
                    count = len(mapping) - 1
                    if old and len(old) != count:
                        action = ("missing entries use the project's global map" if len(old) < count
                                  else "extra entries are ignored")
                        print("Warning: %s has %d %s entries for %d filaments; %s." %
                              (self.path, len(old), key, count, action), file=sys.stderr)
                    for slot, value in enumerate(old[:count], 1):
                        values[mapping[slot] - 1] = value
                    node.set("value", " ".join(values))
            elif node.tag == "assemble_item":
                node.set("object_id", self.ids[(ROOT_MODEL, node.get("object_id"))])
            for attr in list(node.attrib):
                if attr.lower().endswith("uuid"):
                    node.set(attr, self.rewrite_uuid(node.get(attr), "metadata"))
def combine_metadata(projects, maps):
    result = projects[0].metadata
    other = projects[1].metadata
    for obj in other.findall("object"):
        result.insert(len(result.findall("object")), obj)
    plates_a = result.findall("plate")
    plates_b = other.findall("plate")
    for index, plate in enumerate(plates_b):
        if index < len(plates_a):
            target = plates_a[index]
            for key in ("filament_maps", "filament_volume_maps"):
                a_node = target.find("./metadata[@key='%s']" % key)
                b_node = plate.find("./metadata[@key='%s']" % key)
                require((a_node is None) == (b_node is None), "Incompatible plate maps: " + key)
                if a_node is not None:
                    a_values, b_values = a_node.get("value").split(), b_node.get("value").split()
                    for slot in maps[1][1:]:
                        a_values[slot - 1] = b_values[slot - 1]
                    a_node.set("value", " ".join(a_values))
            for instance in plate.findall("model_instance"):
                target.append(instance)
        else:
            result.append(plate)
    identifier = 1
    for index, plate in enumerate(result.findall("plate"), 1):
        node = plate.find("./metadata[@key='plater_id']")
        require(node is not None, "Missing plate ID")
        node.set("value", str(index))
        for node in plate.findall("./model_instance/metadata[@key='identify_id']"):
            node.set("value", str(identifier))
            identifier += 1
    assemble = result.find("assemble")
    other_assemble = other.find("assemble")
    if other_assemble is not None:
        if assemble is None:
            result.append(other_assemble)
        else:
            assemble.extend(list(other_assemble))
    require(all(child.tag in ("object", "plate", "assemble") for child in other),
            "Unsupported additional model settings in input B")
    return xml_bytes(result)


def relationships(targets):
    root = ET.Element("{%s}Relationships" % REL)
    for index, target in enumerate(targets, 1):
        ET.SubElement(root, "{%s}Relationship" % REL, {
            "Id": "rel-%d" % index, "Target": "/" + target,
            "Type": "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
        })
    return xml_bytes(root)


def content_types(projects, files):
    namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    root = ET.fromstring(projects[0].files["[Content_Types].xml"])
    declarations = {(node.tag, node.get("Extension") or node.get("PartName")): node for node in root}
    for node in ET.fromstring(projects[1].files["[Content_Types].xml"]):
        if node.get("PartName"):
            original = node.get("PartName").lstrip("/")
            require(original in projects[1].names, "Content type references an unknown file")
            node.set("PartName", "/" + projects[1].names[original])
        key = (node.tag, node.get("Extension") or node.get("PartName"))
        if key in declarations:
            require(node.get("ContentType") == declarations[key].get("ContentType"),
                    "Incompatible content type declaration")
        else:
            root.append(node)
            declarations[key] = node
    for node in list(root):
        if node.get("PartName") and node.get("PartName").lstrip("/") not in files:
            root.remove(node)
    ET.register_namespace("", namespace)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def validate_output(files, count):
    roots = {name: ET.fromstring(data) for name, data in files.items() if name.endswith(".model")}
    ids, meshes = {}, set()
    for name, root in roots.items():
        for obj in root.find("{%s}resources" % CORE):
            number = obj.get("id")
            require(number not in ids, "Output object IDs collide across mesh files")
            ids[number] = name
            if obj.find("{%s}mesh" % CORE) is not None:
                meshes.add(number)
        for node in root.iter():
            if node.tag in ("{%s}item" % CORE, "{%s}component" % CORE):
                raw = node.get("{%s}path" % PROD)
                target = raw.lstrip("/") if raw else name
                require(target in roots, "Output mesh path does not resolve")
                objects = roots[target].find("{%s}resources" % CORE)
                require(any(x.get("id") == node.get("objectid") for x in objects),
                        "Output object reference does not resolve")
    require(meshes, "Output contains no meshes")
    identity = tuple(range(count + 1))
    for name in roots:
        for match in PAINT.finditer(files[name]):
            remap_paint(match.group(1).decode("ascii").upper(), identity)
    metadata = ET.fromstring(files[MODEL_SETTINGS])
    for obj in metadata.findall("object"):
        require(obj.get("id") in ids, "Output model settings reference an unknown object")
        for part in obj.findall("part"):
            require(part.get("id") in meshes, "Output part settings reference an unknown mesh")


def merge(input_a, input_b, output):
    output = Path(output)
    require(output.resolve() not in {Path(input_a).resolve(), Path(input_b).resolve()},
            "Output must not overwrite an input")
    require(not output.exists(), "Output already exists; choose a new filename")
    projects = [Project(input_a), Project(input_b)]
    settings, maps, order = merge_palettes(*(p.settings for p in projects))
    report = format_filament_report(settings)
    next_id = 1
    for source, project in enumerate(projects):
        next_id = project.prepare(source, next_id, projects[0].files)
    files = {}
    rewritten = []
    for source, project in enumerate(projects):
        model_data = {name: project.rewrite_model(name, maps[source]) for name in project.models}
        rewritten.append(model_data)
        project.rewrite_metadata(maps[source], settings)
        for name, data in project.files.items():
            if name in (SETTINGS, MODEL_SETTINGS, ROOT_MODEL, "[Content_Types].xml", "Metadata/cut_information.xml") or (
                    name.endswith(".rels") or name.endswith(".gcode") or
                    name.startswith("Metadata/plate_") and name.endswith(".json") or
                    name in ("Metadata/slice_info.config", "Metadata/filament_sequence.json")):
                continue
            target = project.names[name]
            require(target not in files, "Archive path collision: " + target)
            files[target] = model_data.get(name, data)
    root_a = ET.fromstring(rewritten[0][ROOT_MODEL])
    root_b = ET.fromstring(rewritten[1][ROOT_MODEL])
    root_a.find("{%s}resources" % CORE).extend(list(root_b.find("{%s}resources" % CORE)))
    build_a, build_b = root_a.find("{%s}build" % CORE), root_b.find("{%s}build" % CORE)
    require(build_a is not None and build_b is not None, "Both projects must have model build items")
    build_a.extend(list(build_b))
    files[ROOT_MODEL] = xml_bytes(root_a)
    files[SETTINGS] = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    files[MODEL_SETTINGS] = combine_metadata(projects, maps)
    cuts = ET.Element("objects")
    object_offset = 0
    for project in projects:
        build = project.models[ROOT_MODEL].find("{%s}build" % CORE)
        object_order = []
        for item in build.findall("{%s}item" % CORE):
            key = project.target(ROOT_MODEL, item)
            if key not in object_order:
                object_order.append(key)
        if "Metadata/cut_information.xml" in project.files:
            cut = ET.fromstring(project.files["Metadata/cut_information.xml"])
            seen = set()
            for obj in cut:
                require(all(int(c.get("connectors_cnt", "0")) == 0 for c in obj.findall("cut_id")),
                        "Models with cut connectors are not supported")
                require(obj.tag == "object", "Unsupported cut-information element")
                index = int(obj.get("id", "0"))
                require(1 <= index <= len(object_order) and index not in seen,
                        "Invalid cut-information object index in %s: %s" % (project.path, obj.get("id")))
                seen.add(index)
                obj.set("id", str(object_offset + index))
                cuts.append(obj)
        object_offset += len(object_order)
    files["Metadata/cut_information.xml"] = xml_bytes(cuts)
    submodels = [name for name in files if name.endswith(".model") and name != ROOT_MODEL]
    files["3D/_rels/3dmodel.model.rels"] = relationships(submodels)
    # Keep A's thumbnail references while replacing only its model relationship.
    root_rels = ET.fromstring(projects[0].files["_rels/.rels"])
    files["_rels/.rels"] = xml_bytes(root_rels)
    files["Metadata/filament_sequence.json"] = json.dumps({
        "plate_%d" % index: {"nozzle_sequence": [], "optimal_assignment": [], "sequence": []}
        for index in range(1, len(projects[0].metadata.findall("plate")) + 1)
    }).encode("utf-8")
    files["[Content_Types].xml"] = content_types(projects, files)
    validate_output(files, len(order))
    temp = output.with_name(output.name + ".tmp")
    require(not temp.exists(), "Temporary output already exists: " + str(temp))
    try:
        with zipfile.ZipFile(temp, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with zipfile.ZipFile(temp) as archive:
            require(archive.testzip() is None, "Output ZIP integrity check failed")
        require(not output.exists(), "Output appeared during merge; refusing to replace it")
        temp.rename(output)
    finally:
        if temp.exists():
            temp.unlink()
    print("Created %s" % output)
    flags = settings["filament_is_mixed"]
    print("%d base filaments; %d mixed filaments. Duplicate base colors retained." % (
        flags.count("0"), flags.count("1")))
    print("Original transforms retained. A's global settings used; review overlap and purging before printing.")
    print("New cross-project purge transitions use 280; recalculate purging volumes in Bambu Studio.")
    print(report)
    print("Consider using merge_3mf_colors.py to merge colors for more efficient printing.")
    print("Note: merge_3mf_colors.py is not available yet.")
    return maps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputA", type=Path)
    parser.add_argument("inputB", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        merge(args.inputA, args.inputB, args.output)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        parser.exit(1, "Merge failed: %s\n" % exc)


if __name__ == "__main__":
    main()
