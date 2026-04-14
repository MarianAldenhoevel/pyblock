import logging
import math
import re
from xml.etree import ElementTree as ET

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
        coordinates normalised to [0,1] x [0,1].
        doc_width / doc_height are in SVG user units (or px).

        Z-order and color are fully respected:
          - Elements are processed in document order (later = on top).
          - Elements whose visible color matches the document background color
            are treated as ERASERS: their geometry is subtracted from whatever
            has been drawn below them.
          - All other elements ADD their geometry to the canvas.

        This allows effects like a white stroke cutting a black filled shape
        into separate pieces, matching the visual SVG appearance exactly.
        """
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely import unary_union

        log.info("Parsing SVG: %s", svg_path)
        tree = ET.parse(svg_path)
        root = tree.getroot()
        _strip_ns(root)

        # Viewport
        vb = root.get("viewBox")
        if vb:
            parts = [float(x) for x in vb.replace(",", " ").split()]
            doc_w, doc_h = parts[2], parts[3]
        else:
            doc_w = _parse_length(root.get("width",  "100"))
            doc_h = _parse_length(root.get("height", "100"))
        log.info("  SVG viewport: %.2f x %.2f user units", doc_w, doc_h)

        # Background color from sodipodi:namedview pagecolor, default white
        bg_color = _parse_color("#ffffff")
        for child in root:
            if child.tag in ("namedview", "sodipodi:namedview"):
                pc = child.get("pagecolor", "#ffffff")
                bg_color = _parse_color(pc) or bg_color
                break

        log.debug("  Background color: %s", bg_color)

        # Walk the element tree, collecting (shapely_geom, is_subtractive) in z-order
        ops: list[tuple[object, bool]] = []   # (Shapely geometry, subtract?)
        self._walk(root, _Transform(), ops, doc_w, doc_h, bg_color)

        # Accumulate canvas: union additive elements, difference subtractive ones
        canvas = None
        for geom, subtract in ops:
            if geom is None or geom.is_empty:
                continue
            if canvas is None:
                if subtract:
                    log.debug("  Skipping leading subtractive element (nothing to subtract from)")
                    continue
                canvas = geom
            elif subtract:
                canvas = canvas.difference(geom)
            else:
                canvas = canvas.union(geom)

        if canvas is None or canvas.is_empty:
            log.warning("  SVG produced no geometry.")
            return [], (doc_w, doc_h)

        # Convert final canvas to normalised (outer, holes) shapes
        shapes = _shapely_canvas_to_shapes(canvas, doc_w, doc_h)
        log.info("  Extracted %d shape(s) from SVG.", len(shapes))
        return shapes, (doc_w, doc_h)

    # Element walker ----

    def _walk(
        self,
        elem: ET.Element,
        transform: "_Transform",
        ops: list,
        doc_w: float,
        doc_h: float,
        bg_color: tuple,
    ) -> None:
        """
        Walk the SVG element tree and append (shapely_geom, is_subtractive) to *ops*.

        Elements are appended in document order (= z-order, bottom to top).
        An element is subtractive if its primary visible color matches bg_color.
        """
        tag = elem.tag
        local_t = _Transform.from_attr(elem.get("transform", ""))
        t = transform @ local_t

        # Container elements: recurse, don't generate geometry
        if tag in ("g", "svg", "defs", "symbol", "marker", "clipPath", "mask"):
            if tag not in ("defs", "symbol", "marker", "clipPath", "mask"):
                for child in elem:
                    self._walk(child, t, ops, doc_w, doc_h, bg_color)
            return

        # Compute the Shapely geometry for this element and whether it erases
        geom, subtract = self._elem_to_geom(elem, t, doc_w, doc_h, bg_color)
        if geom is not None and not geom.is_empty:
            ops.append((geom, subtract))

    def _elem_to_geom(
        self,
        elem: ET.Element,
        t: "_Transform",
        doc_w: float,
        doc_h: float,
        bg_color: tuple,
    ):
        """
        Convert a single SVG element to a (Shapely geometry, is_subtractive) pair.
        Returns (None, False) if the element has no visible geometry.
        """
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely import unary_union

        tag = elem.tag
        style = _parse_style(elem)

        fill      = style.get("fill", "black")
        stroke    = style.get("stroke", "none")
        sw_raw    = style.get("stroke-width", "")
        linecap   = style.get("stroke-linecap", "butt")
        has_fill  = fill not in ("none", "transparent", "")
        stroke_w  = _parse_length(sw_raw) if sw_raw else 0.0
        if stroke_w > 0:
            stroke_w = max(stroke_w, self.min_line_width_mm)

        fill_color   = _parse_color(fill)   if has_fill  else None
        stroke_color = _parse_color(stroke) if stroke not in ("none", "transparent", "") else None

        # Determine if element is subtractive (erases geometry below it).
        # An element is subtractive if ALL its visible paint matches the background.
        # - A filled+stroked element with both colors == bg -> subtractive
        # - A stroke-only element with stroke color == bg  -> subtractive
        # - A fill-only element with fill color == bg      -> subtractive
        def is_bg(color):
            return color is not None and color == bg_color

        if has_fill and stroke_color:
            subtract = is_bg(fill_color) and is_bg(stroke_color)
        elif has_fill:
            subtract = is_bg(fill_color)
        elif stroke_color:
            subtract = is_bg(stroke_color)
        else:
            return None, False   # invisible

        # Compute Shapely geometry in SVG user-unit space
        geom = self._compute_geom(elem, t, tag, style, fill, has_fill,
                                  stroke_w, linecap, doc_w)
        return geom, subtract

    def _compute_geom(
        self,
        elem: ET.Element,
        t: "_Transform",
        tag: str,
        style: dict,
        fill: str,
        has_fill: bool,
        stroke_w: float,
        linecap: str,
        doc_w: float,
    ):
        """Compute Shapely geometry for an element in SVG user-unit space."""
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely import unary_union

        if tag == "path":
            return self._path_to_geom(elem, t, has_fill, stroke_w, linecap)

        elif tag == "rect":
            x  = float(elem.get("x",     0)); y  = float(elem.get("y",      0))
            w  = float(elem.get("width", 0)); h  = float(elem.get("height",  0))
            rx = float(elem.get("rx",    0)); ry = float(elem.get("ry", 0) or rx)
            pts = [t.apply(px, py) for px, py in _rect_with_rounds(x, y, w, h, rx, ry, self.curve_segments)]
            sw_geom = None
            if stroke_w > 0 and not has_fill:
                from shapely.geometry import LinearRing
                sw_geom = _stroke_path_to_geom(pts[:-1], stroke_w, linecap, closed=True)
            return ShapelyPolygon(pts) if has_fill else sw_geom

        elif tag == "circle":
            cx_ = float(elem.get("cx", 0)); cy_ = float(elem.get("cy", 0))
            r   = float(elem.get("r",  0))
            pts = [t.apply(px, py) for px, py in _circle_pts(cx_, cy_, r, self.curve_segments)]
            return ShapelyPolygon(pts)

        elif tag == "ellipse":
            cx_ = float(elem.get("cx", 0)); cy_ = float(elem.get("cy", 0))
            rx_ = float(elem.get("rx", 0)); ry_ = float(elem.get("ry", 0))
            pts = [t.apply(px, py) for px, py in _ellipse_pts(cx_, cy_, rx_, ry_, self.curve_segments)]
            return ShapelyPolygon(pts)

        elif tag == "line":
            x1 = float(elem.get("x1", 0)); y1 = float(elem.get("y1", 0))
            x2 = float(elem.get("x2", 0)); y2 = float(elem.get("y2", 0))
            sw = max(stroke_w, self.min_line_width_mm)
            pts = [t.apply(px, py) for px, py in _stroke_line(x1, y1, x2, y2, sw)]
            return ShapelyPolygon(pts)

        elif tag == "polyline":
            raw = elem.get("points", "")
            coords = [t.apply(px, py) for px, py in _parse_points(raw)]
            sw = max(stroke_w, self.min_line_width_mm)
            geoms = [_stroke_path_to_geom([coords[k], coords[k+1]], sw, linecap, False)
                     for k in range(len(coords) - 1)]
            from shapely import unary_union
            return unary_union(geoms) if geoms else None

        elif tag == "polygon":
            raw = elem.get("points", "")
            coords = _parse_points(raw)
            if coords:
                pts = [t.apply(px, py) for px, py in coords]
                return ShapelyPolygon(pts)

        return None

    def _path_to_geom(self, elem, t, has_fill, stroke_w, linecap):
        """Convert a <path> element to a single Shapely geometry."""
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely import unary_union

        d = elem.get("d", "")
        if not d:
            return None

        sub_paths = _tessellate_svg_d(d, self.curve_segments)
        parts = []

        for sp in sub_paths:
            sp_t = [t.apply(x, y) for x, y in sp]

            # Degenerate: single point
            is_point = len(sp_t) < 2 or (len(sp_t) == 2 and sp_t[0] == sp_t[-1])
            if is_point:
                r = stroke_w / 2 if stroke_w > 0 else self.min_line_width_mm / 2
                parts.append(_stroke_path_to_geom([sp_t[0]], r * 2, linecap, False))
                continue

            is_closed = (sp_t[0] == sp_t[-1]) and len(sp_t) > 2
            pts_open  = sp_t[:-1] if is_closed else sp_t

            if has_fill and is_closed:
                try:
                    poly = ShapelyPolygon(pts_open)
                    if poly.is_valid and not poly.is_empty:
                        parts.append(poly)
                except Exception:
                    pass
            elif stroke_w > 0:
                parts.append(_stroke_path_to_geom(pts_open, stroke_w, linecap, is_closed))

        if not parts:
            return None
        valid = [p for p in parts if p is not None and not p.is_empty]
        if not valid:
            return None
        if len(valid) == 1:
            return valid[0]
        # Multiple sub-paths: use XOR (symmetric difference) to handle
        # compound paths where inner sub-paths are holes
        result = valid[0]
        for p in valid[1:]:
            result = result.symmetric_difference(p)
        return result

    def _legacy_elem_to_raw(
        self,
        elem: ET.Element,
        t: "_Transform",
        doc_w: float,
        doc_h: float,
    ) -> list[list[tuple[float, float]]]:
        """Legacy path: return raw sub-path lists (used by other element types)."""
        return []


# Geometry helpers ------

def _shapely_to_sub_paths(
    geom,
) -> list[list[tuple[float, float]]]:
    """
    Convert a Shapely geometry (Polygon or MultiPolygon) into a list of
    sub-path point lists.  Exterior rings and interior rings (holes) are
    each returned as a separate sub-path so that _group_sub_paths() can
    classify them by containment and winding.
    """
    results: list[list[tuple[float, float]]] = []
    if geom is None or geom.is_empty:
        return results
    geom_type = geom.geom_type
    if geom_type == "Polygon":
        polys = [geom]
    elif geom_type == "MultiPolygon":
        polys = list(geom.geoms)
    else:
        return results
    for poly in polys:
        if poly.is_empty:
            continue
        results.append(list(poly.exterior.coords[:-1]))
        for hole in poly.interiors:
            results.append(list(hole.coords[:-1]))
    return results


def _parse_color(s: str) -> tuple | None:
    """Parse a CSS color string to an (R, G, B) int tuple, or None."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("none", "transparent"):
        return None
    named = {"white": (255,255,255), "black": (0,0,0), "red": (255,0,0),
             "green": (0,128,0), "blue": (0,0,255), "yellow": (255,255,0),
             "cyan": (0,255,255), "magenta": (255,0,255)}
    if s in named:
        return named[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass
    return None


def _shapely_canvas_to_shapes(
    canvas,
    doc_w: float,
    doc_h: float,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    """
    Convert a Shapely geometry (the accumulated canvas) to a list of
    (outer, [holes]) shape tuples with coordinates normalised to [0,1]x[0,1].

    Shapely polygons explicitly carry exterior and interior rings, so hole
    detection is direct — no winding-order analysis needed.
    """
    shapes = []
    if canvas is None or canvas.is_empty:
        return shapes

    geom_type = canvas.geom_type
    if geom_type == "Polygon":
        polys = [canvas]
    elif geom_type == "MultiPolygon":
        polys = list(canvas.geoms)
    elif geom_type == "GeometryCollection":
        polys = [g for g in canvas.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        expanded = []
        for g in polys:
            if g.geom_type == "MultiPolygon":
                expanded.extend(g.geoms)
            else:
                expanded.append(g)
        polys = expanded
    else:
        return shapes

    for poly in polys:
        if poly.is_empty or not poly.is_valid:
            continue
        outer = [(x / doc_w, y / doc_h) for x, y in poly.exterior.coords[:-1]]
        holes = [
            [(x / doc_w, y / doc_h) for x, y in hole.coords[:-1]]
            for hole in poly.interiors
        ]
        if len(outer) >= 3:
            shapes.append((outer, holes))

    return shapes


def _stroke_path_to_geom(
    pts: list[tuple[float, float]],
    width: float,
    linecap: str,
    closed: bool,
):
    """
    Use Shapely to compute the exact buffered outline of a stroked path.
    Returns a Shapely geometry (Polygon or MultiPolygon).

    cap_style:  1=round, 2=flat/butt, 3=square
    join_style: 1=round, 2=mitre, 3=bevel
    """
    from shapely.geometry import LineString, Point, Polygon as ShapelyPolygon
    cap_map = {"round": 1, "butt": 2, "flat": 2, "square": 3}
    cap_style  = cap_map.get(linecap, 1)
    join_style = 1  # round joins match SVG stroke-linejoin:round default

    r = width / 2
    if len(pts) == 0:
        return None
    if len(pts) == 1:
        return Point(pts[0]).buffer(r, resolution=16)

    if closed:
        # Buffer the closed ring; result typically has an exterior and a hole
        ring_pts = pts + [pts[0]]
        line = LineString(ring_pts)
    else:
        line = LineString(pts)
    return line.buffer(r, cap_style=cap_style, join_style=join_style, resolution=16)


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
        return _cubic_bezier(ax, ay, ax+bx*.552, ay+by*.552,
                              cx2+bx*.552, cy2+by*.552, cx2, cy2, n//4 or 1)[1:]
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
        hw = width / 2
        return [
            (x1-hw, y1-hw), (x1+hw, y1-hw),
            (x1+hw, y1+hw), (x1-hw, y1+hw),
            (x1-hw, y1-hw),
        ]
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


def _parse_style(elem: ET.Element) -> dict[str, str]:
    """Return a dict of CSS property -> value from an element's style attribute."""
    style = elem.get("style", "")
    result: dict[str, str] = {}
    for part in style.split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            result[k.strip()] = v.strip()
    for attr in ("fill", "stroke", "stroke-width", "stroke-linecap",
                 "stroke-linejoin", "stroke-dasharray"):
        if attr not in result and elem.get(attr):
            result[attr] = elem.get(attr)
    return result


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
    # Convert to px (96 dpi assumed); viewBox units are treated as px
    conv = {"px": 1, "pt": 1.333, "mm": 3.7795, "cm": 37.795, "in": 96, "%": 1}
    return val * conv.get(unit, 1)


def _strip_ns(elem: ET.Element) -> None:
    """Recursively strip XML namespaces from tag names."""
    elem.tag = elem.tag.split("}")[-1]
    for child in elem:
        _strip_ns(child)



# Transform -------------

class _Transform:
    """2-D affine transform stored as (a, b, c, d, e, f) - SVG matrix form."""

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




# ── Path tessellation primitives ──────────────────────────────────────────────
# These are imported by svg_parser.py and kept here to avoid circular imports.

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
    cx, cy = 0.0, 0.0
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
                current.append((nx, cy))
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


def _cubic_bezier(x0, y0, x1, y1, x2, y2, x3, y3, n):
    pts = []
    for k in range(n + 1):
        t = k / n; mt = 1 - t
        pts.append((
            mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3,
            mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3,
        ))
    return pts


def _quadratic_bezier(x0, y0, x1, y1, x2, y2, n):
    pts = []
    for k in range(n + 1):
        t = k / n; mt = 1 - t
        pts.append((
            mt**2*x0 + 2*mt*t*x1 + t**2*x2,
            mt**2*y0 + 2*mt*t*y1 + t**2*y2,
        ))
    return pts


def _arc(x1, y1, rx, ry, x_rot_deg, large, sweep, x2, y2, n):
    if rx == 0 or ry == 0:
        return [(x1, y1), (x2, y2)]
    phi = math.radians(x_rot_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx = (x1 - x2) / 2; dy = (y1 - y2) / 2
    x1p =  cos_phi*dx + sin_phi*dy
    y1p = -sin_phi*dx + cos_phi*dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p/rx)**2 + (y1p/ry)**2
    if lam > 1:
        sq = math.sqrt(lam); rx *= sq; ry *= sq
    num = max(0.0, (rx*ry)**2 - (rx*y1p)**2 - (ry*x1p)**2)
    den = (rx*y1p)**2 + (ry*x1p)**2
    sq = math.sqrt(num/den) if den else 0.0
    if large == sweep: sq = -sq
    cxp =  sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx
    cx = cos_phi*cxp - sin_phi*cyp + (x1+x2)/2
    cy = sin_phi*cxp + cos_phi*cyp + (y1+y2)/2

    def _angle(ux, uy, vx, vy):
        n_ = math.sqrt(ux*ux+uy*uy) * math.sqrt(vx*vx+vy*vy)
        if n_ == 0: return 0.0
        c = max(-1.0, min(1.0, (ux*vx+uy*vy)/n_))
        a = math.acos(c)
        if ux*vy - uy*vx < 0: a = -a
        return a

    theta1 = _angle(1, 0, (x1p-cxp)/rx, (y1p-cyp)/ry)
    dtheta = _angle((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)
    if sweep == 0 and dtheta > 0: dtheta -= 2*math.pi
    if sweep == 1 and dtheta < 0: dtheta += 2*math.pi
    pts = []
    for k in range(n + 1):
        t = theta1 + dtheta * k / n
        pts.append((
            cos_phi*rx*math.cos(t) - sin_phi*ry*math.sin(t) + cx,
            sin_phi*rx*math.cos(t) + cos_phi*ry*math.sin(t) + cy,
        ))
    return pts

_PATH_RE = re.compile(
    r"([MmZzLlHhVvCcSsQqTtAa])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
)

def _tokenise_path(d: str) -> list[str]:
    return [m.group() for m in _PATH_RE.finditer(d)]

def _is_cmd(token: str) -> bool:
    return token in set("MmZzLlHhVvCcSsQqTtAa")
