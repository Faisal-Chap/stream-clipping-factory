"""Create short synthetic QA inputs with known changes. No GTA material."""

import argparse
import subprocess
from pathlib import Path

from factory.media import tool


def make(directory):
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to generate smoke fixtures.")
    base = [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi"]
    video = ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p"]
    static = "color=0x192724:s=320x180:r=15"
    audio = ("aevalsrc='0.60*sin(2*PI*440*t)*(between(t,18,24)+between(t,45,52)"
             "+between(t,73,81))':s=16000:d=95")
    shot = ("drawbox=x=0:y=0:w=iw:h=ih:t=fill:color={}:enable='between(t,{},{})'")
    motion = ",".join(shot.format(color, a, b) for color, a, b in
                      (("red", 18, 24), ("cyan", 45, 52), ("yellow", 73, 81)))
    subprocess.run([*base, "-i", static, "-f", "lavfi", "-i", audio, "-vf", motion, "-t", "95",
                    *video, "-c:a", "aac", str(directory / "synthetic-signal-events.mp4")], check=True)
    subprocess.run([*base, "-i", static, "-t", "35", *video, str(directory / "synthetic-static.mp4")], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    make(parser.parse_args().directory)
