"""Merge selected base filament slots in a Bambu Studio 3MF project.

Usage: python merge_3mf_colors.py input.3mf [output.3mf ["1,2;4,5"]]
Without slot sets, display the input's filament report without writing a file.
"""

import argparse
import copy
import json
import posixpath
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

from filament_report import format_filament_report
from merge_3mf import (MODEL_SETTINGS, PAINT, SELECTORS, SETTINGS, SLOT_KEYS, Project,
                       palette, remap_model_paint, remap_paint, require, selector, xml_bytes)


def parse_sets(text, base_slots):
    groups, seen = [], set()
    for item in text.split(";"):
        parts = item.split(",")
        require(len(parts) >= 2 and all(re.fullmatch(r"[0-9]+", p.strip()) for p in parts),
                "Each set must contain at least two comma-separated base slot numbers")
        group = [int(p.strip()) for p in parts]
        for slot in group:
            require(slot in base_slots, "Invalid base filament slot: %d" % slot)
            require(slot not in seen, "Slot %d appears more than once" % slot)
            seen.add(slot)
        groups.append(group)
    return groups


def merge_palette(settings, groups):
    bases, mixes = palette(settings)
    count = len(settings["filament_colour"])
    winners = {slot: group[0] for group in groups for slot in group}
    anchors = {min(group): group[0] for group in groups}
    base_order = [anchors[slot] if slot in anchors else slot
                  for slot in (i + 1 for i in bases)
                  if slot not in winners or slot in anchors]
    mapping = [0] * (count + 1)
    for new, old in enumerate(base_order, 1):
        mapping[old] = new
    for old, winner in winners.items():
        mapping[old] = mapping[winner]

    recipes, ratios, mixed_order, warnings = {}, {}, [], []
    for i in mixes:
        old = i + 1
        ingredients = [mapping[int(value)] for value in settings["filament_mixed_components"][i].split(",")]
        weights = [Decimal(value) for value in settings["filament_mixed_sublayer_ratios"][i].split(",")]
        combined = {}
        for slot, weight in zip(ingredients, weights):
            combined[slot] = combined.get(slot, Decimal(0)) + weight
        if len(combined) == 1:
            mapping[old] = next(iter(combined))
            warnings.append("Mixed slot %d now uses only one base filament and was replaced by slot %d." %
                            (old, mapping[old]))
            continue
        require(all(weight > 0 for weight in combined.values()),
                "Mixed slot %d has a nonpositive ratio; correct its recipe before merging" % old)
        mixed_order.append(old)
        recipes[old] = ",".join(str(slot) for slot in combined)
        if len(combined) < len(ingredients):
            ratios[old] = ",".join(format(value, "f") for value in combined.values())
            warnings.append("Mixed slot %d had duplicate ingredients combined; sublayer order may change." % old)
        else:
            ratios[old] = settings["filament_mixed_sublayer_ratios"][i]
    order = base_order + mixed_order
    for new, old in enumerate(order, 1):
        mapping[old] = new

    result, setting_warnings = select_settings(settings, order, mapping, groups)
    result["filament_is_mixed"] = ["0"] * len(base_order) + ["1"] * len(mixed_order)
    result["filament_mixed_components"] = [""] * len(base_order) + [recipes[slot] for slot in mixed_order]
    result["filament_mixed_sublayer_ratios"] = [""] * len(base_order) + [ratios[slot] for slot in mixed_order]
    palette(result)
    return result, tuple(mapping), order, warnings + setting_warnings


def select_settings(settings, order, mapping, groups):
    count = len(settings["filament_colour"])
    result = copy.deepcopy(settings)
    keys = SLOT_KEYS | {k for k, v in settings.items() if k.startswith("filament_") and isinstance(v, list)}
    differences = {tuple(group): set() for group in groups}
    for key in sorted(keys):
        if key not in settings:
            continue
        values = settings[key]
        require(isinstance(values, list), "Invalid per-filament setting: " + key)
        if key in ("filament_volume_map", "filament_nozzle_map"):
            require(len(values) >= count, "Short filament map: " + key)
            values = values[:count]
        require(len(values) > 0 and len(values) % count == 0,
                "Unsupported per-filament array layout: " + key)
        width = len(values) // count
        def chunk(slot):
            return values[(slot - 1) * width:slot * width]
        if key not in ("filament_self_index", "filament_mixed_components", "filament_mixed_sublayer_ratios"):
            for group in groups:
                if any(chunk(slot) != chunk(group[0]) for slot in group[1:]):
                    differences[tuple(group)].add(key)
        result[key] = []
        for new, old in enumerate(order, 1):
            result[key].extend([str(new)] * width if key == "filament_self_index" else chunk(old))

    for key in SELECTORS:
        if key in result:
            result[key] = selector(result[key], mapping, key)
    for key in ("different_settings_to_system", "inherits_group", "compatible_machine_expression_group"):
        if key in settings:
            values = settings[key]
            require(isinstance(values, list) and len(values) == count + 2,
                    "Unsupported inheritance metadata: " + key)
            for group in groups:
                if any(values[slot] != values[group[0]] for slot in group[1:]):
                    differences[tuple(group)].add(key)
            result[key] = [values[0]] + [values[slot] for slot in order] + [values[-1]]
    if "compatible_process_expression_group" in settings:
        values = settings["compatible_process_expression_group"]
        require(isinstance(values, list) and len(values) == count,
                "Unsupported compatibility metadata: compatible_process_expression_group")
        for group in groups:
            if any(values[slot - 1] != values[group[0] - 1] for slot in group[1:]):
                differences[tuple(group)].add("compatible_process_expression_group")
        result["compatible_process_expression_group"] = [values[slot - 1] for slot in order]
    if "flush_volumes_matrix" in settings:
        matrix = settings["flush_volumes_matrix"]
        require(isinstance(matrix, list) and len(matrix) == count * count, "Invalid purge-volume matrix")
        result["flush_volumes_matrix"] = [matrix[(a - 1) * count + b - 1] for a in order for b in order]
    warnings = []
    for group, keys in differences.items():
        if keys:
            warnings.append("Slots %s differ in %s; retained settings from slot %d." %
                            (",".join(str(s) for s in group), ", ".join(sorted(keys)), group[0]))
    return result, warnings


def rewrite_metadata(project, mapping, order, groups):
    count = len(mapping) - 1
    maps = {"filament_maps": "filament_map", "filament_volume_maps": "filament_volume_map",
            "filament_nozzle_maps": "filament_nozzle_map"}
    for plate in project.metadata.findall("plate"):
        for node in plate.findall("./metadata[@key='gcode_file']"):
            plate.remove(node)
    for node in project.metadata.iter("metadata"):
        key, value = node.get("key"), node.get("value")
        if key in SELECTORS:
            node.set("value", selector(value, mapping, key))
        elif key in maps:
            old = value.split()
            if not old:
                continue
            if len(old) != count:
                action = "missing values use the global map" if len(old) < count else "extra values are ignored"
                print("Warning: %s has %d %s entries for %d filaments; %s." %
                      (project.path, len(old), key, count, action), file=sys.stderr)
            global_map = project.settings.get(maps[key])
            require(isinstance(global_map, list) and len(global_map) >= count,
                    "Missing global mapping needed for " + key)
            old = (old + [str(v) for v in global_map[len(old):count]])[:count]
            for group in groups:
                if any(old[slot - 1] != old[group[0] - 1] for slot in group[1:]):
                    print("Warning: plate %s differs for slots %s; retained slot %d's mapping." %
                          (key, ",".join(str(slot) for slot in group), group[0]), file=sys.stderr)
            node.set("value", " ".join(old[slot - 1] for slot in order))
    return xml_bytes(project.metadata)


def is_slice_cache(name):
    return (name.lower().endswith((".gcode", ".gcode.md5", ".gcode.json")) or
            bool(re.fullmatch(r"Metadata/plate_[^/]+\.json", name)) or
            name in ("Metadata/slice_info.config", "Metadata/filament_sequence.json"))


def write_project(project, output, settings, mapping, order, groups):
    removed = {name for name in project.files if is_slice_cache(name)}
    files = {name: data for name, data in project.files.items() if name not in removed}
    for name in list(files):
        if not name.endswith(".rels"):
            continue
        root = ET.fromstring(files[name])
        directory = posixpath.dirname(posixpath.dirname(name))
        changed = False
        for node in list(root):
            target = node.get("Target", "")
            if node.get("TargetMode") == "External":
                continue
            path = unquote(urlsplit(target).path)
            path = path.lstrip("/") if path.startswith("/") else posixpath.normpath(posixpath.join(directory, path))
            if path in removed:
                root.remove(node)
                changed = True
        if changed:
            files[name] = xml_bytes(root)
    for name in project.models:
        files[name] = remap_model_paint(files[name], mapping, name)
        for match in PAINT.finditer(files[name]):
            remap_paint(match.group(1).decode("ascii").upper(), tuple(range(len(order) + 1)))
    files[SETTINGS] = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    files[MODEL_SETTINGS] = rewrite_metadata(project, mapping, order, groups)
    report = format_filament_report(settings, output, project.metadata)
    for name in files:
        if re.fullmatch(r"Metadata/(process_settings|print_setting)_[^/]+\.config", name):
            preset = json.loads(files[name].decode("utf-8-sig"))
            for key in SELECTORS:
                if key in preset:
                    preset[key] = selector(preset[key], mapping, name + ": " + key)
            files[name] = json.dumps(preset, ensure_ascii=False, indent=2).encode("utf-8")
    files["Metadata/filament_sequence.json"] = json.dumps({
        "plate_%d" % index: {"nozzle_sequence": [], "optimal_assignment": [], "sequence": []}
        for index in range(1, len(project.metadata.findall("plate")) + 1)
    }).encode("utf-8")
    types = ET.fromstring(files["[Content_Types].xml"])
    for node in list(types):
        if node.get("PartName") and node.get("PartName").lstrip("/") not in files:
            types.remove(node)
    ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/content-types")
    files["[Content_Types].xml"] = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(types, encoding="utf-8")
    require(not output.exists(), "Output already exists; choose a new filename")
    with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp",
                                     dir=str(output.parent), delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with zipfile.ZipFile(temp_path) as archive:
            require(archive.testzip() is None, "Output ZIP integrity check failed")
        require(not output.exists(), "Output appeared during merge; refusing to replace it")
        temp_path.rename(output)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("sets", nargs="?")
    args = parser.parse_args()
    try:
        if args.sets is None:
            with zipfile.ZipFile(args.input) as archive:
                settings = json.loads(archive.read(SETTINGS).decode("utf-8-sig"))
                metadata = ET.fromstring(archive.read(MODEL_SETTINGS))
            palette(settings)
            print(format_filament_report(settings, args.input, metadata))
            return
        require(args.output.resolve() != args.input.resolve(), "Output must not overwrite the input")
        require(not args.output.exists(), "Output already exists; choose a new filename")
        project = Project(args.input)
        bases, _ = palette(project.settings)
        groups = parse_sets(args.sets, {i + 1 for i in bases})
        settings, mapping, order, warnings = merge_palette(project.settings, groups)
        report = write_project(project, args.output, settings, mapping, order, groups)
        for warning in warnings:
            print("Warning: " + warning, file=sys.stderr)
        print("Created %s" % args.output)
        print("Original slots: " + ", ".join("%d->%d" % (i, mapping[i]) for i in range(1, len(mapping))))
        print("Review filament settings, mixed recipes and purging volumes in Bambu Studio before printing.")
        print(report)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        parser.exit(1, "Merge failed: %s\n" % exc)


if __name__ == "__main__":
    main()
