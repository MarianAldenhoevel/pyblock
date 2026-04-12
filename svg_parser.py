"""
svg_parser.py — Direct SVG file reader for pyBlock.

Handles:
  • <path>               — arbitrary filled or stroked paths
  • <rect>               — rectangles
  • <circle>             — circles
  • <ellipse>            — ellipses
  • <line>               — single line segments (given minimum width → rect)
  • <polyline>           — open polylines (stroked, given min width)
  • <polygon>            — closed filled polygons
  • <g>                  — groups (recursed into, transforms applied)

All shapes — regardless of their fill/stroke colour — are treated as
"active" geometry and contribute to the relief.

Returns paths in *normalised* [0,1]×[0,1] coordinate space.
"""

from __future__ import annotations

import logging
import math
import re
from xml.etree import ElementTree as ET

from image_processor import (
    _tessellate_svg_d,
    _cubic_bezier,
    _quadratic_bezier,
    _arc,
    _group_sub_paths,
    _signed_area_2d,
)

log = logging.getLogger("pyblock.svg")


class SVGParser:
    def __init__(
        self,
        min_line_width_mm: float = 1.0,
        curve_segments: int = 16,
    ) -> None:
        self.min_line_width_mm = min_line_width_mm
        self.curve_segments    = curve_segments

    def parse(
        self, svg_path: str
    ) -> tuple[list[tuple[list, list]], tuple[float, float]]:
        """
        Parse *svg_path* and return:
          (shapes, (doc_width, doc_height))

        Each shape is (outer_contour, [hole_contour, ...]) with all
        coordinates normalised to [0,1] × [0,1].
        doc_width / doc_height are in SVG user units (or px).
        """
        log.info("Parsing SVG: %s", svg_path)
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Strip namespace from tag names for simpler matching
        _strip_ns(root)

        # ── Viewport ──────────────────────────────────────────────────────────
        vb = root.get("viewBox")
        if vb:
            parts = [float(x) for x in vb.replace(",", " ").split()]
            doc_w, doc_h = parts[2], parts[3]
        else:
            doc_w = _parse_length(root.get("width",  "100"))
            doc_h = _parse_length(root.get("height", "100"))

        log.info("  SVG viewport: %.2f × %.2f user units", doc_w, doc_h)

        # Collect raw sub-paths in SVG user-unit space, grouped by element.
        # Each entry is a list of sub-paths from one SVG element.
        grouped_raw: list[list[list[tuple[float, float]]]] = []
        identity = _Transform()
        self._walk(root, identity, grouped_raw, doc_w, doc_h)

        # Classify each group into (outer, holes) shapes
        shapes: list[tuple[list, list]] = []
        for raw_sub_paths in grouped_raw:
            shapes.extend(_group_sub_paths(raw_sub_paths, doc_w, doc_h))

        log.info("  Extracted %d shape(s) from SVG.", len(shapes))
        return shapes, (doc_w, doc_h)

    # ── Element walker ────────────────────────────────────────────────────────

    def _walk(
        self,
        elem: ET.Element,
        transform: "_Transform",
        out: list[list[list[tuple[float, float]]]],
        doc_w: float,
        doc_h: float,
    ) -> None:
        """
        Walk the SVG element tree and append groups of raw sub-paths to *out*.

        Each entry in *out* is a list of sub-paths belonging to one SVG element.
        Sub-paths within the same element are processed together so that holes
        (sub-paths with opposite winding) are correctly identified.

        Simple shapes (rect, circle, etc.) always produce a single outer contour
        with no holes, so they each push a one-element list.

        Compound <path> elements push all their sub-paths as one group so that
        the hole-detection logic in _group_sub_paths() can classify them.
        """
        tag = elem.tag

        # Compose local transform
        local_t = _Transform.from_attr(elem.get("transform", ""))
        t = transform @ local_t

        if tag == "g":
            for child in elem:
                self._walk(child, t, out, doc_w, doc_h)

        elif tag == "path":
            d = elem.get("d", "")
            if d:
                sub = _tessellate_svg_d(d, self.curve_segments)
                group = [[t.apply(x, y) for x, y in pts] for pts in sub if pts]
                if group:
                    out.append(group)   # keep all sub-paths of this element together

        elif tag == "rect":
            x  = float(elem.get("x",      0))
            y  = float(elem.get("y",      0))
            w  = float(elem.get("width",  0))
            h  = float(elem.get("height", 0))
            rx = float(elem.get("rx",     0))
            ry = float(elem.get("ry",     0) or rx)
            pts = _rect_with_rounds(x, y, w, h, rx, ry, self.curve_segments)
            out.append([[t.apply(px, py) for px, py in pts]])

        elif tag == "circle":
            cx_ = float(elem.get("cx", 0))
            cy_ = float(elem.get("cy", 0))
            r   = float(elem.get("r",  0))
            pts = _circle_pts(cx_, cy_, r, self.curve_segments)
            out.append([[t.apply(px, py) for px, py in pts]])

        elif tag == "ellipse":
            cx_ = float(elem.get("cx", 0))
            cy_ = float(elem.get("cy", 0))
            rx_ = float(elem.get("rx", 0))
            ry_ = float(elem.get("ry", 0))
            pts = _ellipse_pts(cx_, cy_, rx_, ry_, self.curve_segments)
            out.append([[t.apply(px, py) for px, py in pts]])

        elif tag == "line":
            # Stroke a line with minimum width
            x1 = float(elem.get("x1", 0)); y1 = float(elem.get("y1", 0))
            x2 = float(elem.get("x2", 0)); y2 = float(elem.get("y2", 0))
            sw = _stroke_width(elem, doc_w) or self.min_line_width_mm
            w  = max(sw, self.min_line_width_mm)
            pts = _stroke_line(x1, y1, x2, y2, w)
            out.append([[t.apply(px, py) for px, py in pts]])

        elif tag == "polyline":
            raw = elem.get("points", "")
            coords = _parse_points(raw)
            sw = _stroke_width(elem, doc_w) or self.min_line_width_mm
            w  = max(sw, self.min_line_width_mm)
            for k in range(len(coords) - 1):
                x1, y1 = coords[k]; x2, y2 = coords[k+1]
                pts = _stroke_line(x1, y1, x2, y2, w)
                out.append([[t.apply(px, py) for px, py in pts]])

        elif tag == "polygon":
            raw = elem.get("points", "")
            coords = _parse_points(raw)
            if coords:
                coords.append(coords[0])  # close
                out.append([[t.apply(px, py) for px, py in coords]])

        else:
            # Recurse into unknown container elements
            for child in elem:
                self._walk(child, t, out, doc_w, doc_h)


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _circle_pts(
    cx: float, cy: float, r: float, n: int
) -> list[tuple[float, float]]:
    pts = []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _ellipse_pts(
    cx: float, cy: float, rx: float, ry: float, n: int
) -> list[tuple[float, float]]:
    pts = []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def _rect_with_rounds(
    x: float, y: float, w: float, h: float,
    rx: float, ry: float, n: int,
) -> list[tuple[float, float]]:
    """Rectangle with optional rounded corners."""
    if rx == 0 and ry == 0:
        return [
            (x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)
        ]
    rx = min(rx, w/2); ry = min(ry, h/2)
    pts: list[tuple[float, float]] = []
    def _arc_pts(cx_, cy_, ax, ay, bx, by, cx2, cy2):
        # Cubic bezier approximation of a quarter circle
        return _cubic_bezier(ax, ay, ax+bx*.552, ay+by*.552,
                              cx2+bx*.552, cy2+by*.552, cx2, cy2, n//4 or 1)[1:]
    # Top edge
    pts.append((x+rx, y))
    pts.append((x+w-rx, y))
    pts.extend(_arc_pts(x+w-rx, y+ry, x+w-rx, y, rx, 0, x+w, y+ry, 0))
    pts.append((x+w, y+h-ry))
    pts.extend(_arc_pts(x+w-rx, y+h-ry, x+w, y+h-ry, 0, ry, x+w-rx, y+h, 0))
    pts.append((x+rx, y+h))
    pts.extend(_arc_pts(x+rx, y+h-ry, x+rx, y+h, -rx, 0, x, y+h-ry, 0))
    pts.append((x, y+ry))
    pts.extend(_arc_pts(x+rx, y+ry, x, y+ry, 0, -ry, x+rx, y, 0))
    pts.append((x+rx, y))
    return pts


def _stroke_line(
    x1: float, y1: float, x2: float, y2: float, width: float
) -> list[tuple[float, float]]:
    """Return a rectangle polygon representing a stroked line segment."""
    dx = x2 - x1; dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        # Degenerate — return a square cap
        hw = width / 2
        return [
            (x1-hw, y1-hw), (x1+hw, y1-hw),
            (x1+hw, y1+hw), (x1-hw, y1+hw),
            (x1-hw, y1-hw),
        ]
    # Perpendicular unit vector
    px = -dy / length * width / 2
    py =  dx / length * width / 2
    return [
        (x1+px, y1+py), (x2+px, y2+py),
        (x2-px, y2-py), (x1-px, y1-py),
        (x1+px, y1+py),
    ]


def _parse_points(raw: str) -> list[tuple[float, float]]:
    nums = [float(v) for v in re.split(r"[\s,]+", raw.strip()) if v]
    return [(nums[k], nums[k+1]) for k in range(0, len(nums)-1, 2)]


def _stroke_width(elem: ET.Element, doc_w: float) -> float:
    """Attempt to read stroke-width from style or attribute."""
    style = elem.get("style", "")
    m = re.search(r"stroke-width\s*:\s*([^;]+)", style)
    val = m.group(1).strip() if m else elem.get("stroke-width", "")
    if val:
        return _parse_length(val)
    return 0.0


_LENGTH_RE = re.compile(r"([\d.eE+-]+)\s*(px|pt|mm|cm|in|%)?")


def _parse_length(s: str) -> float:
    m = _LENGTH_RE.match(s.strip())
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2) or "px"
    # Convert to px (96 dpi assumed)
    conv = {"px": 1, "pt": 1.333, "mm": 3.7795, "cm": 37.795, "in": 96, "%": 1}
    return val * conv.get(unit, 1)


def _strip_ns(elem: ET.Element) -> None:
    """Recursively strip XML namespaces from tag names."""
    elem.tag = elem.tag.split("}")[-1]
    for child in elem:
        _strip_ns(child)


# ── Transform ─────────────────────────────────────────────────────────────────

class _Transform:
    """2-D affine transform stored as (a, b, c, d, e, f) — SVG matrix form."""

    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a=1., b=0., c=0., d=1., e=0., f=0.):
        self.a = a; self.b = b; self.c = c
        self.d = d; self.e = e; self.f = f

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.a*x + self.c*y + self.e,
            self.b*x + self.d*y + self.f,
        )

    def __matmul__(self, other: "_Transform") -> "_Transform":
        a = self.a*other.a + self.c*other.b
        b = self.b*other.a + self.d*other.b
        c = self.a*other.c + self.c*other.d
        d = self.b*other.c + self.d*other.d
        e = self.a*other.e + self.c*other.f + self.e
        f = self.b*other.e + self.d*other.f + self.f
        return _Transform(a, b, c, d, e, f)

    @classmethod
    def from_attr(cls, attr: str) -> "_Transform":
        """Parse a SVG transform attribute into a _Transform."""
        t = cls()
        for fn, args_str in re.findall(r"(\w+)\s*\(([^)]*)\)", attr):
            args = [float(v) for v in re.split(r"[\s,]+", args_str.strip()) if v]
            if fn == "matrix" and len(args) >= 6:
                t = t @ cls(*args[:6])
            elif fn == "translate":
                tx = args[0]; ty = args[1] if len(args) > 1 else 0
                t = t @ cls(1, 0, 0, 1, tx, ty)
            elif fn == "scale":
                sx = args[0]; sy = args[1] if len(args) > 1 else sx
                t = t @ cls(sx, 0, 0, sy, 0, 0)
            elif fn == "rotate":
                a = math.radians(args[0])
                ca, sa = math.cos(a), math.sin(a)
                if len(args) >= 3:
                    cx_, cy_ = args[1], args[2]
                    t = t @ cls(1,0,0,1,cx_,cy_) @ cls(ca,-sa,sa,ca,0,0) @ cls(1,0,0,1,-cx_,-cy_)
                else:
                    t = t @ cls(ca, -sa, sa, ca, 0, 0)
            elif fn == "skewX":
                t = t @ cls(1, 0, math.tan(math.radians(args[0])), 1, 0, 0)
            elif fn == "skewY":
                t = t @ cls(1, math.tan(math.radians(args[0])), 0, 1, 0, 0)
        return t
