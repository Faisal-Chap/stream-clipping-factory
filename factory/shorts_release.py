"""Duration and current-edition rules for released Shorts."""

import json
from fractions import Fraction
from functools import lru_cache

from . import media

MINIMUM = 17
MAXIMUM = 45
RULE = "17.000–45.000 seconds inclusive"
STORY_V4_BOUNDS = (25,150)
RELEASE_BOUNDS = (MINIMUM,MAXIMUM)
CURRENT_DONE = """SELECT d.edition FROM done_editions d
                  WHERE d.story=? ORDER BY d.rowid DESC LIMIT 1"""


def bounds_for(plan):
    return STORY_V4_BOUNDS if plan.get("story_v4") else RELEASE_BOUNDS


def rule(bounds):
    return RULE if bounds==RELEASE_BOUNDS else f"{bounds[0]:.3f}–{bounds[1]:.3f} seconds inclusive"


def check_plan(duration, *, bounds=RELEASE_BOUNDS):
    if not bounds[0] <= duration <= bounds[1]:
        raise ValueError(f"A Short must run {rule(bounds)}. Choose a complete source story within that range.")


def portrait_path(root, row, identity=None):
    edition = identity or row["id"]
    return root/"streamer-runs"/str(row["job"])/row["candidate"]/"editions"/edition/"portrait.mp4"


def timestamp(seconds):
    whole, _, decimals = f"{float(seconds):.6f}".partition(".")
    return f"{whole}.{decimals.rstrip('0').ljust(3, '0')}"


def inspect(path, *, bounds=RELEASE_BOUNDS):
    try:
        stat = path.stat()
        return _decode(path.resolve(), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,bounds)
    except (OSError, ValueError, KeyError, StopIteration, ZeroDivisionError, media.MediaError):
        return {"passed": False, "issue": "Could not verify decoded picture, audio and MP4 duration."}


def matches_master(path, digest):
    try:
        info = path.stat()
        return _master_checksum(path.resolve(),info.st_size,info.st_mtime_ns,info.st_ctime_ns)==digest
    except OSError:
        return False


@lru_cache(maxsize=256)
def _master_checksum(path, _size, _modified, _created):
    return media.checksum(path)


@lru_cache(maxsize=256)
def _decode(path, size, modified, created, bounds):
    probe = [media.require("ffprobe"), "-v", "error", "-protocol_whitelist", "file",
             "-threads", "4", "-of", "json"]
    streams = "stream=index,codec_type,codec_name,time_base,start_pts,duration_ts,sample_rate"
    layout = media.run([*probe, "-show_entries", streams+":format=duration", str(path)], 30)
    if layout.returncode or layout.stderr:
        raise media.MediaError("Could not read the MP4 timeline.")
    payload = json.loads(layout.stdout)
    container = Fraction(payload["format"]["duration"])
    by_type = {}
    for stream in payload["streams"]:
        kind = stream["codec_type"]
        if kind in ("video", "audio"):
            if kind in by_type:
                raise media.MediaError("Expected one picture track and one original-audio track.")
            by_type[kind] = stream
    if len(by_type) != 2:
        raise media.MediaError("Expected one picture track and one original-audio track.")
    declared = {k: int(s["duration_ts"])*Fraction(s["time_base"]) for k,s in by_type.items()}
    maximum = bounds[1]
    if container > maximum+1 or any(span > maximum+1 for span in declared.values()):
        return {"passed": False, "container": timestamp(container),
                "issue": f"The MP4 or a media track exceeds the {maximum:.3f}-second maximum."}
    result = media.run([*probe, "-show_frames", "-show_entries",
                       "frame=media_type,stream_index,best_effort_timestamp,duration,pkt_duration,nb_samples",
                       str(path)], max(100, 2*maximum))
    if result.returncode or result.stderr:
        raise media.MediaError("The MP4 did not decode cleanly.")
    frames = json.loads(result.stdout)["frames"]
    measured = {}
    for kind in ("video", "audio"):
        stream = by_type[kind]
        unit = Fraction(stream["time_base"])
        points = []
        for frame in frames:
            if frame["stream_index"] != stream["index"]:
                continue
            start = int(frame["best_effort_timestamp"])*unit
            duration = int(frame.get("duration", frame.get("pkt_duration", 0)))*unit
            if kind == "audio":
                samples = int(frame["nb_samples"])
                decoded = Fraction(samples, int(stream["sample_rate"]))
                track_end = int(stream["start_pts"])*unit + declared[kind]
                # Older ffprobe builds may decode the entire last AAC block.
                # The final packet's declared duration trims encoder padding.
                if stream["codec_name"] == "aac" and 0 < duration < decoded and start+duration == track_end:
                    decoded = duration
                duration = decoded
            if duration <= 0:
                raise media.MediaError("Decoded frames need positive durations.")
            points.append((start, start+duration))
        points.sort()
        first, end = next(iter(points))
        coverage = end-first
        for start, stop in points[1:]:
            coverage += max(0, stop-max(start,end))
            end = max(stop,end)
        # A leading gap cannot contribute to the minimum, and a late tail
        # still belongs to the presentation even when there is a gap.
        measured[kind] = coverage, max(end, end-first, declared[kind])
    totals = {"container": container, **{k: interval[1] for k,interval in measured.items()}}
    minimum,maximum = bounds
    passed = (all(minimum <= span <= maximum for span in totals.values())
              and all(coverage >= minimum for coverage,_ in measured.values()))
    label = "Release" if bounds==RELEASE_BOUNDS else "Export"
    issue = "" if passed else f"{label} length requires {rule(bounds)} of playable picture and audio within the MP4."
    return {"passed": passed, "issue": issue, **{k: timestamp(v) for k,v in totals.items()},
            **{k+"_playable": timestamp(v[0]) for k,v in measured.items()}}


def require(path, *, bounds=RELEASE_BOUNDS):
    result = inspect(path,bounds=bounds)
    if not result["passed"]:
        raise media.MediaError(describe(result))
    return result


def describe(result):
    lengths = " · ".join(f"{label} {result[key]} s" for key,label in (
        ("video", "Picture"), ("audio", "Audio"), ("container", "MP4")) if key in result)
    return " ".join(value for value in (result["issue"], lengths) if value)


def hold(db, story, edition):
    row = db.execute("SELECT reason FROM done_release_holds WHERE story=? AND edition=?",
                     (story,edition)).fetchone()
    return row["reason"] if row else ""


def restriction(db, identity):
    entry = db.execute("""SELECT m.story FROM streamer_editions e LEFT JOIN done_matches m
                          ON m.job=e.job AND m.candidate=e.candidate WHERE e.id=?""", (identity,)).fetchone()
    if entry and entry["story"] is not None:
        story = entry["story"]
        current = db.execute(CURRENT_DONE, (story,)).fetchone()
        if current:
            if current["edition"] != identity:
                return "Another edition is current for this Done story."
            return hold(db, story, identity)
    newer = db.execute("""SELECT 1 FROM streamer_editions e JOIN streamer_editions n
                          ON n.job=e.job AND n.candidate=e.candidate AND n.rowid>e.rowid
                          WHERE e.id=? AND n.render_state='exported' LIMIT 1""", (identity,)).fetchone()
    return "A newer export is current for this moment." if newer else ""


def available(db, row):
    removing = db.execute("""SELECT 1 FROM factory_release_artifacts WHERE edition=? AND state IN ('deleting','removed')
                              AND path LIKE '%/portrait.mp4'""",(row["id"],)).fetchone()
    return not removing and not restriction(db,row["id"])


def eligible(db, root, row, *, bounds=RELEASE_BOUNDS):
    return available(db,row) and inspect(portrait_path(root,row),bounds=bounds)["passed"]
