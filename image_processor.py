import logging
import math
import os
import re
import subprocess
import tempfile

log = logging.getLogger("pyblock.image")

try:
    from PIL import Image, ImageOps
    _PILLOW_OK = True
except ImportError:
    _PILLOW_OK = False


class ImageProcessor:
    """Load a raster image, quantise it to 1-bit, vectorise, and return shapes."""

    def __init__(
        self,
        threshold: int = 128,
        invert: bool = False,
        vectorizer: str = "vtracer",

        potrace_threshold: float = 0.5,
        potrace_turdsize: int = 2,
        potrace_alphamax: float = 1.0,
        potrace_opttolerance: float = 0.2,

        vtracer_color_precision: int = 6,
        vtracer_filter_speckle: int = 4,

        min_line_width_mm: float = 1.0,
        curve_segments: int = 16,
    ) -> None:
        self.threshold               = threshold
        self.invert                  = invert
        self.vectorizer              = vectorizer
        self.potrace_threshold       = potrace_threshold
        self.potrace_turdsize        = potrace_turdsize
        self.potrace_alphamax        = potrace_alphamax
        self.potrace_opttolerance    = potrace_opttolerance
        self.vtracer_color_precision = vtracer_color_precision
        self.vtracer_filter_speckle  = vtracer_filter_speckle
        self.min_line_width_mm       = min_line_width_mm
        self.curve_segments          = curve_segments

    '''
        Load a raster image, quantize it down to 1 bit and
        then vectorize it to SVG using either potrace or vtracer.  
        Return a list of shapes and the original image size.

        The return type is madness. Don't go there.
    '''
    def process(self, path: str) -> tuple[list[tuple[list, list]], tuple[int, int]]:

        if not _PILLOW_OK:
            raise ImportError(
                "Pillow is required for raster image processing.\n"
                "Install it with:  pip install Pillow"
            )

        log.info("Loading image")
        img = Image.open(path)
        log.info(f"Image loaded. Mode: {img.mode}, Size: {img.width}x{img.height}px, Format: {img.format}")

        orig_w, orig_h = img.width, img.height

        if img.mode != "L":
            img = img.convert("L")
            log.debug("Converted to grayscale.")

        log.info(f"Quantising to 1-bit with threshold {self.threshold}")
        img_1bit = img.point(lambda p: 255 if p >= self.threshold else 0).convert("1")

        if self.invert:
            log.info("Inverting 1-bit image.")
            img_1bit = ImageOps.invert(img_1bit.convert("L")).convert("1")

        total = orig_w * orig_h
        pixels = list(img_1bit.getdata())
        black  = sum(1 for p in pixels if p == 0)
        white  = total - black
        log.info(f"Quantisation stats: Total {total}px, Relief {black}px ({100*black/total if total else 0:.1f}%), Base {white}px ({100*white/total if total else 0:.1f}%)")

        if self.vectorizer == "potrace":
            shapes = self._vectorise_potrace(img_1bit, orig_w, orig_h)
        elif self.vectorizer == "vtracer":
            shapes = self._vectorise_vtracer(img_1bit, orig_w, orig_h)
        else:
            raise ValueError(f"Unknown vectorizer: {self.vectorizer!r}")

        return shapes, (orig_w, orig_h)

    def _vectorise_potrace(self, img_1bit, orig_w: int, orig_h: int) -> list:
        log.info("Vectorising with potrace")
        from svg_parser import SVGParser

        with tempfile.TemporaryDirectory() as tmp:
            bmp_path = os.path.join(tmp, "input.bmp")
            svg_path = os.path.join(tmp, "output.svg")

            img_1bit.convert("L").save(bmp_path)
            log.debug(f"Saved quantised image as {bmp_path}")

            cmd = [
                "potrace", "--svg",
                "--blacklevel",    str(self.potrace_threshold),
                "--turdsize",      str(self.potrace_turdsize),
                "--alphamax",      str(self.potrace_alphamax),
                "--opttolerance",  str(self.potrace_opttolerance),
                "--output", svg_path,
                bmp_path,
            ]
            log.debug(f"Command: {" ".join(cmd)}")

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError("potrace timed out after 120s")
            if result.returncode != 0:
                raise RuntimeError(
                    f"potrace failed (exit {result.returncode}):\n{result.stderr}"
                )
            if result.stderr:
                log.debug(f"potrace stderr: {result.stderr.strip()}")

            parser = SVGParser(
                min_line_width_mm=self.min_line_width_mm,
                curve_segments=self.curve_segments,
            )
            shapes, _ = parser.parse(svg_path)

        log.info(f"potrace produced {len(shapes)} shape(s)")
        
        return shapes

    def _vectorise_vtracer(self, img_1bit, orig_w: int, orig_h: int) -> list:
        log.info("Vectorising with vtracer")
        from svg_parser import SVGParser

        try:
            import vtracer as _vtracer
        except ImportError:
            raise ImportError(
                "The 'vtracer' Python package is required for --vectorizer vtracer.\n"
                "Install it with: pip install vtracer"
            )

        img_rgba   = img_1bit.convert("RGBA")
        pixel_data = img_rgba.get_flattened_data()

        svg_str = _vtracer.convert_pixels_to_svg(
            pixel_data,
            size=(img_1bit.width, img_1bit.height),
            colormode="binary",
            filter_speckle=self.vtracer_filter_speckle,
            color_precision=self.vtracer_color_precision,
        )
        log.debug("vtracer returned %d bytes of SVG", len(svg_str))

        with tempfile.NamedTemporaryFile(
            suffix=".svg", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(svg_str)
            tmp_path = f.name

        try:
            parser = SVGParser(
                min_line_width_mm=self.min_line_width_mm,
                curve_segments=self.curve_segments,
            )
            shapes, _ = parser.parse(tmp_path)
        finally:
            os.unlink(tmp_path)

        log.info(f"vtracer produced {len(shapes)} shape(s)")
    
        return shapes