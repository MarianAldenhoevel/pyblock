"""
utils.py — Shared utilities for pyBlock.
"""

import logging
import shutil
import subprocess
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a clean, coloured console handler."""
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


def print_banner(version: str) -> None:
    """Print the pyBlock startup banner."""
    print(f"pyBlock v{version} — image-to-STL block print converter\n")


# -- Dependency checks ---------------------------------------------------------

def _check_potrace(log: logging.Logger) -> bool:
    """Return True if the 'potrace' binary is available on PATH."""
    path = shutil.which("potrace")
    if path:
        try:
            result = subprocess.run(
                ["potrace", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
            log.debug("Found potrace: %s  (%s)", path, version_line)
        except Exception:
            log.debug("Found potrace at %s (version unknown)", path)
        return True

    log.error(
        "Vectorizer 'potrace' not found on PATH.\n\n"
        "Install it with one of:\n"
        "Ubuntu/Debian : sudo apt install potrace\n"
        "macOS (brew)  : brew install potrace\n"
        "Windows       : https://potrace.sourceforge.net/\n\n"
        "Or choose a different vectorizer with --vectorizer vtracer|none"
    )
    return False

def check_dependencies(vectorizer: str, log: logging.Logger) -> bool:
    """
    Check all external program dependencies required for *vectorizer*.

    Returns True if all required dependencies are present, False otherwise.
    """
    ok = True
    if vectorizer == "potrace":
        ok = _check_potrace(log) and ok
    return ok


# -- Geometry helpers ----------------------------------------------------------

def polygon_area(pts: list[tuple[float, float]]) -> float:
    """Signed area of a 2-D polygon via the shoelace formula."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def ensure_ccw(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return pts in counter-clockwise winding order (positive area)."""
    if polygon_area(pts) < 0:
        return pts[::-1]
    return pts


def ensure_cw(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return pts in clockwise winding order (negative area)."""
    if polygon_area(pts) > 0:
        return pts[::-1]
    return pts
