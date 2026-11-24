"""Versioned source edits and independent editorial and export checkpoints."""

import hashlib
import json
import math
import re

from . import media, shorts_release, streamer_asr, streamer_layout
from .store import connect

FPS = 30
RECIPE = "source-edits-1"
REORDER_RECIPE = "source-edits-2"
LAYOUTS = ("wide", "focus", "reaction")
TIME_CUES = ("Preview", "Earlier", "Replay", "Later")
REVIEW_CHECKS = (
    ("distinct","Distinct premise and payoff are understandable"),
    ("truthful","The gameplay and dialogue support the hook"),
    ("legible","The action and text are legible at phone size"),
    ("sound","The audio and captions are complete and aligned"),
    ("watch","I would keep watching as a GTA-literate viewer"),
)


def source(root, db, job, candidate):
    row = db.execute("SELECT revision,asset,settings FROM jobs WHERE id=?", (job,)).fetchone()
    folder = root / "streamer-runs" / str(job)
    if not row or not (folder / "selection.json").is_file():
        raise ValueError("This run has no retained selection.")
    selection = streamer_asr.read(folder / "selection.json")
    original = next((item for item in selection["proposals"] if item["id"] == candidate), None)
    config = json.loads(row["settings"])
    if original and not original.get("evidence") and config.get("hybrid_version"):
        journal = folder/"hybrid-results.json"
        decision = streamer_asr.read(journal).get(candidate,{}).get("decision",{}) if journal.is_file() else {}
        if decision.get("decision")=="accept":
            a,b = decision["interval"]
            payoff = decision["payoff"]
            original = dict(original,start=a,end=b,shots=[[a,b]],evidence=decision.get("speech") or
                            [{"time":[a,a],"quote":""},{"time":[payoff,payoff],"quote":""}])
    if not original or not original.get("evidence") or row["asset"] != selection["asset"]["hash"]:
        raise ValueError("The source revision or candidate evidence is unavailable.")
    return original, selection, row["revision"], config["stream"]


def seconds(value):
    try:
        number = float(value)
        if math.isfinite(number) and 0 <= number <= 48 * 3600:
            return number
    except (ValueError, TypeError):
        pass
    raise ValueError("Enter source times as finite nonnegative seconds.")


def frame_time(value):
    return round(seconds(value) * FPS) / FPS


def normalize(fields, duration, *, bounds=shorts_release.RELEASE_BOUNDS):
    title = fields.get("title", "").strip()
    rationale = fields.get("rationale", "").strip()
    if not 1 <= len(title) <= 90 or not 1 <= len(rationale) <= 2000:
        raise ValueError("Enter an edition name and the observed evidence.")
    if fields.get("decision") == "reject":
        return {"title": title, "rationale": rationale, "decision": "rejected"}
    hook, post, context = (fields.get(name, "").strip() for name in ("hook", "post", "context"))
    if not 1 <= len(hook) <= 55 or not 1 <= len(context) <= 45 or not 1 <= len(post) <= 400:
        raise ValueError("Enter a hook (up to 55 characters), game context and post copy.")
    cuts = []
    cursor = 0
    for line in fields.get("cuts", "").splitlines():
        if not line.strip():
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) not in (4,5) or parts[2] not in LAYOUTS:
            raise ValueError("Use start | end | layout | crop keyframes | optional time cue for each shot.")
        start, end = map(frame_time, parts[:2])
        cue = parts[4] if len(parts)==5 else ""
        if cue and cue not in TIME_CUES:
            raise ValueError("Use Preview, Earlier, Replay or Later for a time cue.")
        if end > duration or end - start < .19:
            raise ValueError("Every shot needs a positive source interval within the archive.")
        if cuts and start<cuts[-1]["end"] and cue not in ("Earlier","Replay"):
            raise ValueError("Mark a return to earlier footage with Earlier or Replay.")
        repeats = [prior for prior in cuts if max(start,prior["start"])<min(end,prior["end"])-.001]
        if repeats and cue!="Replay" and not all(prior.get("time_cue")=="Preview" for prior in repeats):
            raise ValueError("Mark repeated source footage as Replay.")
        keys = []
        for key in filter(str.strip, parts[3].split(";")):
            values = key.split(",")
            if len(values) != 3:
                raise ValueError("Each crop keyframe needs source time, x and y.")
            at, x, y = map(seconds, values)
            if not start-.02 <= at <= end+.02 or not 0 <= x <= 1 or not 0 <= y <= 1:
                raise ValueError("Keep crop keyframes in their shot and x/y between 0 and 1.")
            if keys and at <= keys[-1][0]:
                raise ValueError("Enter crop keyframes in increasing time order.")
            keys.append([at, x, y])
        if len(keys) > 12 or (keys and parts[2] != "focus"):
            raise ValueError("Use up to twelve crop keyframes per focus shot.")
        cuts.append({"start": start, "end": end, "layout": parts[2],
                     "keys": keys, "edit_start": round(cursor, 6),
                     "edit_end": round(cursor + end - start, 6)})
        if cue:
            cuts[-1]["time_cue"] = cue
        cursor += end - start
    if not 1 <= len(cuts) <= 12:
        raise ValueError("Use 1–12 shots.")
    shorts_release.check_plan(round(cursor * FPS) / FPS,bounds=bounds)
    crossfade = seconds(fields.get("crossfade", "0.04"))
    if crossfade > .08:
        raise ValueError("Limit the original-audio crossfade to 80 ms.")
    for prior, after in zip(cuts, cuts[1:]):
        gap = after["start"] - prior["end"]
        before_limit = duration-prior["end"]
        after_limit = after["start"]
        after["audio_handle"] = (round(min(abs(gap), crossfade, before_limit*2, after_limit*2)/2, 6)
                                 if abs(gap) > .001 else 0)
    cues = []
    for line in fields.get("captions", "").splitlines():
        if not line.strip():
            continue
        parts = [piece.strip() for piece in line.split("|", 3)]
        if len(parts) != 4:
            raise ValueError("Use source start | source end | caption text | emphasis.")
        start, end = map(seconds, parts[:2])
        text, emphasis = parts[2:]
        if end <= start or len(text) > 68 or (emphasis and emphasis.casefold() not in text.casefold()):
            raise ValueError("Use a positive caption range and an emphasis term from the caption.")
        from .factory_metrics import coverage
        covered = coverage([(c["start"],c["end"]) for c in cuts],start,end)
        if end-start-covered > .003 or (cues and start < cues[-1]["end"]):
            raise ValueError("Captions must be ordered and wholly inside retained footage.")
        cues.append({"start": start, "end": end, "text": text, "emphasis": emphasis})
    if len(cues) > 180:
        raise ValueError("Use at most 180 caption corrections.")
    beats = {}
    if fields.get("beats", "").strip():
        times = fields["beats"].split(",")
        if len(times) != 3:
            raise ValueError("Mark three source times for premise, meaningful action and payoff.")
        for name, value in zip(("premise", "action", "payoff"), times):
            at = seconds(value)
            spot = next((c for c in cuts if c["start"] <= at < c["end"]), None)
            if not spot:
                raise ValueError("Every evidence marker must be in retained footage.")
            beats[name] = {"source": at, "edit": round(spot["edit_start"]+at-spot["start"], 3)}
        if [b["source"] for b in beats.values()] != sorted(b["source"] for b in beats.values()):
            raise ValueError("Mark premise, action and payoff in source order.")
    qualified = fields.get("decision") == "qualify"
    if qualified and not beats:
        raise ValueError("Qualification requires the three evidence markers.")
    return {"title": title, "hook": hook, "context": context, "post": post, "cuts": cuts,
            "captions": cues, "beats": beats, "crossfade": crossfade, "rationale": rationale,
            "duration": round(cursor, 6), "decision": "qualified" if qualified else "candidate"}


def enqueue(root, db, job, candidate, fields):
    _, selection, revision, setting = source(root, db, job, candidate)
    from . import portal_templates
    config = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?",(job,)).fetchone()[0])
    style = portal_templates.assign(db,job,candidate,config.get("template_plan"))
    parent = db.execute("""SELECT e.plan FROM done_matches m JOIN done_editions d ON d.story=m.story
                       JOIN streamer_editions e ON e.id=d.edition WHERE m.job=? AND m.candidate=?
                       AND e.job=? ORDER BY d.rowid DESC LIMIT 1""",(job,candidate,job)).fetchone()
    if not parent:
        parent = db.execute("""SELECT plan FROM streamer_editions WHERE job=? AND candidate=?
                    AND json_extract(plan,'$.origin') IN ('north-yankton-pilot','audio-first')
                    AND json_type(plan,'$.render_style')='object' ORDER BY rowid DESC LIMIT 1""",(job,candidate)).fetchone()
    original = json.loads(parent["plan"]) if parent else None
    locked = bool(original and config.get("hybrid_version") and original.get("source_windows"))
    if locked:
        selection = dict(selection,asset=dict(selection["asset"],windows=original["source_windows"]))
    plan = pin(root,job,candidate,fields,selection,revision,setting,planned_style=style,
               story_v4=bool(original and original.get("story_v4")))
    if locked and plan["decision"]!="rejected":
        old_bounds = [(cut["start"],cut["end"]) for cut in original["cuts"]]
        if [(cut["start"],cut["end"]) for cut in plan["cuts"]]!=old_bounds:
            raise ValueError("Keep the locked source interval when correcting this hybrid edition.")
        plan["hybrid_locked"] = True
    if parent and plan["decision"]!="rejected":
        proof = dict(original["proof"])
        cut = next((item for item in plan["cuts"] if item["start"]<=proof["source"]<item["end"]),None)
        if not cut:
            raise ValueError("Keep the verified source payoff in this story’s recut.")
        proof["edit"] = round(cut["edit_start"]+proof["source"]-cut["start"],6)
        if not plan["beats"]:
            plan["beats"] = {"premise":{"source":plan["cuts"][0]["start"],"edit":0},
                             "action":{"source":proof["source"],"edit":proof["edit"]},
                             "payoff":{"source":proof["source"],"edit":proof["edit"]}}
        from . import template_motion
        treatment = template_motion.pin(dict(original["render_style"],treatment=template_motion.DIALOGUE_VERSION))
        plan.update(render_style=treatment,proof=proof,hook_decision=original["hook_decision"],
                    origin="manual",prior_verified_hook=plan["hook"]==original["hook"])
        if plan.get("story_v4"):
            plan["owner_viewing"] = True
        if not plan["prior_verified_hook"]:
            plan["hook_decision"] = {"hook":plan["hook"],"reason":plan["rationale"],"provenance":"Operator-authored recut"}
    return register(db,plan)


def pin(root,job,candidate,fields,selection,revision,setting,*,planned_style=None,story_v4=False):
    asset,transcript = selection["asset"],selection["transcript"]
    normalized = normalize(fields,asset["duration"],
                           bounds=shorts_release.STORY_V4_BOUNDS if story_v4 else shorts_release.RELEASE_BOUNDS)
    if story_v4:
        normalized["story_v4"] = True
    camera = streamer_layout.rectangle(fields.get("camera_crop"))
    hud = streamer_layout.rectangle(fields.get("hud_crop"))
    windows = []
    if asset.get("kind")=="audio-first" and normalized["decision"]!="rejected":
        if normalized["crossfade"]:
            raise ValueError("Use a zero crossfade for selected platform ranges without extra source handles.")
        for shot in normalized["cuts"]:
            window = next((w for w in asset["windows"] if w["start"]-.0001<=shot["start"]
                           and shot["end"]<=w["end"]+.0001),None)
            if not window:
                raise ValueError("That shot needs verified footage. Keep edits within the acquired source intervals.")
            if window not in windows:
                windows.append(window)
    return dict(normalized, recipe=streamer_layout.RECIPE, job=job, candidate=candidate, asset=asset["hash"],
                archive=asset["path"], source_revision=revision, transcript=transcript,
                transcript_hash=media.checksum(root / transcript), fps=FPS,
                credit=setting["credit"], camera=camera, game=streamer_layout.rectangle(fields.get("game_crop")),
                **({"hud":hud} if hud else {}),**({"planned_style":planned_style} if planned_style else {}),
                **({"source_windows":windows} if windows else {}))


def source_input(root, plan, start, end):
    if not plan.get("source_windows"):
        return root/plan["archive"], start
    for window in plan["source_windows"]:
        if window["start"]-.0001 <= start and end <= window["end"]+.0001:
            return root/window["path"], max(0,start-window["start"])
    raise media.MediaError("The edit refers to a source interval that was not acquired.")


def encoded_plan(plan):
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), encoded


def register(db,plan,*, queued=True):
    if plan["decision"] != "rejected":
        shorts_release.check_plan(plan["duration"],bounds=shorts_release.bounds_for(plan))
    identity,encoded = encoded_plan(plan)
    state = "held" if plan["decision"] == "rejected" else "queued"
    if state=="queued" and not queued:
        state = "rendering"
    db.execute("""INSERT OR IGNORE INTO streamer_editions(id,job,candidate,plan,qualification,render_state)
                  VALUES(?,?,?,?,?,?)""", (identity, plan["job"], plan["candidate"], encoded, plan["decision"], state))
    return identity


def directory(root, job, candidate, identity):
    return root / "streamer-runs" / str(job) / candidate / "editions" / identity


def review(db, root, identity, reason):
    reason = reason.strip()
    if not 1 <= len(reason) <= 2000:
        raise ValueError("Enter picture, audio and caption observations for final QA.")
    feedback = db.execute("SELECT verdict FROM streamer_edition_feedback WHERE edition=? ORDER BY id DESC LIMIT 1",
                          (identity,)).fetchone()
    if feedback and feedback["verdict"]!="accepted":
        raise ValueError("Resolve the latest independent review before confirming final QA.")
    row = db.execute("SELECT * FROM streamer_editions WHERE id=?", (identity,)).fetchone()
    if not row or row["render_state"] != "exported" or row["qualification"] != "qualified":
        raise ValueError("Final QA requires a qualified export awaiting review.")
    if restriction := shorts_release.restriction(db, identity):
        raise ValueError(restriction)
    shorts_release.require(shorts_release.portrait_path(root, row),bounds=shorts_release.bounds_for(json.loads(row["plan"])))
    saved = db.execute("""UPDATE streamer_editions SET qa=? WHERE id=? AND qa IS NULL
                          AND render_state='exported' AND qualification='qualified'""", (reason, identity))
    if not saved.rowcount:
        raise ValueError("Final QA requires a qualified export awaiting review.")


def feedback(db, row, fields):
    reason = fields.get("reason","").strip()
    verdict = fields.get("verdict")
    if row["render_state"]!="exported" or verdict not in ("accepted","rework","rejected") or not 1<=len(reason)<=2000:
        raise ValueError("A playable edition, verdict and observed evidence are required.")
    if verdict=="accepted" and any(fields.get(key)!="on" for key,_ in REVIEW_CHECKS):
        raise ValueError("Confirm every independent viewing requirement before accepting.")
    db.execute("INSERT INTO streamer_edition_feedback(edition,verdict,reason) VALUES(?,?,?)",
               (row["id"],verdict,reason))


def comparison(root, db, job, candidate, identity):
    row = db.execute("""SELECT * FROM streamer_editions WHERE job=? AND candidate=? AND id=?
                        AND render_state='exported'""", (job, candidate, identity)).fetchone()
    original = root / "streamer-runs" / str(job) / candidate / "portrait.mp4"
    variant = directory(root, job, candidate, identity) / "portrait.mp4"
    if not row or not original.is_file() or not variant.is_file():
        raise ValueError("Two playable portraits are required for a blind comparison.")
    return original, variant


def version_slot(ballot):
    if not re.fullmatch(r"[a-f0-9]{32}", ballot or ""):
        raise ValueError("Reload the blind pair to receive a ballot.")
    return "a" if int(ballot[0], 16) % 2 == 0 else "b"


def save_ballot(db, identity, ballot, fields):
    slot = version_slot(ballot)
    verdict = fields.get("verdict")
    observations = {key: fields.get(key, "").strip() for key in ("mechanic", "picture", "reason")}
    if verdict not in ("a", "b", "tie", "both_weak") or any(
            not 1 <= len(message) <= 1200 for message in observations.values()):
        raise ValueError("Answer each viewing question and choose a verdict.")
    db.execute("""INSERT OR IGNORE INTO streamer_edition_ballots(edition,ballot,version_slot,verdict,observations)
                  VALUES(?,?,?,?,?)""", (identity, ballot, slot, verdict, json.dumps(observations, ensure_ascii=False)))


def next_edit(db):
    row = db.execute("""SELECT e.* FROM streamer_editions e JOIN jobs j ON j.id=e.job WHERE e.render_state='queued'
                     AND (COALESCE(json_extract(e.plan,'$.origin'),'manual')='manual' OR
                     (json_extract(e.plan,'$.origin') IN ('north-yankton-pilot','audio-first') AND j.state='complete'))
                     ORDER BY e.rowid LIMIT 1""").fetchone()
    if row:
        db.execute("UPDATE streamer_editions SET render_state='rendering',error='' WHERE id=?", (row["id"],))
    return row


def process(root, row):
    from .streamer_edit_render import render
    plan = json.loads(row["plan"])
    target = directory(root, row["job"], row["candidate"], row["id"])
    try:
        render(root, target, plan)
        if plan.get("render_style"):
            from . import factory_done, streamer_auto
            check = streamer_auto.qc(root,target,plan,streamer_asr.read(root/plan["transcript"]),audition=False)
            if not check["passed"]:
                raise media.MediaError("; ".join(check["issues"]))
            if plan.get("origin")=="north-yankton-pilot" or plan.get("prior_verified_hook") or plan["decision"]=="qualified":
                with connect(root,True) as db:
                    factory_done.finish(root,db,row["job"],row["candidate"],row["id"],plan,target)
                    db.execute("UPDATE streamer_editions SET render_state='exported',qualification='qualified',error='' WHERE id=?",
                               (row["id"],))
                    count = factory_done.current_count(db,root,row["job"])
                    db.execute("""UPDATE jobs SET progress=? WHERE id=? AND state='complete'
                                  AND json_extract(settings,'$.north_yankton')=1""",
                               (f"{count} distinct edited reels passed. Target 7. Open Done for the masters.",row["job"]))
                    coverage = target.parent.parent.parent/"audio-first.json"
                    if plan.get("source_windows") and coverage.is_file():
                        state = streamer_asr.read(coverage)
                        message = (f"{count} qualified Shorts. {state['analyzed']/60:.1f} of "
                                   f"{state['duration']/60:.1f} source minutes judged."
                                   +(" Partial coverage." if not state.get("complete") else ""))
                        db.execute("UPDATE jobs SET progress=? WHERE id=? AND state='complete'",(message,row["job"]))
                return
        state, error = "exported", ""
    except (OSError, ValueError, media.MediaError) as failure:
        state, error = "failed", str(failure) if isinstance(failure, (ValueError, media.MediaError)) else "Could not read or write the edit artifacts."
    with connect(root, True) as db:
        db.execute("UPDATE streamer_editions SET render_state=?,error=? WHERE id=?", (state, error, row["id"]))
