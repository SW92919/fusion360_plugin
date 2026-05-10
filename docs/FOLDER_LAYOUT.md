# Folder layout (from screenshots + pipeline needs)

## Recommended structure

```text
New folder/                          ← user-selected root (e.g. Downloads/New folder)
  Lifeproof/                         ← texture root (subfolders = color sets)
    Antler Trail Oak/
      antler-trail-oak-1.jpg         ← appearance / decal slot 1
      antler-trail-oak-2.jpg         ← appearance / decal slot 2
      antler-trail-oak-lifeproof-...jpg   ← optional swatch (can be ignored by glob)
      (optional existing renders / outputs live here too)
  Treads Plus Bullnose - Appearance.f3d
  Treads Plus Square Nose - Decal.f3d
```

## Detection rules

1. **Color set** = immediate subfolder of the selected texture root (e.g. `Lifeproof/Antler Trail Oak`).
2. **Slot images** = files in that folder matching `*_1.*` and `*_2.*` (case-insensitive), any common raster extension (`.jpg`, `.jpeg`, `.png`, `.tif`, …).
3. **Fallbacks** (from your earlier spec):
   - Missing `_1` → use **first** image in folder (excluding obvious swatch if you add heuristics later).
   - Missing `_2` → use **last** image; if the design has only one appearance slot, **ignore** `_2`.
4. **Swatch / marketing hero** images: optionally exclude by minimum dimensions, filename keywords (`swatch`, `4000`), or an ignore list in config.

## Output location

Write renders **into the same color folder** as the source textures so art stays next to inputs:

`.../Lifeproof/Antler Trail Oak/Treads Plus Bullnose - Appearance – antler-trail-oak-1 – Nose Front.png`

If that collides with an existing file, bump to `... (v2).png`, etc.
