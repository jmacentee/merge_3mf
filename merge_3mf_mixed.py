"""Merge identical mixed filament slots in a Bambu Studio 3MF project.

Usage: python merge_3mf_mixed.py input.3mf [output.3mf]
Without an output file, display the input's filament report.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from filament_report import format_filament_report, identical_mixed_groups
from merge_3mf import MODEL_SETTINGS, SETTINGS, Project, palette, require, selector
from merge_3mf_colors import select_settings, write_project


def merge_mixed_palette(settings, groups):
    count = len(settings["filament_colour"])
    winners = {slot: group[0] for group in groups for slot in group}
    order = [slot for slot in range(1, count + 1) if slot not in winners or winners[slot] == slot]
    mapping = [0] * (count + 1)
    for new, old in enumerate(order, 1):
        mapping[old] = new
    for old, winner in winners.items():
        mapping[old] = mapping[winner]
    result, warnings = select_settings(settings, order, mapping, groups)
    if "filament_mixed_components" in result:
        result["filament_mixed_components"] = [
            ",".join(selector(value, mapping, "mixed components") for value in recipe.split(","))
            if recipe else "" for recipe in result["filament_mixed_components"]]
    palette(result)
    return result, tuple(mapping), order, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        if args.output is None:
            with zipfile.ZipFile(args.input) as archive:
                settings = json.loads(archive.read(SETTINGS).decode("utf-8-sig"))
                metadata = ET.fromstring(archive.read(MODEL_SETTINGS))
            palette(settings)
            print(format_filament_report(settings, args.input, metadata))
            return
        require(args.output.resolve() != args.input.resolve(), "Output must not overwrite the input")
        require(not args.output.exists(), "Output already exists; choose a new filename")
        project = Project(args.input)
        palette(project.settings)
        groups = identical_mixed_groups(project.settings, project.metadata)
        settings, mapping, order, warnings = merge_mixed_palette(project.settings, groups)
        report = write_project(project, args.output, settings, mapping, order, groups)
        for warning in warnings:
            print("Warning: " + warning, file=sys.stderr)
        print("Created %s" % args.output)
        print("Merged %d duplicate mixed slots in %d groups." %
              (sum(len(group) - 1 for group in groups), len(groups)))
        print("Original slots: " + ", ".join("%d->%d" % (i, mapping[i]) for i in range(1, len(mapping))))
        print("Review mixed recipes and purging volumes in Bambu Studio before printing.")
        print(report)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        parser.exit(1, "Merge failed: %s\n" % exc)


if __name__ == "__main__":
    main()
