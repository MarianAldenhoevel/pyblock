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
                
                exit

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
    ) -> list[list[tuple[float, float]]]:
        log.info("Vectorising with vtracer…")
        with tempfile.TemporaryDirectory() as tmp:
            png_path = os.path.join(tmp, "input.png")
            svg_path = os.path.join(tmp, "output.svg")

            img_1bit.convert("RGB").save(png_path)

            cmd = [
                "vtracer",
                "--input", png_path,
                "--output", svg_path,
                "--colormode", "binary",
                f"--color_precision={self.vtracer_color_precision}",
                f"--filter_speckle={self.vtracer_filter_speckle}",
            ]
            log.debug("  Command: %s", " ".join(cmd))
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError("vtracer timed out after 120 s")
            if result.returncode != 0:
                raise RuntimeError(
                    f"vtracer failed (exit {result.returncode}):\n{result.stderr}"
                )

            paths = _parse_svg_paths(svg_path, w, h, self.curve_segments)

        log.info("  vtracer produced %d path(s).", len(paths))
        return paths

    # ── pixel-grid fallback ───────────────────────────────────────────────────

    def _vectorise_pixels(
        self, img_1bit: "Image.Image", w: int, h: int
    ) -> list[tuple[list[tuple[float, float]], list]]:
        """
        Very simple pixel-block approach: each black pixel becomes a unit square
        polygon.  Neighbouring squares are merged into rectangular runs per row.
        Raster rectangles never have holes, so each is wrapped as (outer, []).
        """
        log.info("  Building pixel-rect geometry (%d×%d)…", w, h)
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

        log.info("  Pixel-grid extraction produced %d rectangle(s).", len(shapes))
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


def _group_sub_paths(
    sub_paths: list[list[tuple[float, float]]],
    svg_w: float, svg_h: float,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    """
    Group a list of sub-paths (from a single SVG <path> element) into
    (outer, [holes]) Shape tuples using winding-order classification.

    SVG uses the even-odd / nonzero fill rule: sub-paths with opposite
    winding to the first (largest) contour are holes.  We determine
    outer vs hole by signed area: the largest-area contour is the outer
    shell; remaining contours whose winding opposes the outer are holes.
    Those that have the same winding as the outer are independent filled
    shapes.

    All coordinates are normalised to [0,1]×[0,1] before returning.
    """
    if not sub_paths:
        return []

    # Normalise and compute signed areas
    normed = []
    for sp in sub_paths:
        # drop closing duplicate for area calculation
        pts = sp[:-1] if (len(sp) > 1 and sp[0] == sp[-1]) else sp
        if len(pts) < 3:
            continue
        norm = [(x / svg_w, y / svg_h) for x, y in pts]
        area = _signed_area_2d(norm)
        normed.append((norm, area))

    if not normed:
        return []

    # Sort by absolute area descending — largest contour first
    normed.sort(key=lambda t: abs(t[1]), reverse=True)

    shapes: list[tuple[list, list]] = []
    used = [False] * len(normed)

    for i, (outer_pts, outer_area) in enumerate(normed):
        if used[i]:
            continue
        used[i] = True
        holes = []
        # Any subsequent contour that has opposite winding AND is geometrically
        # inside the outer is a hole.  We use winding sign as the primary test
        # (matching SVG nonzero fill rule behaviour for well-formed paths).
        for j in range(i + 1, len(normed)):
            if used[j]:
                continue
            inner_pts, inner_area = normed[j]
            # Opposite winding → hole
            if (outer_area > 0) != (inner_area > 0):
                holes.append(inner_pts)
                used[j] = True
        shapes.append((outer_pts, holes))

    return shapes


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
