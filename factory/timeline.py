"""Timed captions, overlapping source windows and cached ordered evidence."""

import json
import re
from pathlib import Path

from . import analysis
from .media import MediaError

VERSION = "context-media-v1"
WINDOW, STRIDE = 24, 12
STAMP = r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})"


def timestamp(match):
    hours, minutes, seconds, milliseconds = (int(x or 0) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def parse_captions(text, duration):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.splitlines()
        for idx, line in enumerate(lines):
            stamps = list(re.finditer(STAMP, line))
            if len(stamps) != 2 or "-->" not in line:
                continue
            start, end = [timestamp(match) for match in stamps]
            label = re.sub(r"<[^>]*>", "", " ".join(lines[idx+1:])).strip()[:400]
            if label and 0 <= start < end and start < duration:
                cues.append({"start": start, "end": min(end, duration), "text": label})
            break
    return sorted(cues, key=lambda cue: cue["start"])


def captions(root, asset, snapshot):
    master = root / asset["path"]
    location = Path(snapshot.get("location") or snapshot.get("link") or "")
    for suffix in (".vtt", ".srt"):
        if not location.is_absolute():
            break
        sidecar = location.with_suffix(suffix)
        if sidecar.is_file() and sidecar.stat().st_size < 2 * 1024**2:
            cues = parse_captions(sidecar.read_text(encoding="utf-8-sig", errors="replace"), asset["duration"])
            if cues:
                return cues, "Attached source captions · accuracy unverified"
    try:
        text = analysis.ffmpeg(master, ["-i", "-map", "0:s:0", "-f", "webvtt", "pipe:1"], 45).decode(errors="replace")
    except MediaError:
        return [], ("Speech unverified · audio energy only" if asset["audio"] else "No audio stream")
    cues = parse_captions(text, asset["duration"])
    return cues, "Embedded captions · accuracy unverified" if cues else "Speech unverified · no usable captions"


def windows(duration):
    if duration < 8:
        return []
    return [(start, round(min(start + WINDOW, duration), 3))
            for start in range(0, int(duration), STRIDE) if duration - start >= 8]


def interval_cues(cues, start, end):
    return [cue for cue in cues if cue["end"] > start and cue["start"] < end]


def evidence(root, asset, start, end, cues):
    folder = root / "analysis" / asset["hash"] / VERSION / f"{round(start*1000)}-{round(end*1000)}"
    folder.mkdir(parents=True, exist_ok=True)
    master = root / asset["path"]
    result = []
    for index in range(9):
        at = round(start + max(0, end - start - .5) * index / 8, 3)
        path = folder / f"frame-{index}.jpg"
        if not path.is_file():
            analysis.ffmpeg(master, ["-ss", str(at), "-i", "-map", "0:V:0", "-frames:v", "1",
                                     "-vf", "scale=480:-2", "-update", "1", str(path)], 60)
        if path.is_file():
            result.append({"id": f"f{index}", "time": at, "file": path.relative_to(root).as_posix()})
    speech = [dict(cue, id=f"c{index}") for index, cue in enumerate(interval_cues(cues, start, end))]
    analysis.atomic_json(folder / "evidence.json", {"frames": result, "captions": speech})
    return result, speech


def video_segment(root, asset, start, end):
    folder = root / "analysis" / asset["hash"] / VERSION
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"embedding-{round(start*1000)}-{round(end*1000)}.mp4"
    if not path.exists():
        partial = path.with_suffix(".partial")
        analysis.ffmpeg(root / asset["path"], ["-ss", str(start), "-i", "-t", str(end-start), "-map", "0:V:0",
                        "-an", "-vf", "fps=1,scale=320:-2", "-c:v", "libx264", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-f", "mp4", str(partial)], 80)
        partial.replace(path)
    return path.read_bytes()
