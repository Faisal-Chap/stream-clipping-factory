"""Portrait master, original audio and word-synchronized captions."""

import math
from textwrap import wrap

from . import media
from .streamer_asr import write


def inspect_media(path):
    result = media.run([media.require("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(path)], 35)
    import json
    if result.returncode:
        raise media.MediaError("Could not inspect stream timebases.")
    data = json.loads(result.stdout)
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    return (video["width"], video["height"],
            {stream["codec_type"]: float(stream.get("start_time") or 0) for stream in data["streams"]
             if stream["codec_type"] in ("video", "audio")})


def facecam(master, start, end, override):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, "Gameplay-led · face detector unavailable"
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits, sampled, rail_samples = [], 0, []
    for ratio in (.05, .25, .50, .75, .95):
        stamp = start + (end-start)*ratio
        frame = media.run([media.require("ffmpeg"), "-v", "error", "-ss", f"{stamp:.3f}",
                           "-i", str(master), "-frames:v", "1", "-vf", "scale=640:-2",
                           "-c:v", "mjpeg", "-threads", "1", "-f", "image2pipe", "-"], 22)
        if not frame.stdout:
            continue
        img = cv2.imdecode(np.frombuffer(frame.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        sampled += 1
        h, w = img.shape
        if override:
            x, y, cw, ch = override
            roi = img[round(y*h):round((y+ch)*h), round(x*w):round((x+cw)*w)]
            edge = cv2.Canny(roi, 45, 100)
            rails = np.concatenate((edge[:5].ravel(), edge[-5:].ravel(),
                                    edge[:, :5].ravel(), edge[:, -5:].ravel()))
            rail_samples.append(rails > 0)
            enlarged = cv2.resize(roi, None, fx=2, fy=2)
            if len(detector.detectMultiScale(enlarged, 1.10, 3, minSize=(30, 30))):
                hits.append(override)
            continue
        for x, y, fw, fh in detector.detectMultiScale(img, 1.12, 4, minSize=(24, 24)):
            cx, cy = (x+fw/2)/w, (y+fh/2)/h
            if not .035 <= fw/w <= .18 or not (cx < .29 or cx > .71):
                continue
            width, height = max(.12, fw/w*2.2), max(.15, fh/h*1.8)
            if width > .40 or height > .42:
                continue
            hits.append((min(max(cx-width/2, 0), 1-width), min(max(cy-height*.42, 0), 1-height), width, height))
            break
    if override:
        stable_edges = 0
        if rail_samples:
            reference = rail_samples[0]
            for rails in rail_samples[1:]:
                union = np.count_nonzero(reference | rails)
                stable_edges += union > 30 and np.count_nonzero(reference & rails) / union > .32
        if sampled == 5 and (len(hits) >= 4 or len(hits) >= 2 and stable_edges >= 3):
            return override, f"Operator camera crop · face observed in {len(hits)}/5 frames"
        return None, f"Gameplay-led · face observed in {len(hits)}/5 override samples"
    if len(hits) < 4 or sampled < 5:
        return None, f"Gameplay-led · face observed in {len(hits)}/5 samples"
    typical = tuple(float(np.median([hit[i] for hit in hits])) for i in range(4))
    stable = all(abs(hit[0]-typical[0]) <= .04 and abs(hit[1]-typical[1]) <= .06 for hit in hits)
    return (typical, f"Detected face region · stable in {len(hits)}/5 frames") if stable else (
        None, "Gameplay-led · sampled face region moved")


def ass_time(seconds):
    cs = max(0, round(seconds * 100))
    minutes, remainder = divmod(cs, 6000)
    return f"{minutes//60}:{minutes%60:02}:{remainder//100:02}.{remainder%100:02}"


def ass_escape(value):
    return "".join(" " if char in "{}\\\r\n" else char for char in value)


def make_captions(path, words, candidate, credit):
    start, end = candidate["start"], candidate["end"]
    rows = ["""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,60,&H00FFFFFF,&H00FFFFFF,&H00110F0E,&H00110F0E,-1,0,0,0,100,100,0,0,1,4,0,8,110,162,0,1
Style: Words,Arial,59,&H00FFFFFF,&H00FFFFFF,&H00110F0E,&H00110F0E,-1,0,0,0,100,100,0,0,1,5,1,5,100,176,0,1
Style: Credit,Arial,34,&H00E0E0E0,&H00FFFFFF,&H00110F0E,&H00110F0E,-1,0,0,0,100,100,1,0,1,2,0,5,100,170,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""]

    def event(style, left, right, text):
        if ass_time(right) != ass_time(left):
            rows.append(f"Dialogue: 0,{ass_time(left)},{ass_time(right)},{style},,0,0,0,,{text}")

    hook = r"\N".join(ass_escape(part) for part in wrap(candidate["hook"], width=27))
    event("Hook", 0, min(3.7, end-start), r"{\pos(508,104)}" + hook)
    event("Credit", 0, end-start, r"{\pos(508,1660)}" + ass_escape(credit))
    selected = [word for word in words if start <= word[1] and word[2] <= end+.015]
    groups = []
    group = []
    for word in selected:
        if group and (len(group) >= 5 or len(" ".join(w[3] for w in group+[word])) > 28 or
                      word[1] - group[-1][2] > .42):
            groups.append(group)
            group = []
        group.append(word)
        if word[3].endswith((".", "?", "!")):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    for group in groups:
        for index, word in enumerate(group):
            shown = [ass_escape(member[3]) for member in group]
            shown[index] = r"{\1c&H56FAF4&}" + shown[index] + r"{\1c&HFFFFFF&}"
            next_start = group[index+1][1] if index+1 < len(group) else word[2]+.08
            event("Words", word[1]-start, min(next_start, word[2]+.18, end)-start,
                  r"{\pos(508,1555)}" + " ".join(shown))
    path.write_text("\n".join(rows), encoding="utf-8-sig")


def even(value):
    return math.floor(value/2)*2


def export(root, asset, candidate, transcript, config, folder, *, portrait=True):
    from . import shorts_release

    folder.mkdir(parents=True, exist_ok=True)
    master = root / asset["path"]
    target = folder / ("portrait.mp4" if portrait else "source-context.mp4")
    edition = folder / "edition.json"
    if target.is_file() and (not portrait or edition.is_file()):
        return target
    start, end = candidate["start"], candidate["end"]
    if portrait:
        shorts_release.check_plan(end-start)
    base = [media.require("ffmpeg"), "-v", "error", "-nostdin", "-y"]
    if portrait:
        width,height,_ = inspect_media(master)
        from . import streamer_layout
        try:
            camera, layout = streamer_layout.evidence(master,{"start":start,"end":end},None)
        except streamer_layout.FramingRequired:
            camera, layout = facecam(master,start,end,None)
        game = "scale=1080:608:force_original_aspect_ratio=decrease,pad=1080:608:(ow-iw)/2:(oh-ih)/2"
        if camera:
            box = [even(v * dimension) for v, dimension in zip(camera, (width,height,width,height))]
            x, y, w, h = box
            mask = f"drawbox=x={x}:y={y}:w={w}:h={h}:color=0x121418:t=fill"
            filters = (f"[0:v]split[camera][game];[camera]crop={w}:{h}:{x}:{y},"
                       "scale=860:488:force_original_aspect_ratio=decrease,"
                       "pad=1080:544:(ow-iw)/2:oh-ih-18:color=0x121418[top];"
                       f"[game]{mask},{game}[play];[top][play]vstack,"
                       "pad=1080:1920:0:332:color=0x121418,setsar=1,fps=30")
        else:
            filters = (f"[0:v]{game},pad=1080:1920:0:586:color=0x121418,setsar=1,fps=30")
        make_captions(folder / "words.ass", transcript["words"], candidate, config["stream"]["credit"])
        filters += ",ass=words.ass[v]"
        flags = ["-filter_complex_threads", "2", "-filter_complex", filters, "-map", "[v]"]
    else:
        start, end = max(0,start-8), min(asset["duration"],end+8)
        layout, camera = "", None
        flags = ["-map", "0:v:0", "-vf", "scale='min(960,iw)':-2,fps=30"]
    pending = target.with_suffix(".partial.mp4")
    import subprocess
    try:
        result = subprocess.run([*base, "-ss", f"{start:.3f}", "-i", str(master),
                                 "-t", f"{end-start:.3f}", *flags, "-map", "0:a:0",
                                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                 "-threads", "4", "-pix_fmt", "yuv420p", "-c:a", "aac",
                                 "-b:a", "160k", "-movie_timescale", "48000", "-movflags", "+faststart", str(pending)],
                                cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=400)
    except subprocess.TimeoutExpired:
        raise media.MediaError("Portrait encoding timed out. Retry the job.") from None
    if result.returncode:
        raise media.MediaError("FFmpeg could not encode the stream cut. Check the video and subtitle setup.")
    media.probe(pending)
    if portrait:
        width, height, _ = inspect_media(pending)
        if (width,height) != (1080,1920):
            raise media.MediaError("Portrait output failed the required dimensions.")
        duration_check = shorts_release.require(pending)
        write(edition, {"layout": layout, "camera": camera, "candidate": candidate["id"], "release_length": duration_check,
                        "source_range": [start,end], "post": candidate["post"],
                        "credit": config["stream"]["credit"]})
    pending.replace(target)
    return target
