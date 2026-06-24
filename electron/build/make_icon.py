"""Generate the app icon FROM THE REAL LOGO RASTER (frontend/assets/logo.png) — never an
SVG/redraw. Crops the metallic spine mark (left of the wordmark), centers it on its own navy
background by the mark's bright-pixel bounding box, and writes a multi-resolution Windows .ico
plus a 1024 .png (for macOS .icns / Linux later).

Run:  cd electron/build && python make_icon.py
"""
from pathlib import Path
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "frontend" / "assets" / "logo.png"
OUT_ICO = Path(__file__).resolve().parent / "icon.ico"
OUT_PNG = Path(__file__).resolve().parent / "icon.png"

WORDMARK_X = 600   # helix right edge ~580; cut before the "MIKA" wordmark (~680) so it can't leak in


def main():
    im = Image.open(SRC).convert("RGB")
    left = im.crop((0, 0, WORDMARK_X, im.height))           # mark only, no wordmark
    bg = im.getpixel((4, 4))                                 # the logo's own navy

    # bounding box of the bright mark vs the dark navy background
    gray = np.array(left.convert("L"))
    ys, xs = np.where(gray > 55)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    half = max(x1 - x0, y1 - y0) / 2.0 * 1.18                # 18% breathing room
    side = int(half * 2)

    canvas = Image.new("RGB", (side, side), bg)             # square navy plate
    canvas.paste(left, (int(side / 2 - cx), int(side / 2 - cy)))
    icon = canvas.resize((1024, 1024), Image.LANCZOS)

    icon.save(OUT_PNG)
    icon.save(OUT_ICO, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"mark bbox in left-crop: x[{x0},{x1}] y[{y0},{y1}]  square side={side}")
    print(f"wrote {OUT_ICO} and {OUT_PNG}")


if __name__ == "__main__":
    main()
