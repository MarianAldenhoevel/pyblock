"""
stl_builder.py — Geometry builder and STL file writer for pyBlock.

Takes a list of 2-D paths (in [0,1]×[0,1] normalised space) and produces
a single solid STL body consisting of:

  1. A solid rectangular base plate.
  2. A relief layer extruded from (or into) the top surface of the plate,
     corresponding to the input paths.

The STL is written as either binary (default, compact) or ASCII.

Coordinate system
-----------------
  X — plate width   (0 … plate_width_mm)
  Y — plate height  (0 … plate_height_mm)
  Z — vertical      (0 = bottom of plate, plate_thickness_mm = top of plate)

Relief raised above top surface: z from plate_thickness_mm
                                         to plate_thickness_mm + relief_height_mm
Relief embossed into plate surface (relief_height_mm < 0):
                                       z from plate_thickness_mm + relief_height_mm
                                                to plate_thickness_mm
"""

from __future__ import annotations

import logging
import math
import struct
from typing import Sequence

log = logging.getLogger("pyblock.stl")

# Type aliases
Point2 = tuple[float, float]
Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]

# A Shape is an outer contour with zero or more holes.
# All contours are lists of (x, y) points in normalised [0,1] space.
# Outer contour: CCW winding (positive signed area)
# Hole contours: CW winding  (negative signed area) — they cut into the outer
Shape = tuple[list[Point2], list[list[Point2]]]  # (outer, [hole, ...])


class STLBuilder:
    def __init__(
        self,
        plate_width_mm: float,
        plate_height_mm: float,
        plate_thickness_mm: float,
        relief_height_mm: float,
        min_line_width_mm: float,
        binary: bool = True,
        curve_segments: int = 16,
    ) -> None:
        self.pw  = plate_width_mm
        self.ph  = plate_height_mm
        self.pt  = plate_thickness_mm
        self.rh  = relief_height_mm
        self.lw  = min_line_width_mm
        self.bin = binary
        self.cs  = curve_segments

        # Z levels
        if relief_height_mm >= 0:
            self.z_plate_top    = plate_thickness_mm
            self.z_relief_top   = plate_thickness_mm + relief_height_mm
        else:
            # Emboss: relief goes below plate surface
            self.z_plate_top    = plate_thickness_mm
            self.z_relief_top   = plate_thickness_mm + relief_height_mm  # < z_plate_top

    def build_and_write(
        self,
        shapes: list[Shape],
        out_path: str,
    ) -> int:
        """
        Build geometry from *shapes* and write to *out_path*.
        Each shape is (outer_contour, [hole_contour, ...]).
        Returns the total number of triangles written.
        """
        triangles: list[Triangle] = []

        # ── Base plate ────────────────────────────────────────────────────────
        log.info("  Building base plate…")
        triangles.extend(self._base_plate())
        log.debug("    Base plate: %d triangles so far.", len(triangles))

        # ── Relief geometry ───────────────────────────────────────────────────
        if shapes and self.rh != 0.0:
            log.info("  Extruding %d shape(s) into relief layer…", len(shapes))
            for idx, (outer, holes) in enumerate(shapes):
                if len(outer) < 3:
                    continue
                tris = self._extrude_shape(outer, holes)
                triangles.extend(tris)
                if (idx + 1) % 100 == 0:
                    log.debug("    … %d / %d shapes processed.", idx+1, len(shapes))
            log.debug("    After relief: %d triangles.", len(triangles))

        # ── Write STL ─────────────────────────────────────────────────────────
        log.info("  Writing STL to '%s' (%s)…", out_path,
                 "binary" if self.bin else "ASCII")
        if self.bin:
            _write_binary_stl(triangles, out_path)
        else:
            _write_ascii_stl(triangles, out_path, name="pyblock_relief")

        return len(triangles)

    # ── Base plate ────────────────────────────────────────────────────────────

    def _base_plate(self) -> list[Triangle]:
        """Return triangles for the solid rectangular base plate."""
        w, h, z0, zt = self.pw, self.ph, 0.0, self.z_plate_top
        tris: list[Triangle] = []

        if self.rh >= 0:
            # Standard case: plate top is the background, relief sits above
            top_z = zt
        else:
            # Emboss case: plate top sits at zt, relief pockets go below
            top_z = zt

        # Bottom face (z = 0), normal pointing down
        tris += _quad(
            (0, 0, 0), (w, 0, 0), (w, h, 0), (0, h, 0),
            flip=True,
        )
        # Top face (z = top_z), normal pointing up
        tris += _quad(
            (0, 0, top_z), (w, 0, top_z), (w, h, top_z), (0, h, top_z),
        )
        # Front face (y = 0)
        tris += _quad(
            (0, 0, 0), (w, 0, 0), (w, 0, top_z), (0, 0, top_z),
            flip=True,
        )
        # Back face (y = h)
        tris += _quad(
            (0, h, 0), (w, h, 0), (w, h, top_z), (0, h, top_z),
        )
        # Left face (x = 0)
        tris += _quad(
            (0, 0, 0), (0, h, 0), (0, h, top_z), (0, 0, top_z),
        )
        # Right face (x = w)
        tris += _quad(
            (w, 0, 0), (w, h, 0), (w, h, top_z), (w, 0, top_z),
            flip=True,
        )
        return tris

    # ── Shape extrusion (outer contour + holes) ──────────────────────────────

    def _extrude_shape(self, outer: list[Point2], holes: list[list[Point2]]) -> list[Triangle]:
        """
        Extrude a shape (outer polygon + optional holes) into a 3-D solid
        between z_plate_top and z_relief_top.

        The outer contour and each hole each get side-wall quads.
        The top and bottom caps are triangulated with holes via mapbox_earcut,
        so the hole regions are open (not capped) — they appear as pockets
        or pass-throughs depending on the relief direction.
        """
        tris: list[Triangle] = []

        def to_mm(pts):
            closed = list(pts)
            if closed and closed[0] != closed[-1]:
                closed.append(closed[0])
            return [(x * self.pw, y * self.ph) for x, y in closed]

        outer_mm = to_mm(outer)
        holes_mm = [to_mm(h) for h in holes]

        z_bot = self.z_plate_top
        z_top = self.z_relief_top

        if z_bot == z_top:
            return []

        # For embossed (rh < 0): z_top < z_bot.  Swap so z_bot < z_top always.
        # flip_cap tracks whether to reverse the face normals for the caps.
        if z_top < z_bot:
            z_bot, z_top = z_top, z_bot
            flip_cap = True
        else:
            flip_cap = False

        # ── Caps (top and bottom) with holes ─────────────────────────────────
        cap_tris = _earcut_with_holes(outer_mm, holes_mm)
        for a, b, c in cap_tris:
            if flip_cap:
                # top cap (normals up) and bottom cap (normals down) are swapped
                tris.append((_p3(a, z_top), _p3(c, z_top), _p3(b, z_top)))
                tris.append((_p3(a, z_bot), _p3(b, z_bot), _p3(c, z_bot)))
            else:
                tris.append((_p3(a, z_top), _p3(b, z_top), _p3(c, z_top)))
                tris.append((_p3(a, z_bot), _p3(c, z_bot), _p3(b, z_bot)))

        # ── Side walls for outer contour ──────────────────────────────────────
        _add_walls(tris, outer_mm, z_bot, z_top, flip=False)

        # ── Side walls for each hole (winding reversed so normals point inward) ─
        for hole_mm in holes_mm:
            _add_walls(tris, hole_mm, z_bot, z_top, flip=True)

        return tris



# ── Polygon-with-holes triangulation via mapbox_earcut ───────────────────────

def _earcut_with_holes(
    outer_mm: list[Point2],
    holes_mm: list[list[Point2]],
) -> list[tuple[Point2, Point2, Point2]]:
    """
    Triangulate a polygon that may have holes using mapbox_earcut.

    outer_mm  — outer contour (closed, i.e. first == last point)
    holes_mm  — list of hole contours (each closed)

    Returns a list of (a, b, c) vertex triples in mm coordinates.
    """
    import numpy as np
    try:
        import mapbox_earcut as earcut
    except ImportError:
        raise ImportError(
            "mapbox_earcut is required for polygon-with-holes triangulation.\n"
            "Install it with:  pip install mapbox_earcut"
        )

    # Build flat vertex array and hole-start index list for earcut
    # earcut expects: flat [x0,y0, x1,y1, ...] + list of hole start indices
    all_verts: list[Point2] = []
    ring_ends: list[int] = []          # earcut wants the END index of each ring

    # Outer contour — drop closing duplicate
    outer_open = outer_mm[:-1] if (outer_mm and outer_mm[0] == outer_mm[-1]) else outer_mm
    # Ensure CCW (positive area) for correct earcut winding
    if _signed_area(outer_open) < 0:
        outer_open = outer_open[::-1]
    all_verts.extend(outer_open)
    ring_ends.append(len(all_verts))   # end of outer ring

    for hole in holes_mm:
        hole_open = hole[:-1] if (hole and hole[0] == hole[-1]) else hole
        # Holes must be CW (negative area) for earcut
        if _signed_area(hole_open) > 0:
            hole_open = hole_open[::-1]
        all_verts.extend(hole_open)
        ring_ends.append(len(all_verts))   # end of this hole ring

    if len(all_verts) < 3:
        return []

    verts_np = np.array(all_verts, dtype=np.float64)          # shape (n, 2)
    rings_np = np.array(ring_ends,  dtype=np.uint32)           # end index per ring
    indices  = earcut.triangulate_float64(verts_np, rings_np)

    tris = []
    for k in range(0, len(indices), 3):
        a = all_verts[indices[k]]
        b = all_verts[indices[k+1]]
        c = all_verts[indices[k+2]]
        tris.append((a, b, c))
    return tris


def _signed_area(pts: list[Point2]) -> float:
    n = len(pts)
    area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def _add_walls(
    tris: list[Triangle],
    contour_mm: list[Point2],
    z_bot: float,
    z_top: float,
    flip: bool,
) -> None:
    """Add side-wall quads for a single closed contour."""
    n = len(contour_mm)
    for k in range(n - 1):
        p0 = contour_mm[k]
        p1 = contour_mm[k + 1]
        tris += _quad(
            _p3(p0, z_bot), _p3(p1, z_bot),
            _p3(p1, z_top), _p3(p0, z_top),
            flip=flip,
        )


# ── Quad helper ───────────────────────────────────────────────────────────────

def _quad(
    a: Point3, b: Point3, c: Point3, d: Point3,
    flip: bool = False,
) -> list[Triangle]:
    """Split a quad (a, b, c, d) into two triangles, optionally flipping normals."""
    if flip:
        return [(a, c, b), (a, d, c)]
    return [(a, b, c), (a, c, d)]


def _p3(p2: Point2, z: float) -> Point3:
    return (p2[0], p2[1], z)


# ── Normal calculation ────────────────────────────────────────────────────────

def _normal(t: Triangle) -> Point3:
    a, b, c = t
    ax, ay, az = b[0]-a[0], b[1]-a[1], b[2]-a[2]
    bx, by, bz = c[0]-a[0], c[1]-a[1], c[2]-a[2]
    nx = ay*bz - az*by
    ny = az*bx - ax*bz
    nz = ax*by - ay*bx
    mag = math.sqrt(nx*nx + ny*ny + nz*nz)
    if mag == 0:
        return (0.0, 0.0, 1.0)
    return (nx/mag, ny/mag, nz/mag)


# ── STL writers ───────────────────────────────────────────────────────────────

def _write_binary_stl(triangles: list[Triangle], path: str) -> None:
    """Write triangles as a binary STL file (80-byte header + triangle records)."""
    header = b"pyBlock STL output" + b" " * (80 - 18)
    with open(path, "wb") as f:
        f.write(header)
        f.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            nx, ny, nz = _normal(tri)
            f.write(struct.pack("<fff", nx, ny, nz))
            for vx, vy, vz in tri:
                f.write(struct.pack("<fff", float(vx), float(vy), float(vz)))
            f.write(b"\x00\x00")  # attribute byte count


def _write_ascii_stl(
    triangles: list[Triangle], path: str, name: str = "solid"
) -> None:
    """Write triangles as an ASCII STL file."""
    with open(path, "w", encoding="ascii") as f:
        f.write(f"solid {name}\n")
        for tri in triangles:
            nx, ny, nz = _normal(tri)
            f.write(f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}\n")
            f.write("    outer loop\n")
            for vx, vy, vz in tri:
                f.write(f"      vertex {vx:.6e} {vy:.6e} {vz:.6e}\n")
            f.write("    endloop\n")
            f.write("  endfacet\n")
        f.write(f"endsolid {name}\n")
