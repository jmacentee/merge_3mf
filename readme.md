# Merge two Bambu Studio 3MF projects

`merge_3mf.py` merges two compatible Bambu Studio projects without guessing
color roles or combining duplicate filaments. It requires Python 3.7 or later
and uses only the standard library. It never launches Bambu Studio.

## Usage

From PowerShell in this folder:

```powershell
python -B .\merge_3mf.py .\A.3mf .\B.3mf .\A_and_B.3mf
```

The parameters are `inputA`, `inputB`, and `output`, in that order. Paths
containing spaces must be quoted. The script refuses to overwrite either input
or an existing output; choose a new output name for each run.

To recreate the sample:

```powershell
python -B .\merge_3mf.py `
  .\ColorFactory_Original.3mf `
  .\Hedgehog_Original.3mf `
  .\ColorFactory_And_Hedgehog.3mf
```

This output already exists in the folder. Use another output name if running
the command again.

## Palette ordering

The output contains, in order:

1. All ordinary/base filaments from A, in their original relative order.
2. All ordinary/base filaments from B, in their original relative order.
3. All mixed filaments from A, in their original relative order.
4. All mixed filaments from B, in their original relative order.

Support filaments are treated as ordinary/base filaments. Duplicate colors and
profiles are deliberately retained, even across the two projects. The script
uses `filament_is_mixed` to distinguish mixed slots, not their displayed color.
Interleaved source palettes are also supported.

For the sample, the output has **9 base slots and 25 mixed slots**:

| Output slots | Source |
| --- | --- |
| 1-5 | ColorFactory base filaments: Black, White, Yellow, Cyan, Magenta |
| 6-9 | Hedgehog base filaments: White, Magenta, Yellow, Cyan |
| 10-30 | ColorFactory's 21 mixed filaments |
| 31-34 | Hedgehog's 4 mixed filaments |

Mixed recipes retain their ordered ratios; their ingredient references are
remapped to the corresponding output base slots. Painted triangles, including
subdivided paint trees, and object/part default filament assignments are
remapped as well. No manual repainting should be needed.

## Geometry, settings, and safety

- Both projects' models, parts, support blockers, and build transforms are
  retained. The script does not resize, orient, arrange, or move models.
  **Models may overlap.** Fix placement yourself in Bambu Studio.
- Object and mesh IDs are made globally unique, including across subfiles, to
  avoid Bambu Studio's shared-mesh cache reusing the wrong model.
- Conflicting file paths and UUIDs are separated. Input B's extra files are
  placed under a unique `inputB` prefix inside the ZIP.
- Plates at the same position in the two projects' plate lists are combined.
  Additional plates are retained. Original build coordinates are unchanged.
- **Input A supplies the global printer/process settings**, including layer
  heights and support settings. Input B's explicit per-object and per-part
  overrides are retained, but its global process settings do not replace A's.
  For the sample, A's layer height is 0.20 mm and initial layer height is
  0.12 mm; the original Hedgehog's global layer heights differ.
- The script rejects differing printer models, nozzle diameters, build areas,
  and other checked printer configuration fields. It does not automatically
  convert projects between printers.
- Per-filament profiles/settings are reordered with their palette entries.
  Both inputs must have compatible per-filament array layouts.
- Extra entries beyond the palette in global nozzle/volume maps are discarded.
  If a plate map has too few entries, the script warns and fills the missing
  slots from that project's global map; review the mapping in Bambu Studio.
- Existing within-project purge-volume entries are retained. Newly introduced
  cross-project transitions use **280** as a placeholder. **Recalculate
  purging volumes for the actual printer and filaments before printing.**
- Cached G-code and filament sequences are discarded. Thumbnails inherited
  from A may not show the merged scene until you save it in Bambu Studio.
- Inputs must already contain valid mixed recipes. This generic script
  **does not repair blank or damaged ingredients** from a second reference
  project. Use the original, working files rather than the broken ColorFactory
  file.

## Supported inputs and limitations

This is a merger for **Bambu Studio filament-painted projects**, not every
possible 3MF format. Multiple model objects, mesh subfiles, parts, and plates
are supported. Both projects must use millimeter units.

It rejects unsupported material/texture resources, cyclic or unresolved
components, ambiguous part references, invalid recipes, and palettes exceeding
127 slots. It also rejects cut connectors and recognized layer-specific
settings/G-code metadata, which need their own specialized remapping. Different
slicer versions may use incompatible settings layouts; the script stops rather
than guessing how to combine them. Keep original files and inspect any unfamiliar
project features carefully.

## Verification

The script checks input/output ZIP integrity, recipe references and ratios,
mesh geometry preservation during paint remapping, globally unique output
object IDs, and model/part/paint reference resolution.

Run the focused Python tests with:

```powershell
python -B -m unittest test_merge_3mf -v
```

**You do all slicer and printing tests.** Open the output as a project in Bambu
Studio, confirm both models and their colors, fix overlap, inspect mixed
recipes, review A's global settings and purge volumes, then slice and inspect
the preview before printing.

# WARNING
- Bambu Slicer is not smart enough (yet, November 2026) to treat two project filaments that are both mapped to the same AMS spool as 
a single filament.  I combined two projects and got two each of CMYK and 1st slice was 17h19m, 2477 filament changes, 121g purge tower.
Manually merging the duplicate filaments one-by-one and then correcting the virtual mixed filaments reduced the same project with no other
changes to 14h35m, 1497 filament changes, 72g purge tower. 