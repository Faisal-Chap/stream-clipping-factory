"""Per-source-hour accounting. Edition variants share a story identity."""

import math
from collections import defaultdict

from . import streamer_asr

HOUR = 3600


def payoff(candidate):
    if "primary_payoff" in candidate:
        return candidate["primary_payoff"]
    evidence = candidate.get("evidence")
    return evidence[-1]["time"][1] if evidence else max(candidate.get("start",0),candidate.get("end",0)-.001)


def coverage(intervals, left, right):
    covered, end = 0, left
    for start, stop in sorted(intervals):
        start, stop = max(left,start,end), min(right,stop)
        if stop>start:
            covered += stop-start
            end = stop
    return covered


def hour_index(at, duration):
    return int(max(0,min(at,max(0,duration-.001)))//HOUR)


def funnel(root, db, job, data, target=7):
    duration = data["asset"]["duration"]
    intervals = [(chapter["start"], chapter["end"]) for chapter in data.get("chapters",[])
                 if chapter.get("judgment")!="blocked" or chapter.get("followup") in ("compact","split-retry")]
    rows = []
    for index in range(math.ceil(duration/HOUR)):
        left,right = index*HOUR,min(duration,(index+1)*HOUR)
        rows.append({"start":left,"end":right,"covered":coverage(intervals,left,right),"complete":right-left==HOUR,
                     "target":target,
                     **{key:[] for key in ("discovered","merged","candidate","rejected","qualified","exported",
                                          "qa","independent","awaiting","delivered")}})
    folder = root/"streamer-runs"/str(job)
    editions = defaultdict(list)
    for row in db.execute("""SELECT candidate,qualification,render_state,qa,e.id,
                (SELECT verdict FROM streamer_edition_feedback WHERE edition=e.id ORDER BY id DESC LIMIT 1) AS feedback
                FROM streamer_editions e WHERE job=?""",(job,)):
        editions[row["candidate"]].append(row)
    reviews = {}
    for candidate in data["proposals"]:
        identity = candidate["id"]
        if candidate.get("merged_into"):
            continue
        review = folder/identity/"review.json"
        reviews[identity] = streamer_asr.read(review).get("decision") if review.is_file() else None
    for item in data["proposals"]:
        identity = item["id"]
        row = rows[hour_index(payoff(item),duration)]
        row["discovered"].append(identity)
        if item.get("merged_into"):
            row["merged"].append(identity)
            continue
        row["candidate"].append(identity)
        versions = editions[identity]
        accepted = reviews.get(identity)=="accepted"
        qualifies = accepted or any(x["qualification"]=="qualified" for x in versions)
        if qualifies:
            row["qualified"].append(identity)
        rework = any(x["feedback"] in ("rework","rejected") or x["qualification"]=="rejected"
                     or x["render_state"]=="failed" or (
                         x["qualification"]=="candidate" and (folder/identity/"editions"/x["id"]/"qc.json").is_file())
                     for x in versions)
        if rework or (not qualifies and (reviews.get(identity)=="rejected" or item.get("rejection"))):
            row["rejected"].append(identity)
        row["exported"].extend([f"{identity}/{x['id']}" for x in versions if x["render_state"]=="exported"])
        if (folder/identity/"portrait.mp4").is_file():
            row["exported"].append(identity)
        if accepted or any(x["feedback"]=="accepted" and x["render_state"]=="exported" for x in versions):
            row["independent"].append(identity)
        if (not reviews.get(identity) and any(x["render_state"]=="exported" for x in versions)
                and not any(x["feedback"] for x in versions)):
            row["awaiting"].append(identity)
        row["qa"].extend([f"{identity}/{x['id']}" for x in versions
                          if x["qa"] and x["qualification"]=="qualified" and x["render_state"]=="exported"
                          and x["feedback"] in (None,"accepted")])
    return rows
