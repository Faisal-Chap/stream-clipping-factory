"""Whole-recording coarse scan and denser shortlist evidence."""

import json
import math
import statistics
from array import array

from .media import MediaError, require, run
from .semantic import ClipRetriever, availability, model_fingerprint

SCAN_VERSION = "coarse-3s-v1"
RANK_VERSION = "moments-v2"
STEP, SIZE = 3, 96
FRAME_BYTES = SIZE * SIZE * 3


def version(method):
    if method == "semantic":
        if not availability():
            raise MediaError("CLIP setup required. Install the local model or select Baseline in Settings.")
        return f"{SCAN_VERSION}:{RANK_VERSION}:clip-{model_fingerprint()}"
    return f"{SCAN_VERSION}:{RANK_VERSION}:baseline"


def ffmpeg(master, options, timeout=3600):
    result = run([require("ffmpeg"), "-nostdin", "-v", "error", "-y",
                  "-protocol_whitelist", "file", "-threads", "4", *options[:options.index("-i")],
                  "-i", str(master), *options[options.index("-i") + 1:]], timeout)
    if result.returncode:
        raise MediaError("FFmpeg could not decode or render this media.")
    return result.stdout


def atomic_json(path, value):
    temp = path.with_suffix(".partial")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def coarse_scan(root, asset, update):
    cache = root / "analysis" / asset["hash"] / SCAN_VERSION
    cache.mkdir(parents=True, exist_ok=True)
    frames = cache / "frames.rgb"
    index = cache / "timeline.json"
    if frames.is_file() and index.is_file():
        try:
            timeline = json.loads(index.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        else:
            if frames.stat().st_size == len(timeline) * FRAME_BYTES:
                return frames, timeline
    master = root / asset["path"]
    update("Sampling the entire recording every 3 seconds")
    partial = cache / "frames.partial"
    ffmpeg(master, ["-i", "-map", "0:V:0", "-an", "-filter_threads", "2",
                    "-vf", f"fps=1/{STEP}:round=up,scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease,"
                           f"pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-pix_fmt", "rgb24", "-c:v", "rawvideo", "-threads", "1", "-f", "rawvideo", str(partial)])
    timeline = []
    previous = None
    with partial.open("rb") as stream:
        while frame := stream.read(FRAME_BYTES):
            if len(frame) != FRAME_BYTES:
                raise MediaError("The coarse frame cache is incomplete.")
            sample = frame[::24]
            change = (sum(abs(a - b) for a, b in zip(sample, previous)) / len(sample) / 255
                      if previous else 0)
            timeline.append({"t": len(timeline) * STEP, "change": round(change, 4), "audio": 0})
            previous = sample
    if asset["audio"]:
        update("Measuring source audio across the timeline")
        sound = cache / "audio.partial"
        ffmpeg(master, ["-i", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "2000",
                        "-f", "s16le", str(sound)])
        with sound.open("rb") as stream:
            for sample in timeline:
                pcm = array("h", stream.read(2000 * STEP * 2))
                if pcm:
                    sample["audio"] = round(math.sqrt(sum(v*v for v in pcm) / len(pcm)) / 32768, 4)
        sound.unlink()
    partial.replace(frames)
    atomic_json(index, timeline)
    return frames, timeline


def add_semantics(frames, timeline, update):
    retriever = ClipRetriever()
    with frames.open("rb") as stream:
        for offset in range(0, len(timeline), 24):
            samples = timeline[offset:offset+24:3]
            batch = []
            for sample in samples:
                stream.seek(sample["t"] // STEP * FRAME_BYTES)
                batch.append(stream.read(FRAME_BYTES))
            for sample, (label, margin) in zip(samples, retriever.labels(b"".join(batch), SIZE, SIZE), strict=True):
                sample.update(label=label, margin=round(margin, 4))
            update(f"Local CLIP · {min(offset + 24, len(timeline))}/{len(timeline)} timeline samples covered")


def blackout_times(frames, timeline):
    # The central region excludes letterbox bars and most corner overlays.
    pixels = [(y * SIZE + x) * 3 for y in range(28, 67, 3) for x in range(24, 76, 3)]
    blackouts = []
    with frames.open("rb") as stream:
        for sample in timeline:
            frame = stream.read(FRAME_BYTES)
            if sum(max(frame[i:i+3]) > 26 for i in pixels) < .04 * len(pixels):
                blackouts.append(sample["t"])
    return blackouts


def evidence_window(time, duration, blackouts):
    start, end = max(0, time - 12), min(duration, time + 9)
    if time in blackouts:
        return start, end, "The signal is a blackout or loading transition."
    previous = next((cut for cut in reversed(blackouts) if start <= cut < time), None)
    following = next((cut for cut in blackouts if time < cut < end), None)
    if previous is not None:
        start = previous + STEP
    if following is not None:
        end = following
    if time - start < 6 or end - time < 3:
        return start, end, "A blackout leaves too little continuous setup or payoff."
    return start, end, ""


def rank(timeline, duration, method, blackouts=()):
    if not timeline:
        return []
    audio_floor = statistics.median(sample["audio"] for sample in timeline)
    candidates = []
    for index, sample in enumerate(timeline):
        time = sample["t"]
        if time < 6 or duration - time < 3:
            continue
        motion = sample["change"]
        sound = max(0, sample["audio"] - audio_floor)
        near = [s for s in timeline[max(0, index-3):index+4] if "label" in s]
        tags = list(dict.fromkeys(s["label"] for s in near))
        semantic = sum(max(0, s["margin"]) for s in near if s["label"] in
                       ("vehicle damage", "on-foot combat", "aircraft", "conversation"))
        score = 4 * motion + 3 * sound
        if method == "semantic":
            score += min(.30, semantic * 5) + (.06 if len(tags) > 1 else 0)
            if tags == ["interface"]:
                score -= .30
        start, end, reason = evidence_window(time, duration, blackouts)
        if not reason and motion < .017 and sound < .045:
            reason = "Little visual change or audio rise in the evidence window."
        elif not reason and score < .19:
            reason = "Below the pilot evidence threshold."
        candidates.append(dict(start=start, end=round(end, 3),
                               time=time, score=round(score, 3), change=motion, audio=sample["audio"],
                               tags=tags, labels=near, rejection=reason))
    return sorted(candidates, key=lambda c: (-c["score"], c["time"]))


def select(candidates, limit):
    selected, rejected = [], []
    # Separate windows by their boundaries, not merely by distance between peak timestamps.
    for candidate in candidates:
        item = candidate.copy()
        if not item["rejection"] and any(item["start"] < other["end"] + 3
                                        and item["end"] + 3 > other["start"] for other in selected):
            item["rejection"] = "Overlaps a higher-ranked candidate."
        if not item["rejection"] and len(selected) >= limit:
            item["rejection"] = "Below the preview limit for this run."
        if item["rejection"]:
            rejected.append(item)
        else:
            selected.append(item)
    return selected, rejected


def analyze(root, asset, method, analysis_version, update):
    frames, timeline = coarse_scan(root, asset, update)
    blackouts = blackout_times(frames, timeline)
    if method == "semantic" and timeline:
        add_semantics(frames, timeline, update)
    result = {"method": method, "version": analysis_version, "samples": len(timeline),
              "ranked": rank(timeline, asset["duration"], method, blackouts)}
    if not result["ranked"]:
        result["empty_reason"] = "No window with room for a setup and payoff."
    return result
