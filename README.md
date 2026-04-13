# pyBlock

**pyBlock** converts raster images (JPEG, PNG, ) or SVG files into STL files
ready for 3D printing. The output is a solid base plate with an embossed or
raised relief of the input image - a block print that can stamp a design onto
paper, fabric, or clay.

Much of the credit for the meat of this program goes to Claude.ai. Still I feel
responsible for any mistakes.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | |
| [Pillow](https://python-pillow.org/) | ≥ 10.0 | `pip install Pillow` |
| [potrace](https://potrace.sourceforge.net/) | ≥ 1.16 | OS package - default vectorizer |
| [vtracer](https://github.com/visioncortex/vtracer) | any | Optional alternative vectorizer |

Install potrace:

```bash
# Ubuntu / Debian
sudo apt install potrace

# macOS
brew install potrace

# Windows - download the binary from https://potrace.sourceforge.net/
# and place potrace.exe somewhere on your PATH or just next to pyblock.py.
```
