#!/usr/bin/env python3
"""Regenerate the README demo GIF from the published source recording.

    python docs/render-dashboard-gif.py [path/to/hermes-talk-dashboard-cut.mp4]

Requires ffmpeg on PATH. Downloads the source capture from the v0.3.0 release
if it is not already beside this script.

The GIF is the first 40 seconds of that recording at 8x speed, which is what
the README caption claims. The source is 1280x582; the GIF is rendered at that
same native size rather than downscaled, because the demo's whole point is the
text on screen -- the transcript, and the agent's brief in the runs panel. The
previous GIF was a 640x291 downscale, which cost every URL and body line in
the runs panel (issue #79).

Palette notes: 64 colours with a bayer dither and rectangle diff mode. The UI
is flat dark chrome over a grainy background, which is the easy case for a
small palette -- 64 and 128 colours are indistinguishable here even at full
size, and 64 saves about 1.2 MB. Bayer compresses far better than an
error-diffusion dither, and diff_mode=rectangle exploits the static tail of
the clip.
"""

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

DOCS = Path(__file__).resolve().parent
TARGET = DOCS / "dashboard.gif"
# Cached outside the repo: the source clip is ~7.6 MB and is a release asset,
# not something this tree should carry a second copy of.
CACHED_SOURCE = Path(tempfile.gettempdir()) / "hermes-talk-dashboard-cut.mp4"
SOURCE_URL = (
    "https://github.com/TheSmokeDev/hermes-talk/releases/download/"
    "v0.3.0/hermes-talk-dashboard-cut.mp4"
)

SECONDS = 40  # of source footage
SPEEDUP = 8  # matches the README caption
FPS = 10  # playback frame rate
COLORS = 64

# Speed the clip up first, then resample to the playback rate. Doing it in this
# order is what makes the output exactly SECONDS / SPEEDUP long; sampling first
# and restamping afterwards left ffmpeg padding the tail with duplicate frames.
SPEED_FILTER = f"setpts=PTS/{SPEEDUP},fps={FPS}"


def run(args: list[str]) -> None:
    subprocess.run(args, check=True)


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg not found on PATH")

    source = Path(sys.argv[1]) if len(sys.argv) > 1 else CACHED_SOURCE
    if not source.exists():
        print(f"downloading {SOURCE_URL} -> {source}")
        urllib.request.urlretrieve(SOURCE_URL, source)

    palette = DOCS / "dashboard-palette.png"
    run([
        ffmpeg, "-v", "error", "-t", str(SECONDS), "-i", str(source),
        "-vf", f"{SPEED_FILTER},palettegen=max_colors={COLORS}:stats_mode=diff",
        "-y", str(palette),
    ])
    run([
        ffmpeg, "-v", "error", "-t", str(SECONDS), "-i", str(source), "-i", str(palette),
        "-lavfi",
        f"{SPEED_FILTER}[x];"
        "[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
        "-y", str(TARGET),
    ])
    palette.unlink()

    size_mb = TARGET.stat().st_size / 1_000_000
    print(f"wrote {TARGET.name}: {size_mb:.2f} MB")
    if size_mb > 8:
        raise SystemExit(f"{TARGET.name} is {size_mb:.2f} MB; drop COLORS or SECONDS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
