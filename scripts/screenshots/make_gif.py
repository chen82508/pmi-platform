"""Assemble the captured frames into the README walkthrough GIF.

Usage: python make_gif.py <frames dir> <out.gif> [width]
Frames carry their own display duration (frames.json from capture.mjs).
"""

import json
import sys
from pathlib import Path

from PIL import Image

frames_dir, out = Path(sys.argv[1]), sys.argv[2]
width = int(sys.argv[3]) if len(sys.argv) > 3 else 960

imgs, durations = [], []
for path, ms in json.loads((frames_dir / "frames.json").read_text()):
    im = Image.open(path).convert("RGB")
    im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    # 256 colours keep the hero gradient from banding; no dither keeps text crisp.
    imgs.append(im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
    durations.append(ms)
imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=durations, loop=0, optimize=True, disposal=1)
print(f"{len(imgs)} frames, {sum(durations) / 1000:.1f}s -> {out}")
