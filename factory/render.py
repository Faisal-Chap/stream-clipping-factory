"""Local preview encoding and dense frame evidence."""

from .analysis import ffmpeg
from .media import MediaError, probe

INSET = ("scale=540:960:force_original_aspect_ratio=decrease:force_divisible_by=2,"
         "pad=540:960:(ow-iw)/2:(oh-ih)/2:color=0x101217,setsar=1,fps=30")


def render_preview(root, asset, story, plan):
    folder = root / "previews" / str(story)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "preview.mp4"
    if target.is_file():
        try:
            probe(target)
        except MediaError:
            pass  # Rebuild to a partial file before replacing the damaged preview.
        else:
            return target.relative_to(root).as_posix()
    master = root / asset["path"]
    for index, timestamp in enumerate(plan["evidence"]):
        thumbnail = folder / f"evidence-{index}.jpg"
        if not thumbnail.is_file():
            ffmpeg(master, ["-ss", str(timestamp), "-i", "-map", "0:V:0", "-frames:v", "1",
                            "-vf", "scale=480:-2", "-update", "1", str(thumbnail)], 180)
    encode(master, plan["start"], plan["end"], target, INSET)
    return target.relative_to(root).as_posix()


def encode(master, start, end, target, filters):
    partial = target.with_suffix(".partial")
    ffmpeg(master, ["-ss", str(start), "-i", "-t", str(end-start),
                    "-map", "0:V:0", "-map", "0:a:0?", "-filter_threads", "2",
                    "-vf", filters,
                    "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    "-f", "mp4", str(partial)], 600)
    probe(partial)
    partial.replace(target)


def render_context(root, asset, story, plan):
    folder = root / "previews" / str(story)
    master = root / asset["path"]
    context = folder / "source-context.mp4"
    alternate = folder / "alternate-framing.mp4"
    if not context.is_file():
        encode(master, max(0, plan["start"]-6), min(asset["duration"], plan["end"]+8),
               context, "scale=540:-2,setsar=1,fps=24")
    if not alternate.is_file():
        encode(master, plan["start"], plan["end"], alternate,
               "crop=w='min(iw,2*trunc(ih*2/3))':h=ih," + INSET)
