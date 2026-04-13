#!/usr/bin/env python3
"""
pyBlock - Convert images to 3D-printable STL block prints.

Reads raster images (JPEG, PNG, etc.) or SVG files and outputs an STL file
representing a solid plate with an embossed or raised relief suitable for
printing on paper or fabric.
"""

import argparse
from asyncio import subprocess
import logging
import sys
import os
import time
import shutil

from image_processor import ImageProcessor
from svg_parser import SVGParser
from stl_builder import STLBuilder

__version__ = "0.0.1"

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyblock",
        description=(
            "Convert images or SVG files into STL block prints for 3D printing.\n"
            "Produces a solid plate with an embossed or raised relief of the input image."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pyblock -input photo.jpg -output stamp.stl
  pyblock -i drawing.svg -o block.stl -width 100 -relief-height 2.0
  pyblock -input logo.png -threshold 128 -min-line-width 1.5 -invert
  pyblock -input photo.jpg -vectorizer potrace -potrace-threshold 0.45
""",
    )

    # Input / Output ----
    io_group = parser.add_argument_group("Input / Output")
    io_group.add_argument(
        "--input", "-i",
        metavar="FILE",
        required=True,
        help="Input image file (JPEG, PNG, BMP, TIFF, GIF) or SVG file.",
    )
    io_group.add_argument(
        "--output", "-o",
        metavar="FILE",
        default=None,
        help="Output STL file path. Defaults to <input_basename>.stl.",
    )

    # Plate geometry ----
    geom_group = parser.add_argument_group("Plate Geometry")
    geom_group.add_argument(
        "--width", "-w",
        type=float,
        default=100.0,
        metavar="MM",
        help="Width of the output plate in mm (default: 100). Height is computed from image aspect ratio.",
    )
    geom_group.add_argument(
        "--plate-thickness", "-pt",
        type=float,
        default=3.0,
        metavar="MM",
        help="Thickness of the solid base plate in mm (default: 3.0).",
    )
    geom_group.add_argument(
        "--relief-height", "-rh",
        type=float,
        default=1.0,
        metavar="MM",
        help=(
            "Height of the relief layer above the plate surface in mm (default: 1.0). "
            "Use a negative value to emboss into the plate surface instead."
        ),
    )
    geom_group.add_argument(
        "--min-line-width", "-lw",
        type=float,
        default=1.0,
        metavar="MM",
        help="Minimum width for lines and thin features in mm (default: 1.0).",
    )

    # Raster / quantization -
    raster_group = parser.add_argument_group("Raster Image Processing")
    raster_group.add_argument(
        "--threshold", "-t",
        type=int,
        default=128,
        metavar="0-255",
        help=(
            "Grayscale threshold for 1-bit quantization (0-255, default: 128). "
            "Pixels at or above this value become white (background); below become black (relief)."
        ),
    )
    raster_group.add_argument(
        "--invert",
        action="store_true",
        help="Invert the 1-bit image before processing (swap black and white).",
    )

    # Vectorization -----
    vec_group = parser.add_argument_group("Vectorization (raster inputs only)")
    vec_group.add_argument(
        "--vectorizer",
        choices=["potrace", "vtracer"],
        default="potrace",
        help=(
            "Vectorization engine to use (default: vtracer). "
            "'none' skips vectorization and works directly from the pixel grid."
        ),
    )
    vec_group.add_argument(
        "--potrace-threshold",
        type=float,
        default=0.5,
        metavar="0.0-1.0",
        help="Potrace: Blackness threshold (0.0-1.0, default: 0.5).",
    )
    vec_group.add_argument(
        "--potrace-turdsize",
        type=int,
        default=2,
        metavar="N",
        help="Potrace: Suppress speckles of up to N pixels (default: 2).",
    )
    vec_group.add_argument(
        "--potrace-alphamax",
        type=float,
        default=1.0,
        metavar="0.0-1.333",
        help="Potrace: Corner threshold parameter (default: 1.0).",
    )
    vec_group.add_argument(
        "--potrace-opttolerance",
        type=float,
        default=0.2,
        metavar="FLOAT",
        help="Potrace: Curve optimization tolerance (default: 0.2).",
    )
    vec_group.add_argument(
        "--vtracer-color-precision",
        type=int,
        default=6,
        metavar="N",
        help="Vtracer: Number of significant bits for color quantization (default: 6).",
    )
    vec_group.add_argument(
        "--vtracer-filter-speckle",
        type=int,
        default=4,
        metavar="PX",
        help="Vtracer: Discard patches smaller than N pixels (default: 4).",
    )

    # STL output --------
    stl_group = parser.add_argument_group("STL Output")
    stl_group.add_argument(
        "--stl-binary",
        action="store_true",
        default=True,
        help="Write binary STL (default).",
    )
    stl_group.add_argument(
        "--stl-ascii",
        action="store_true",
        default=False,
        help="Write ASCII STL instead of binary.",
    )
    stl_group.add_argument(
        "--curve-segments",
        type=int,
        default=16,
        metavar="N",
        help="Number of line segments used to approximate each curve (default: 16).",
    )

    # Logging / misc ----
    misc_group = parser.add_argument_group("Miscellaneous")
    misc_group.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging.",
    )
    misc_group.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress all output except errors.",
    )
    misc_group.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser

def setup_logging(level: int = logging.INFO) -> None:
    GREY   = "\033[90m"
    CYAN   = "\033[96m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    level_colours = {
        logging.DEBUG:    GREY,
        logging.INFO:     CYAN,
        logging.WARNING:  YELLOW,
        logging.ERROR:    RED,
        logging.CRITICAL: BOLD + RED,
    }

    level_names = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARN",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "FATAL",
    }

    class ColouredFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            colour = level_colours.get(record.levelno, RESET)
            level_name = level_names.get(record.levelno, record.levelname)
            level_tag = f"{colour}[{level_name:<5}]{RESET}"
            name_tag  = f"{GREY}{record.name}{RESET}"
            return f"{level_tag} {name_tag}: {record.getMessage()}"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColouredFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Resolve logging level
    if args.quiet:
        log_level = logging.ERROR
    elif args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    setup_logging(log_level)
    log = logging.getLogger("pyblock")

    if not args.quiet:
        print(f"pyBlock v{__version__} - Image-to-STL block print converter\n")
        
    # Validate arguments
    if not os.path.isfile(args.input):
        log.error(f"Input file not found: {args.input}")
        return 1

    if args.threshold < 0 or args.threshold > 255:
        log.error(f"--threshold must be in range 0-255 (got {args.threshold})")
        return 1

    if args.width <= 0:
        log.error(f"--width must be positive (got {args.width})")
        return 1

    if args.plate_thickness <= 0:
        log.error(f"--plate-thickness must be positive (got {args.plate_thickness})")
        return 1

    if args.min_line_width <= 0:
        log.error(f"--min-line-width must be positive (got {args.min_line_width})")
        return 1

    if args.curve_segments < 4:
        log.error(f"--curve-segments must be at least 4 (got {args.curve_segments})")
        return 1

    if args.stl_ascii and args.stl_binary:
        log.error("Cannot specify both -stl-ascii and -stl-binary.")
        return 1    
        
    if args.output:
        output_path = args.output
    else:
        directory = os.path.dirname(args.input)
        base = os.path.splitext(os.path.basename(args.input))[0]
        output_path = os.path.join(directory, f"{base}.stl")

    use_binary_stl = not args.stl_ascii

    # Determine input type
    ext = os.path.splitext(args.input)[1].lower()

    # Check external dependencies -
    if ext == ".svg" and args.vectorizer == "potrace":
        path = shutil.which("potrace")
        if path:
            try:
                result = subprocess.run(["potrace", "--version"], capture_output=True, text=True, timeout=5)
                version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
                log.debug(f"Found potrace: {path} ({version_line})")
            except Exception:
                log.debug(f"Found potrace at {path} (version unknown)")
        else: 
            log.error(
                "Vectorizer 'potrace' not found on PATH.\n\n"
                "Install it with one of:\n"
                "Ubuntu/Debian : sudo apt install potrace\n"
                "macOS (brew)  : brew install potrace\n"
                "Windows       : https://potrace.sourceforge.net/\n\n"
                "Or choose a different vectorizer with -vectorizer vtracer"
            )
            return 1
        
    # Process input
    t_start = time.perf_counter()
    shapes = []
    image_width_mm = args.width
    image_height_mm = None   # filled in after we know aspect ratio

    if ext == ".svg":
        log.info(f"Reading SVG from '{args.input}'")
        svg_parser = SVGParser(
            min_line_width_mm=args.min_line_width,
            curve_segments=args.curve_segments,
        )
        shapes, (svg_w, svg_h) = svg_parser.parse(args.input)
        aspect = svg_h / svg_w if svg_w else 1.0
        image_height_mm = image_width_mm * aspect
        log.info(f"SVG parsed: {len(shapes)} path(s), document size {svg_w:.2f}x{svg_h:.2f} user units")
    else:
        log.info(f"Reading raster image from '{args.input}'")
        processor = ImageProcessor(
            threshold=args.threshold,
            invert=args.invert,
            vectorizer=args.vectorizer,
            potrace_threshold=args.potrace_threshold,
            potrace_turdsize=args.potrace_turdsize,
            potrace_alphamax=args.potrace_alphamax,
            potrace_opttolerance=args.potrace_opttolerance,
            vtracer_color_precision=args.vtracer_color_precision,
            vtracer_filter_speckle=args.vtracer_filter_speckle,
            min_line_width_mm=args.min_line_width,
            curve_segments=args.curve_segments,
        )
        shapes, (img_w_px, img_h_px) = processor.process(args.input)
        aspect = img_h_px / img_w_px if img_w_px else 1.0
        image_height_mm = image_width_mm * aspect

    if not shapes:
        log.warning("No geometry found in input - the STL will contain only the base plate.")

    log.info(f"Output plate: {image_width_mm:.2f}x{image_height_mm:.2f}mm, Plate thickness: {args.plate_thickness:.2f}mm, Relief: {args.relief_height:.2f}mm")

    # Build STL
    log.info("Building STL geometry")
    builder = STLBuilder(
        plate_width_mm=image_width_mm,
        plate_height_mm=image_height_mm,
        plate_thickness_mm=args.plate_thickness,
        relief_height_mm=args.relief_height,
        min_line_width_mm=args.min_line_width,
        binary=use_binary_stl,
        curve_segments=args.curve_segments,
    )
    triangle_count = builder.build_and_write(shapes, output_path)

    elapsed = time.perf_counter() - t_start
    log.info(f"Done! Wrote {triangle_count} triangles to '{output_path}' in {elapsed:.2f}s")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
