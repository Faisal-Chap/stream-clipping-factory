"""Offline temporal proposal experiment and equal-budget hosted shortlists."""

import statistics

from .analysis import rank, select

VERSION = "context-rank-v1"
JUDGE_WINDOWS = 8


def local_rank(samples, duration, blackouts, cues):
    base = rank(samples, duration, "baseline", blackouts)
    by_time = {sample["t"]: sample for sample in samples}
    result = []
    for item in base:
        t = item["time"]
        pre = [by_time[u]["change"] for u in range(t-12, t-2, 3) if u in by_time]
        post = [by_time[u]["change"] for u in range(t+3, t+13, 3) if u in by_time]
        recent = [by_time[u]["change"] for u in range(t-3, t+7, 3) if u in by_time]
        if not pre or not post or not recent:
            continue
        before, after = statistics.median(pre), statistics.median(post)
        persistent = statistics.median(recent)
        audio = item["audio"]
        start, end = max(0, t-15), min(duration, t+12)
        near = [b for b in blackouts if start <= b <= end]
        past = max((b for b in near if b < t), default=None)
        later = min((b for b in near if b > t), default=None)
        if past is not None:
            start = past + 3
        if later is not None:
            end = later
        speech = [cue for cue in cues if cue["end"] > start and cue["start"] < end]
        if speech and end - .85 <= speech[-1]["end"] <= min(end+2.5, duration) and later is None:
            end = min(duration, speech[-1]["end"] + .25)
        # Neighbouring activity plus a before/after change beats an isolated spike.
        score = 3.4*persistent + 1.7*abs(before-after) + .9*min(audio, .18)
        if persistent > .027 and speech:
            score += .06
        rejection = ""
        if t in near or t - start < 8 or end - t < 6:
            rejection = "Interrupted setup or outcome near a blackout or source boundary."
        elif persistent < .018 or (persistent < .045 and abs(before-after) < .025) or score < .16:
            rejection = "No sustained context change. Keep this as a retrieval option only."
        result.append(dict(item, start=round(start, 3), end=round(end, 3), score=round(score, 3),
                           rejection=rejection, context=True, speech=speech,
                           signal=dict(before=round(before, 3), after=round(after, 3),
                                       sustained=round(persistent, 3))))
    return sorted(result, key=lambda item: (-item["score"], item["time"]))


def shortlist(ranked, baseline, windows, cues, duration, semantic=()):
    # Keep quieter and uniformly spread options in both hosted arms.
    local, _ = select(ranked, 5)
    popular, _ = select(baseline, 5)
    speech = [(max(0, c["start"]-9), min(duration, c["end"]+12)) for c in cues
              if "!" in c["text"] or "?" in c["text"]]
    spread = list(windows[1::max(1, len(windows)//4)])
    groups = [(semantic[:4], "embedding"), (spread[:2], "coverage"), (speech[:2], "transcript"),
              ([(r["start"], r["end"]) for r in local], "context"),
              ([(r["start"], r["end"]) for r in popular], "baseline"), (windows, "coverage")]
    picks = []
    for options, route in groups:
        for start, end in options:
            if len(picks) >= JUDGE_WINDOWS:
                return picks
            if end-start < 8 or any(min(end, p["end"])-max(start, p["start"]) >= 10 for p in picks):
                continue
            picks.append({"start": start, "end": end, "route": route})
    return picks
