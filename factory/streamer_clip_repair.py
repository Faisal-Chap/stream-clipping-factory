"""Bounded cut repair and safe reuse of already verified picture ranges."""

import json
import math
from array import array

from . import media, shorts_release, streamer_event_review

MAX_EXTENSION = 14
PROBE_MARGIN = 3
RESERVES_PER_HOUR = 3


def corridor(shots, low, high, extra):
    return max(low,shots[0][0]-extra),min(high,shots[-1][1]+extra)


def probe_ranges(shots, low, high):
    ranges = [list(shot) for shot in shots]
    ranges[0][0] = max(low,math.floor((ranges[0][0]-PROBE_MARGIN)*30)/30)
    ranges[-1][1] = min(high,math.ceil((ranges[-1][1]+PROBE_MARGIN)*30)/30)
    return ranges


def safe_edge(words, position, lower, upper, *, end):
    value = position
    for word in reversed(words) if not end else words:
        if word[4]>=.7 and word[1]+.07<value<word[2]-.07:
            value = word[2]+.06 if end else word[1]-.06
    frame = math.ceil(value*30) if end else math.floor(value*30)
    return min(math.floor(upper*30)/30,max(math.ceil(lower*30)/30,frame/30))


def revised_cut(candidate, words, decision, low, high):
    """Move an edge once, only toward verified nearby word IDs, or ask for denser frames."""
    repair = decision.get("repair",{}) if decision.get("verdict")=="hold" else {}
    direction = repair.get("direction")
    shots = [list(shot) for shot in candidate["shots"]]
    if direction=="dense":
        return shots,None
    if direction not in ("earlier","later"):
        return None
    left,right = corridor(shots,low,high,MAX_EXTENSION)
    allowed = [[left,shots[0][0]]] if direction=="earlier" else [[shots[-1][1],right]]
    try:
        cue = streamer_event_review.read_span(words,repair.get("words"),allowed)
    except ValueError:
        return None
    if direction=="earlier":
        shots[0][0] = safe_edge(words,cue["time"][0]-.6,left,shots[0][0],end=False)
    else:
        shots[-1][1] = safe_edge(words,cue["time"][1]+.75,shots[-1][1],right,end=True)
    excess = sum(b-a for a,b in shots)-shorts_release.MAXIMUM
    if excess>.001:
        if direction=="later":
            limit = min(e["time"][0] for e in candidate["evidence"])-.3
            start = shots[0][0]
            if not start+excess<=min(limit,shots[0][1]-1):
                return None
            shots[0][0] = safe_edge(words,start+excess+.07,start,min(limit,shots[0][1]-1),end=False)
        else:
            limit = max(e["time"][1] for e in candidate["evidence"])+.3
            end = shots[-1][1]
            if not max(limit,shots[-1][0]+1)<=end-excess:
                return None
            shots[-1][1] = safe_edge(words,end-excess-.07,max(limit,shots[-1][0]+1),end,end=True)
    try:
        shorts_release.check_plan(round(sum(b-a for a,b in shots)*30)/30)
    except ValueError:
        return None
    if any(w[4]>=.7 and w[1]+.085<edge<w[2]-.085 for shot in shots for edge in shot for w in words):
        return None
    if not all(any(a<=e["time"][0] and e["time"][1]<=b for a,b in shots) for e in [*candidate["evidence"],cue]):
        return None
    return shots,cue


def next_piece(windows, at, end):
    candidates = [w for w in windows if w["start"]<=at+.0001 and w["end"]>at+.0001]
    if candidates:
        window = max(candidates,key=lambda w:w["end"])
        return min(end,window["end"]),window
    return min([end]+[w["start"] for w in windows if at+.0001<w["start"]<end]),None


def cover(windows, left, right, fetch):
    """Return consecutive verified pieces and fetch only uncovered source time."""
    cursor,pieces = left,[]
    while cursor<right-.0001:
        end,window = next_piece(windows,cursor,right)
        if window is None:
            window = fetch(cursor,end)
            if not window["start"]<=cursor+.0001 or window["end"]<end-.0001:
                raise media.MediaError("The fetched picture did not cover the planned source clock.")
        pieces.append((cursor,end,window))
        cursor = end
    return pieces


def dimensions(path):
    probe = media.run([media.require("ffprobe"),"-v","error","-select_streams","v:0",
                       "-show_entries","stream=width,height","-of","json",str(path)],20)
    try:
        video = json.loads(probe.stdout)["streams"][0]
        return video["width"],video["height"]
    except (ValueError,KeyError,IndexError):
        raise media.MediaError("A reused picture range has no verified dimensions.") from None


def pcm(path, start, duration):
    decode_from = max(0,start-2)
    first = round((start-decode_from)*8000)
    last = first+round(duration*8000)
    result = media.run([media.require("ffmpeg"),"-v","error","-ss",f"{decode_from:.6f}","-i",str(path),
                        "-t",f"{duration:.6f}","-vn","-ac","1",
                        "-af",f"aresample=8000:first_pts=0,atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS",
                        "-f","s16le","-"],25)
    samples = array("h")
    if not result.returncode:
        samples.frombytes(result.stdout)
    return samples


def cue_alignment(source, source_at, portrait, portrait_at, duration):
    """Measure the original-audio cue on the two edit clocks, with no audio upload."""
    original = pcm(source,source_at,duration)
    lead = min(.06,portrait_at)
    rendered = pcm(portrait,portrait_at-lead,duration+.12)
    if len(original)<2400 or len(rendered)<len(original):
        raise media.MediaError("The final portrait is missing a decoded original-audio cue.")
    energy = sum(value*value for value in original)
    if energy < 400*len(original):
        return None

    def correlation(at):
        part = rendered[at:at+len(original)]
        power = sum(value*value for value in part)
        return sum(a*b for a,b in zip(part,original))/math.sqrt(power*energy) if power else 0

    last = len(rendered)-len(original)
    expected = round(lead*8000)
    def confidence(at):
        return correlation(at)-abs(at-expected)*.00004
    best = max(range(0,last+1,8),key=confidence)
    nearest = max(range(max(0,best-7),min(last,best+7)+1),key=confidence)
    score = correlation(nearest)
    return {"offset_ms":round((nearest-expected)/8,3),"correlation":round(score,4),
            "source_local_seconds":round(source_at,6),"portrait_seconds":round(portrait_at,6)}
