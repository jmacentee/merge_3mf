"""Reusable reporting for validated Bambu Studio filament palettes."""

import colorsys
import json
import re
from decimal import Decimal
from pathlib import Path


# Names of per-filament settings that do not start with "filament_".
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


def identical_mixed_groups(settings, metadata):
    """Find mixed slots with equal ordered recipes, settings, plate maps and purge transitions."""
    count = len(settings["filament_colour"])
    flags = settings.get("filament_is_mixed", ["0"] * count)
    mixed = [i for i, flag in enumerate(flags) if str(flag) == "1"]
    if not mixed:
        return []
    keys = SLOT_KEYS | {k for k, v in settings.items() if k.startswith("filament_") and isinstance(v, list)}
    fields = []
    for key in sorted(keys - {"filament_self_index", "filament_mixed_components",
                              "filament_mixed_sublayer_ratios"}):
        if key not in settings:
            continue
        values = settings[key]
        if not isinstance(values, list):
            raise ValueError("Invalid per-filament setting: " + key)
        if key in ("filament_volume_map", "filament_nozzle_map"):
            if len(values) < count:
                raise ValueError("Short filament map: " + key)
            values = values[:count]
        if not values or len(values) % count:
            raise ValueError("Unsupported per-filament array layout: " + key)
        width = len(values) // count
        fields.append([values[i * width:(i + 1) * width] for i in range(count)])
    for key in ("different_settings_to_system", "inherits_group", "compatible_machine_expression_group",
                "compatible_process_expression_group"):
        if key not in settings:
            continue
        values = settings[key]
        prefix = 0 if key == "compatible_process_expression_group" else 1
        if not isinstance(values, list) or len(values) != count + 2 * prefix:
            raise ValueError("Unsupported inheritance metadata: " + key)
        fields.append(values[prefix:prefix + count])
    maps = {"filament_maps": "filament_map", "filament_volume_maps": "filament_volume_map",
            "filament_nozzle_maps": "filament_nozzle_map"}
    for plate in metadata.findall("plate"):
        for node in plate.findall("metadata"):
            key = node.get("key")
            if key not in maps:
                continue
            values = node.get("value", "").split()
            if not values:
                continue
            fallback = settings.get(maps[key])
            if len(values) < count:
                if not isinstance(fallback, list) or len(fallback) < count:
                    raise ValueError("Missing global mapping needed for " + key)
                values += [str(v) for v in fallback[len(values):count]]
            fields.append(values[:count])
    matrix = settings.get("flush_volumes_matrix")
    if matrix is not None and (not isinstance(matrix, list) or len(matrix) != count * count):
        raise ValueError("Invalid purge-volume matrix")
    candidates = {}
    for i in mixed:
        recipe = tuple(int(value) for value in settings["filament_mixed_components"][i].split(","))
        ratios = tuple(Decimal(value) for value in settings["filament_mixed_sublayer_ratios"][i].split(","))
        signature = (recipe, ratios, json.dumps([field[i] for field in fields], sort_keys=True))
        candidates.setdefault(signature, []).append(i)
    result = []
    for slots in candidates.values():
        compatible = []
        for slot in slots:
            for group in compatible:
                if matrix is None or all(
                        matrix[slot * count + k] == matrix[other * count + k] and
                        matrix[k * count + slot] == matrix[k * count + other]
                        for other in group for k in range(count) if k not in (slot, other)):
                    group.append(slot)
                    break
            else:
                compatible.append([slot])
        result.extend([[slot + 1 for slot in group] for group in compatible if len(group) > 1])
    return result


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


def format_filament_report(settings, project_path=None, metadata=None):
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
            script = Path(__file__).resolve().with_name("merge_3mf_colors.py")
            lines.append("Suggested PowerShell command (review materials and settings first):")
            lines.append("python -B %s %s %s %s" %
                         (quote_powershell(script), quote_powershell(path),
                          quote_powershell(output), quote_powershell(sets)))
            lines.append("Matching colors do not guarantee compatible materials or settings.")
        else:
            lines.append("No identical base filament colors found.")
    if metadata is not None:
        groups = identical_mixed_groups(settings, metadata)
        if groups:
            lines.append("Identical mixed filaments:")
            for group in groups:
                i = group[0] - 1
                lines.append("Slots %s: ingredients %s; ratios %s" % (
                    ", ".join(str(slot) for slot in group),
                    settings["filament_mixed_components"][i],
                    settings["filament_mixed_sublayer_ratios"][i]))
            if project_path is not None:
                path = Path(project_path)
                output = path.with_name(path.stem + "_merged_mixed" + path.suffix)
                script = Path(__file__).resolve().with_name("merge_3mf_mixed.py")
                lines.append("Suggested PowerShell command to merge identical mixed filaments:")
                lines.append("python -B %s %s %s" %
                             (quote_powershell(script), quote_powershell(path), quote_powershell(output)))
        else:
            lines.append("No identical mixed filaments found.")
    return "\n".join(lines)


def quote_powershell(value):
    return "'" + str(value).replace("'", "''") + "'"
