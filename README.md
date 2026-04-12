# pyBlock

**pyBlock** converts raster images (JPEG, PNG, …) or SVG files into STL files
ready for 3D printing. The output is a solid base plate with an embossed or
raised relief of the input image — a block print that can stamp a design onto
paper, fabric, or clay.

---

## Quick start

```bash
pip install -r requirements.txt
# (also install potrace via your OS package manager — see Requirements)

python pyblock.py --input logo.png --output logo_block.stl
python pyblock.py -i drawing.svg  -o drawing_block.stl --width 80 --relief-height 2
```

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | |
| [Pillow](https://python-pillow.org/) | ≥ 10.0 | `pip install Pillow` |
| [potrace](https://potrace.sourceforge.net/) | ≥ 1.16 | OS package — default vectorizer |
| [vtracer](https://github.com/visioncortex/vtracer) | any | Optional alternative vectorizer |

Install potrace:

```bash
# Ubuntu / Debian
sudo apt install potrace

# macOS
brew install potrace

# Windows — download the binary from https://potrace.sourceforge.net/
# and place potrace.exe somewhere on your PATH.
```

---

## Pipeline

```
Raster image (JPEG/PNG/…)          SVG file
       │                                │
  Pillow load                     SVG parser
  Grayscale + 1-bit                (shapes, paths,
  quantisation                      lines, arcs)
       │                                │
  Vectorize (potrace / vtracer /       │
             pixel-grid fallback)       │
       └────────────┬───────────────────┘
                    │
            Normalised 2-D paths
                    │
             STL builder
        (base plate + relief extrusion
         + ear-clip triangulation)
                    │
              Binary/ASCII STL
```

---

## All command-line parameters

### Input / Output

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--input` | `-i` | *(required)* | Input file: raster image or `.svg` |
| `--output` | `-o` | `<input>.stl` | Output STL file path |

### Plate Geometry

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--width` | `-w` | `100` mm | Plate width. Height is derived from the image aspect ratio. |
| `--plate-thickness` | `-pt` | `3.0` mm | Thickness of the solid base plate |
| `--relief-height` | `-rh` | `1.0` mm | Height of the relief above the plate surface. **Negative values emboss into the plate.** |
| `--min-line-width` | `-lw` | `1.0` mm | Minimum width for lines and thin features |

### Raster Image Processing

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--threshold` | `-t` | `128` | Grayscale threshold (0–255). Pixels **below** this value become the relief. |
| `--invert` | | off | Invert the 1-bit image (swap black ↔ white) |
| `--dither` | | off | Use Floyd-Steinberg dithering instead of hard threshold |

### Vectorization

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--vectorizer` | | `potrace` | `potrace`, `vtracer`, or `none` |
| `--potrace-threshold` | | `0.5` | Potrace blackness threshold (0–1) |
| `--potrace-turdsize` | | `2` | Suppress speckles of up to N pixels |
| `--potrace-alphamax` | | `1.0` | Corner smoothing threshold (0–1.333) |
| `--potrace-opttolerance` | | `0.2` | Curve optimisation tolerance |
| `--vtracer-color-precision` | | `6` | Significant bits for color quantisation |
| `--vtracer-filter-speckle` | | `4` | Discard patches smaller than N pixels |

### STL Output

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--stl-binary` | | on | Write binary STL (compact) |
| `--stl-ascii` | | off | Write ASCII STL instead of binary |
| `--curve-segments` | | `16` | Line segments per curve approximation |

### Miscellaneous

| Parameter | Short | Description |
|---|---|---|
| `--verbose` | `-v` | Enable debug logging |
| `--quiet` | `-q` | Suppress all output except errors |
| `--version` | | Show version and exit |

---

## Examples

```bash
# Basic raster image → STL (100 mm wide, 3 mm plate, 1 mm raised relief)
python pyblock.py -i photo.jpg -o photo_block.stl

# Emboss a logo 0.5 mm into the plate surface
python pyblock.py -i logo.png -o logo_embossed.stl --relief-height -0.5

# Large fabric stamp (200 mm wide, thicker plate, finer curves)
python pyblock.py -i pattern.png -o stamp.stl \
  --width 200 --plate-thickness 5 --relief-height 2 \
  --curve-segments 32

# Use vtracer instead of potrace
python pyblock.py -i artwork.png -o artwork.stl --vectorizer vtracer

# Skip vectorization — work directly from the pixel grid
python pyblock.py -i pixel_art.png -o pixel_block.stl --vectorizer none

# Direct SVG input
python pyblock.py -i drawing.svg -o drawing_block.stl --width 80 --relief-height 1.5

# Invert + dither + ASCII STL (for inspection)
python pyblock.py -i sketch.png -o sketch.stl --invert --dither --stl-ascii

# Dark subject on light background (lower threshold)
python pyblock.py -i signature.png -o sig_block.stl --threshold 80
```

---

## Output geometry

The STL body is a single manifold solid:

```
z = plate_thickness + relief_height   ┌──────────────┐  ← relief top
                                      │  relief layer │
z = plate_thickness                   ├──────────────┤  ← plate top surface
                                      │              │
                                      │  base plate  │
z = 0                                 └──────────────┘  ← bottom
```

With a **negative** `--relief-height` the relief is a pocket cut into the
plate surface (intaglio / deboss).

---

## License

MIT — see `LICENSE` for details.
