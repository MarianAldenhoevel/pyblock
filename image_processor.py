"""
image_processor.py — Raster image loading, 1-bit quantisation, and vectorisation.

Pipeline
--------
1. Load the image with Pillow.
2. Convert to grayscale.
3. Quantise to 1-bit
4. Print quantisation statistics to the console.
5. Vectorise the 1-bit image via:
      • potrace  — invokes the system 'potrace' binary
      • vtracer  — invokes the system 'vtracer' binary
      • none     — pixel-grid marching-squares fallback
6. Return a list of closed polygons in *image* coordinate space,
   normalised so the image fits inside a 1×1 box,
   plus the original pixel dimensions.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Sequence

log = logging.getLogger("pyblock.image")

# Optional imports — import errors are caught at call time so the module always loads.
try:
    from PIL import Image
    _PILLOW_OK = True
except ImportError:
    _PILLOW_OK = False


# ── Public class ──────────────────────────────────────────────────────────────

class ImageProcessor:
    """Load a raster image and return vectorised geometry."""

    def __init__(
        self,
        threshold: int = 128,
        invert: bool = False,
        vectorizer: str = "potrace",
        potrace_threshold: float = 0.5,
        potrace_turdsize: int = 2,
        potrace_alphamax: float = 1.0,
        potrace_opttolerance: float = 0.2,
        vtracer_color_precision: int = 6,
        vtracer_filter_speckle: int = 4,
        min_line_width_mm: float = 1.0,
        curve_segments: int = 16,
    ) -> None:
        self.threshold            = threshold
        self.invert               = invert
        self.vectorizer           = vectorizer
        self.potrace_threshold    = potrace_threshold
        self.potrace_turdsize     = potrace_turdsize
        self.potrace_alphamax     = potrace_alphamax
        self.potrace_opttolerance = potrace_opttolerance
        self.vtracer_color_precision = vtracer_color_precision
        self.vtracer_filter_speckle  = vtracer_filter_speckle
        self.min_line_width_mm    = min_line_width_mm
        self.curve_segments       = curve_segments

    # ── Main entry point ──────────────────────────────────────────────────────

    def process(
        self, path: str
    ) -> tuple[list[list[tuple[float, float]]], tuple[int, int]]:
        """
        Process *path* and return:
          (paths, (width_px, height_px))

        Each path is a closed list of (x, y) points normalised to [0, 1] in the
        image's coordinate space (origin = top-left, y grows downward).
        """
        if not _PILLOW_OK:
            raise ImportError(
                "Pillow is required for raster image processing.\n"
                "Install it with:  pip install Pillow"
            )

        log.info("Loading image…")
        img = Image.open(path)
        log.info(
            "  Mode: %s  |  Size: %d×%d px  |  Format: %s",
            img.mode, img.width, img.height, img.format,
        )

        orig_w, orig_h = img.width, img.height

        # Convert to grayscale
        if img.mode != "L":
            img = img.convert("L")
            log.debug("  Converted to grayscale.")

        # 1-bit quantisation
        img_1bit = self._quantise(img)

        # Statistics
        self._print_stats(img_1bit, orig_w, orig_h)

        # Vectorise
        paths = self._vectorise(img_1bit, orig_w, orig_h)

        return paths, (orig_w, orig_h)

    # ── Quantisation ──────────────────────────────────────────────────────────

    def _quantise(self, img_gray: "Image.Image") -> "Image.Image":
        """Return a 1-bit (mode '1') PIL image."""
        import PIL.Image as PILImage

        log.info("Quantising to 1-bit with threshold %d…", self.threshold)
        # Point transform: pixel >= threshold → 255 (white), else 0 (black)
        img_thresh = img_gray.point(lambda p: 255 if p >= self.threshold else 0)
        img_1bit = img_thresh.convert("1")

        if self.invert:
            from PIL import ImageOps
            log.info("Inverting 1-bit image.")
            img_1bit = ImageOps.invert(img_1bit.convert("L")).convert("1")

        return img_1bit

    # ── Statistics ────────────────────────────────────────────────────────────

    @staticmethod
    def _print_stats(img_1bit: "Image.Image", w: int, h: int) -> None:
        total = w * h
        # PIL mode '1' stores pixels as 0 or 255 when accessed via getdata()
        pixels = list(img_1bit.getdata())
        black  = sum(1 for p in pixels if p == 0)
        white  = total - black
        pct_b  = 100.0 * black / total if total else 0.0
        pct_w  = 100.0 * white / total if total else 0.0

        log.info("  ┌── 1-bit quantisation statistics ──────────────────")
        log.info("  │  Total pixels : %10d", total)
        log.info("  │  Black (set)  : %10d  (%5.1f %%)  — will form relief", black, pct_b)
        log.info("  │  White (bg)   : %10d  (%5.1f %%)  — base plate only", white, pct_w)
        log.info("  └───────────────────────────────────────────────────")

    # ── Vectorisation dispatch ────────────────────────────────────────────────

    def _vectorise(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[list[tuple[float, float]]]:
        if self.vectorizer == "potrace":
            return self._vectorise_potrace(img_1bit, w, h)
        elif self.vectorizer == "vtracer":
            return self._vectorise_vtracer(img_1bit, w, h)
        else:
            log.info("Vectorizer: none — using pixel-grid outline extraction.")
            return self._vectorise_pixels(img_1bit, w, h)

    # ── potrace ───────────────────────────────────────────────────────────────

    def _vectorise_potrace(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[list[tuple[float, float]]]:
        log.info("Vectorising with potrace…")
        with tempfile.TemporaryDirectory() as tmp:
            bmp_path = os.path.join(tmp, "input.bmp")
            svg_path = os.path.join(tmp, "output.svg")

            # potrace reads BMP; save the 1-bit image as an 8-bit greyscale BMP
            # (potrace interprets dark pixels as foreground)
            img_1bit.convert("L").save(bmp_path)

            log.debug(f"  Saved quantized image as {bmp_path}")

            cmd = [
                "potrace",
                "--svg",
                "--blacklevel", f"{self.potrace_threshold}",
                "--turdsize", f"{self.potrace_turdsize}",
                "--alphamax", f"{self.potrace_alphamax}",
                "--opttolerance", f"{self.potrace_opttolerance}",
                "--output", svg_path,
                bmp_path,
            ]
            log.debug("  Command: %s", " ".join(cmd))
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError("potrace timed out after 120s")
            if result.returncode != 0:
                raise RuntimeError(
                    f"potrace failed (exit {result.returncode}):\n{result.stderr}"
                )
            if result.stderr:
                log.debug("  potrace stderr: %s", result.stderr.strip())

            paths = _parse_svg_paths(svg_path, w, h, self.curve_segments)

        log.info("  potrace produced %d path(s).", len(paths))
        return paths

    # ── vtracer ───────────────────────────────────────────────────────────────

    def _vectorise_vtracer(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[tuple[list[tuple[float, float]], list]]:
        """
        Vectorise using the vtracer Python library (no subprocess or temp files).

        vtracer.convert_pixels_to_svg() takes raw RGBA pixel data and returns
        an SVG string directly in memory.  The output SVG has no group-level
        transform (unlike potrace); instead each <path> carries its own
        transform="translate(x,y)" which our _parse_svg_paths walker handles.
        """
        log.info("Vectorising with vtracer…")
        try:
            import vtracer as _vtracer
        except ImportError:
            raise ImportError(
                "The 'vtracer' Python package is required for --vectorizer vtracer.\n"
                "Install it with:  pip install vtracer"
            )

        # vtracer needs RGBA pixel data as a tuple of (R,G,B,A) tuples.
        # The 1-bit image is converted to RGBA: black pixels → (0,0,0,255),
        # white pixels → (255,255,255,255).
        img_rgba = img_1bit.convert("RGBA")
        pixel_data = img_rgba.get_flattened_data()

        svg_str = _vtracer.convert_pixels_to_svg(
            pixel_data,
            size=(w, h),
            colormode="binary",
            filter_speckle=self.vtracer_filter_speckle,
            color_precision=self.vtracer_color_precision,
        )
        log.debug("  vtracer returned %d bytes of SVG", len(svg_str))

        # Parse the in-memory SVG string through a temporary file so we can
        # reuse _parse_svg_paths (which uses ElementTree.parse on a path).
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".svg", mode="w",
                                         encoding="utf-8", delete=False) as f:
            f.write(svg_str)
            tmp_path = f.name
        try:
            shapes = _parse_svg_paths(tmp_path, w, h, self.curve_segments)
        finally:
            os.unlink(tmp_path)

        log.info("  vtracer produced %d shape(s).", len(shapes))
        return shapes

    # ── pixel-grid / contour fallback ────────────────────────────────────────

    def _vectorise_pixels(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[tuple[list[tuple[float, float]], list]]:
        """
        Vectorise the 1-bit image by tracing pixel-exact outlines.

        Uses skimage.measure.find_contours (Marching Squares) when available,
        which produces proper closed polygons with hole detection — letter
        counters ('o', 'e', 'B' etc.) become holes, giving manifold STL output.

        Falls back to a simple horizontal pixel-run approach if skimage is not
        installed.  The fallback produces valid geometry but adjacent runs share
        walls, which some slicers will repair automatically.
        """
        try:
            import numpy as np
            from skimage import measure as _skm
            return self._vectorise_pixels_contour(img_1bit, w, h, np, _skm)
        except ImportError:
            log.warning(
                "scikit-image not installed — using pixel-run fallback.\n"
                "  Install it for better results:  pip install scikit-image"
            )
            return self._vectorise_pixels_runs(img_1bit, w, h)

    def _vectorise_pixels_contour(
        self, img_1bit: "Image.Image", w: int, h: int, np, skm
    ) -> list[tuple[list[tuple[float, float]], list]]:
        """Marching-squares contour tracing via scikit-image."""
        log.info("  Tracing pixel contours (%d×%d) via scikit-image…", w, h)

        # Build binary array: 1 = foreground (black), 0 = background
        arr = np.array(img_1bit.convert("L"))
        binary = (arr < 128).astype(np.uint8)

        # Pad with zeros so contours at the image border close properly
        padded = np.pad(binary, 1, constant_values=0)

        # find_contours returns sub-pixel contours in (row, col) order
        contours_rc = skm.find_contours(padded, 0.5)
        # Undo the 1-pixel padding offset
        contours_rc = [c - 1 for c in contours_rc]

        # Convert (row, col) → normalised (x, y) = (col/w, row/h)
        def to_xy(c_rc):
            return [(float(col) / w, float(row) / h) for row, col in c_rc]

        # Build flat list of (normalised_pts, signed_area) for grouping
        sub_paths = [to_xy(c) for c in contours_rc]

        log.info("  Contour tracing produced %d contour(s).", len(sub_paths))

        # _group_sub_paths classifies by winding: positive area = outer, negative = hole
        # Pass svg_w=1, svg_h=1 because pts are already normalised
        return _group_sub_paths(sub_paths, 1.0, 1.0)

    def _vectorise_pixels_runs(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[tuple[list[tuple[float, float]], list]]:
        """Fallback: merge horizontal pixel runs into rectangle shapes."""
        log.info("  Building pixel-run geometry (%d×%d)…", w, h)
        pixels = img_1bit.load()
        shapes = []
        for y in range(h):
            x_start: int | None = None
            for x in range(w):
                is_black = (pixels[x, y] == 0)
                if is_black and x_start is None:
                    x_start = x
                elif not is_black and x_start is not None:
                    shapes.append((_rect_path(x_start, y, x, y + 1, w, h), []))
                    x_start = None
            if x_start is not None:
                shapes.append((_rect_path(x_start, y, w, y + 1, w, h), []))
        log.info("  Pixel-run fallback produced %d rectangle(s).", len(shapes))
        return shapes


# ── SVG path parser (shared by potrace + vtracer outputs) ────────────────────

def _signed_area_2d(pts: list[tuple[float, float]]) -> float:
    """Shoelace signed area. Positive = CCW, negative = CW."""
    n = len(pts)
    area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def _point_in_polygon(pt: tuple[float, float],
                       poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (2-D, unnested)."""
    x, y = pt
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _group_sub_paths(
    sub_paths: list[list[tuple[float, float]]],
    svg_w: float, svg_h: float,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    """
    Group a list of sub-paths into (outer, [holes]) Shape tuples.

    Works for both SVG compound paths and independently traced contours
    (e.g. from scikit-image Marching Squares), regardless of which winding
    sign the caller uses for outers vs holes.

    Algorithm
    ---------
    1. Normalise to [0,1]x[0,1] and compute signed areas.
    2. Sort by absolute area descending.  The largest contour is always an
       outer shell; smaller contours are classified relative to it.
    3. For each contour, walk the sorted list to find the smallest enclosing
       parent via point-in-polygon.  A contour whose parent has OPPOSITE
       winding is a hole of that parent; same winding means it is a nested
       independent outer (e.g. a filled island inside a donut).
    4. Contours with no enclosing parent are top-level outers.

    This is winding-sign agnostic: it handles SVG paths (where the outer may
    be CW or CCW depending on the drawing tool) and scikit-image contours
    (where the outer is always CW in screen/y-down coordinates).
    """
    if not sub_paths:
        return []

    # Normalise and compute signed areas
    normed: list[tuple[list[tuple[float, float]], float]] = []
    for sp in sub_paths:
        pts = sp[:-1] if (len(sp) > 1 and sp[0] == sp[-1]) else sp
        if len(pts) < 3:
            continue
        norm = [(x / svg_w, y / svg_h) for x, y in pts]
        area = _signed_area_2d(norm)
        normed.append((norm, area))

    if not normed:
        return []

    # Sort by absolute area descending so we process large contours first
    normed.sort(key=lambda t: abs(t[1]), reverse=True)

    n = len(normed)
    # parent[i] = index of the smallest enclosing contour, or -1 for top-level
    parent: list[int] = [-1] * n

    for i in range(n):
        pts_i, _ = normed[i]
        test_pt = pts_i[0]
        # Find the smallest (latest in sorted order) contour that contains i
        for j in range(i - 1, -1, -1):
            pts_j, _ = normed[j]
            if _point_in_polygon(test_pt, pts_j):
                parent[i] = j
                break

    # Classify: a contour is a hole of its parent if their winding signs differ
    # Contours with no parent, or same winding as parent, are independent outers
    # We collect (outer_idx -> [hole_pts]) and handle nesting levels
    outer_holes: dict[int, list] = {}
    is_hole: list[bool] = [False] * n

    for i in range(n):
        p = parent[i]
        if p == -1:
            outer_holes.setdefault(i, [])
        else:
            _, area_i = normed[i]
            _, area_p = normed[p]
            if (area_i > 0) != (area_p > 0):
                # Opposite winding: i is a hole of p
                is_hole[i] = True
                outer_holes.setdefault(p, []).append(normed[i][0])
            else:
                # Same winding: i is an independent inner outer
                outer_holes.setdefault(i, [])

    # Ensure every outer has an entry
    for i in range(n):
        if not is_hole[i]:
            outer_holes.setdefault(i, [])

    # Build result list; each outer may have holes assigned to it
    # Normalise winding: ensure outers have consistent sign by checking against
    # the largest contour (index 0) and reversing if needed
    if normed:
        canonical_sign = 1 if normed[0][1] > 0 else -1
    else:
        canonical_sign = 1

    result = []
    for i in sorted(outer_holes.keys()):
        if is_hole[i]:
            continue  # already assigned as a hole
        pts, area = normed[i]
        # Ensure outer winding is consistent (all outers same sign)
        if (area > 0) != (canonical_sign > 0):
            pts = pts[::-1]
        holes = outer_holes.get(i, [])
        result.append((pts, holes))

    return result


def _parse_svg_paths(
    svg_path: str, img_w: int, img_h: int, curve_segments: int
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    """
    Extract and tessellate <path d="..."> elements from a vectoriser SVG file
    (potrace / vtracer output), correctly applying all group transforms.

    potrace wraps paths in a <g transform="translate(0,H) scale(0.1,-0.1)">.
    Ignoring this transform makes geometry ~10x too large and Y-mirrored.
    This function walks the tree applying cumulative transforms so that
    path coordinates are correctly mapped into the viewBox coordinate space
    before normalisation.

    Returns a list of Shape objects: (outer_contour, [hole_contour, ...])
    with all coordinates normalised to [0,1]x[0,1].
    """
    import re
    from xml.etree import ElementTree as ET
    from svg_parser import _Transform, _strip_ns

    tree = ET.parse(svg_path)
    root = tree.getroot()
    _strip_ns(root)

    # Viewport: the coordinate space path data is expressed in after transforms
    vb = root.get("viewBox")
    if vb:
        parts = [float(x) for x in vb.replace(",", " ").split()]
        svg_w, svg_h = parts[2], parts[3]
    else:
        # Strip unit suffixes (pt, mm, px ...) from width/height attributes
        def _bare(s, default):
            m = re.match(r"[0-9.eE+-]+", (s or "").strip())
            return float(m.group()) if m else default
        svg_w = _bare(root.get("width",  ""), float(img_w))
        svg_h = _bare(root.get("height", ""), float(img_h))

    log.debug("  SVG coordinate space: %.4f x %.4f (normalisation basis)", svg_w, svg_h)

    # Walk the element tree accumulating transforms, collect sub-path groups
    grouped: list[list[list[tuple[float, float]]]] = []

    def _walk(elem, t: _Transform) -> None:
        tag = elem.tag
        local = _Transform.from_attr(elem.get("transform", ""))
        t2 = t @ local
        if tag in ("g", "svg"):
            for child in elem:
                _walk(child, t2)
        elif tag == "path":
            d = elem.get("d", "")
            if d:
                raw = _tessellate_svg_d(d, curve_segments)
                group = [[t2.apply(x, y) for x, y in pts] for pts in raw if pts]
                if group:
                    grouped.append(group)

    _walk(root, _Transform())

    shapes: list[tuple[list, list]] = []
    for group in grouped:
        shapes.extend(_group_sub_paths(group, svg_w, svg_h))
    return shapes


# ── SVG 'd' attribute tessellator ─────────────────────────────────────────────

def _tessellate_svg_d(
    d: str, curve_segments: int
) -> list[list[tuple[float, float]]]:
    """
    Parse an SVG path 'd' attribute and return a list of sub-paths,
    each a list of (x, y) points.  Curves are tessellated into line segments.
    """
    tokens = _tokenise_path(d)
    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    cx, cy = 0.0, 0.0          # current point
    start_x, start_y = 0.0, 0.0
    prev_ctrl: tuple[float, float] | None = None
    prev_cmd = ""

    i = 0
    while i < len(tokens):
        cmd = tokens[i]; i += 1
        relative = cmd.islower()
        cmd_u = cmd.upper()

        def _abs(dx: float, dy: float) -> tuple[float, float]:
            return (cx + dx, cy + dy) if relative else (dx, dy)

        if cmd_u == "M":
            if current:
                paths.append(current)
            x, y = _abs(float(tokens[i]), float(tokens[i+1])); i += 2
            current = [(x, y)]
            cx, cy = x, y
            start_x, start_y = x, y
            # subsequent coords are implicit L
            while i < len(tokens) and not _is_cmd(tokens[i]):
                x, y = _abs(float(tokens[i]), float(tokens[i+1])); i += 2
                current.append((x, y))
                cx, cy = x, y

        elif cmd_u == "Z":
            if current:
                current.append((start_x, start_y))
                paths.append(current)
                current = []
            cx, cy = start_x, start_y

        elif cmd_u == "L":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                x, y = _abs(float(tokens[i]), float(tokens[i+1])); i += 2
                current.append((x, y))
                cx, cy = x, y

        elif cmd_u == "H":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                nx = (cx + float(tokens[i])) if relative else float(tokens[i]); i += 1
                cy_new = cy
                current.append((nx, cy_new))
                cx = nx

        elif cmd_u == "V":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                ny = (cy + float(tokens[i])) if relative else float(tokens[i]); i += 1
                current.append((cx, ny))
                cy = ny

        elif cmd_u == "C":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                x1, y1 = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                x2, y2 = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                x,  y  = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                pts = _cubic_bezier(cx, cy, x1, y1, x2, y2, x, y, curve_segments)
                current.extend(pts[1:])
                prev_ctrl = (x2, y2)
                cx, cy = x, y

        elif cmd_u == "S":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                if prev_cmd in ("C", "S", "c", "s") and prev_ctrl:
                    x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
                else:
                    x1, y1 = cx, cy
                x2, y2 = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                x,  y  = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                pts = _cubic_bezier(cx, cy, x1, y1, x2, y2, x, y, curve_segments)
                current.extend(pts[1:])
                prev_ctrl = (x2, y2)
                cx, cy = x, y

        elif cmd_u == "Q":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                x1, y1 = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                x,  y  = _abs(float(tokens[i]),   float(tokens[i+1])); i += 2
                pts = _quadratic_bezier(cx, cy, x1, y1, x, y, curve_segments)
                current.extend(pts[1:])
                prev_ctrl = (x1, y1)
                cx, cy = x, y

        elif cmd_u == "T":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                if prev_cmd in ("Q", "T", "q", "t") and prev_ctrl:
                    x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
                else:
                    x1, y1 = cx, cy
                x, y = _abs(float(tokens[i]), float(tokens[i+1])); i += 2
                pts = _quadratic_bezier(cx, cy, x1, y1, x, y, curve_segments)
                current.extend(pts[1:])
                prev_ctrl = (x1, y1)
                cx, cy = x, y

        elif cmd_u == "A":
            while i < len(tokens) and not _is_cmd(tokens[i]):
                rx   = float(tokens[i]);   i += 1
                ry   = float(tokens[i]);   i += 1
                rot  = float(tokens[i]);   i += 1
                large = int(tokens[i]);    i += 1
                sweep = int(tokens[i]);    i += 1
                x, y = _abs(float(tokens[i]), float(tokens[i+1])); i += 2
                pts = _arc(cx, cy, rx, ry, rot, large, sweep, x, y, curve_segments)
                current.extend(pts[1:])
                cx, cy = x, y

        prev_cmd = cmd

    if current:
        paths.append(current)

    return paths


# ── Bezier / arc helpers ──────────────────────────────────────────────────────

def _cubic_bezier(
    x0: float, y0: float,
    x1: float, y1: float,
    x2: float, y2: float,
    x3: float, y3: float,
    n: int,
) -> list[tuple[float, float]]:
    pts = []
    for k in range(n + 1):
        t = k / n
        mt = 1 - t
        x = mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3
        y = mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3
        pts.append((x, y))
    return pts


def _quadratic_bezier(
    x0: float, y0: float,
    x1: float, y1: float,
    x2: float, y2: float,
    n: int,
) -> list[tuple[float, float]]:
    pts = []
    for k in range(n + 1):
        t = k / n
        mt = 1 - t
        x = mt**2*x0 + 2*mt*t*x1 + t**2*x2
        y = mt**2*y0 + 2*mt*t*y1 + t**2*y2
        pts.append((x, y))
    return pts


def _arc(
    x1: float, y1: float,
    rx: float, ry: float,
    x_rot_deg: float,
    large: int, sweep: int,
    x2: float, y2: float,
    n: int,
) -> list[tuple[float, float]]:
    """Approximate an SVG arc with n line segments."""
    if rx == 0 or ry == 0:
        return [(x1, y1), (x2, y2)]

    phi = math.radians(x_rot_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)

    # Step 1: compute (x1', y1')
    dx = (x1 - x2) / 2; dy = (y1 - y2) / 2
    x1p =  cos_phi*dx + sin_phi*dy
    y1p = -sin_phi*dx + cos_phi*dy

    # Step 2: compute (cx', cy')
    rx, ry = abs(rx), abs(ry)
    lam = (x1p/rx)**2 + (y1p/ry)**2
    if lam > 1:
        sq = math.sqrt(lam)
        rx *= sq; ry *= sq

    num = max(0.0, (rx*ry)**2 - (rx*y1p)**2 - (ry*x1p)**2)
    den = (rx*y1p)**2 + (ry*x1p)**2
    sq = math.sqrt(num/den) if den else 0.0
    if large == sweep:
        sq = -sq

    cxp =  sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx

    # Step 3: centre
    cx = cos_phi*cxp - sin_phi*cyp + (x1+x2)/2
    cy = sin_phi*cxp + cos_phi*cyp + (y1+y2)/2

    # Step 4: angles
    def _angle(ux, uy, vx, vy):
        n_ = math.sqrt(ux*ux + uy*uy) * math.sqrt(vx*vx + vy*vy)
        if n_ == 0: return 0.0
        c = max(-1.0, min(1.0, (ux*vx + uy*vy) / n_))
        a = math.acos(c)
        if ux*vy - uy*vx < 0: a = -a
        return a

    theta1 = _angle(1, 0, (x1p - cxp)/rx, (y1p - cyp)/ry)
    dtheta = _angle((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)

    if sweep == 0 and dtheta > 0: dtheta -= 2*math.pi
    if sweep == 1 and dtheta < 0: dtheta += 2*math.pi

    pts = []
    for k in range(n + 1):
        t = theta1 + dtheta * k / n
        xp = rx * math.cos(t)
        yp = ry * math.sin(t)
        pts.append((
            cos_phi*xp - sin_phi*yp + cx,
            sin_phi*xp + cos_phi*yp + cy,
        ))
    return pts


# ── Tokeniser ─────────────────────────────────────────────────────────────────

_PATH_RE = re.compile(
    r"([MmZzLlHhVvCcSsQqTtAa])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
)
_CMD_CHARS = set("MmZzLlHhVvCcSsQqTtAa")


def _tokenise_path(d: str) -> list[str]:
    return [m.group() for m in _PATH_RE.finditer(d)]


def _is_cmd(token: str) -> bool:
    return token in _CMD_CHARS


# ── Rectangle path helper ─────────────────────────────────────────────────────

def _rect_path(
    x0: int, y0: int, x1: int, y1: int, w: int, h: int
) -> list[tuple[float, float]]:
    """Return a CCW unit-normalised rectangle polygon."""
    return [
        (x0/w, y0/h),
        (x1/w, y0/h),
        (x1/w, y1/h),
        (x0/w, y1/h),
        (x0/w, y0/h),
    ]
