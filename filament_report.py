"""Reusable reporting for validated Bambu Studio filament palettes."""

import colorsys
import re
from pathlib import Path


def valid_color(color):
    return isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", color)


def color_name(color):
    if not valid_color(color):
        return "unrecognized color"
    red, green, blue = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    if value < 0.15:
        return "black"
    if saturation < 0.15:
        return "white" if value >= 0.85 else "gray"
    hue *= 360
    if value < 0.55 and 15 <= hue < 50:
        return "brown"
    for boundary, name in (
            (15, "red"), (45, "orange"), (70, "yellow"), (160, "green"),
            (190, "cyan"), (210, "blue/cyan"), (260, "blue"),
            (290, "purple"), (320, "magenta"), (345, "pink/magenta")):
        if hue < boundary:
            return name
    return "red"


def slot_values(settings, key, count, label):
    values = settings.get(key)
    if values is None:
        return [label + " unavailable"] * count
    if not isinstance(values, list) or not values or len(values) % count:
        raise ValueError("Invalid per-filament report field: " + key)
    width = len(values) // count
    result = []
    for i in range(count):
        chunk = values[i * width:(i + 1) * width]
        unique = list(dict.fromkeys(str(value) for value in chunk if value is not None and str(value).strip()))
        result.append(" / ".join(unique) if unique else label + " unavailable")
    return result


def format_filament_report(settings, project_path=None):
    """Return a report for a palette whose recipes have already been validated."""
    colors = settings["filament_colour"]
    count = len(colors)
    flags = settings.get("filament_is_mixed", ["0"] * count)
    recipes = settings.get("filament_mixed_components", [""] * count)
    materials = slot_values(settings, "filament_type", count, "material")
    vendors = slot_values(settings, "filament_vendor", count, "vendor")
    usages = [0] * count
    for flag, recipe in zip(flags, recipes):
        if str(flag) == "1":
            for slot in {int(value) for value in recipe.split(",")}:
                usages[slot - 1] += 1
    lines = ["Filament report (color names are approximate):"]
    for i, color in enumerate(colors):
        if str(flags[i]) == "0":
            lines.append("Slot %d: %s (%s), %s, %s (used in %d mixed filaments)" % (
                i + 1, color_name(color), color, materials[i], vendors[i], usages[i]))
    if project_path is not None:
        groups, unknown = {}, []
        for i, color in enumerate(colors):
            if str(flags[i]) != "0":
                continue
            if valid_color(color):
                groups.setdefault(color.upper(), []).append(i + 1)
            else:
                unknown.append(i + 1)
        duplicates = [group for group in groups.values() if len(group) > 1]
        if unknown:
            lines.append("Cannot compare unrecognized color values in base slots: %s." %
                         ", ".join(str(slot) for slot in unknown))
        if duplicates:
            path = Path(project_path)
            output = path.with_name(path.stem + "_merged_colors" + path.suffix)
            sets = ";".join(",".join(str(slot) for slot in group) for group in duplicates)
            def quote(value):
                return "'" + str(value).replace("'", "''") + "'"
            script = Path(__file__).resolve().with_name("merge_3mf_colors.py")
            lines.append("Suggested PowerShell command (review materials and settings first):")
            lines.append("python -B %s %s %s %s" %
                         (quote(script), quote(path), quote(output), quote(sets)))
            lines.append("Matching colors do not guarantee compatible materials or settings.")
        else:
            lines.append("No identical base filament colors found.")
    return "\n".join(lines)
