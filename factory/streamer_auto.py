"""Source-referenced structure selection and production QC for the normal worker."""

import json
import math
import re
from array import array
from collections import Counter
from statistics import fmean, pstdev

from . import media, shorts_release, streamer_asr, streamer_edit_render, streamer_edits
from .factory_metrics import HOUR, hour_index, payoff
from .store import connect

OPENING_LIMIT = 12
CALLBACK = re.compile(r"\b(again|remember|earlier|last time|same thing)\b",re.I)
GENERAL_TERMS = {"gta","chaos","game","vehicle","driving","player","streamer","because","there","about","right","going"}


def close_boundary(words, time, left, right, *, opening):
    time = max(left,min(right,time))
    for word in words:
        if word[1] < time < word[2]:
            time = max(left, word[1]-.055) if opening else min(right,word[2]+.055)
    return streamer_edits.frame_time(time)


def speech_gap(words, left, right):
    relevant = [w for w in words if w[2]>left and w[1]<right]
    a = left
    for w in relevant:
        if w[1]-a >= 2.2:
            return a+.24,w[1]-.24
        a = max(a,w[2])
    return None


def keyed(cut,tracking=()):
    start,end,layout,cue = cut
    keyframes = ""
    if layout=="focus":
        points = {stamp:(x,y) for stamp,x,y in tracking if start<=stamp<=end}
        keyframes = "; ".join(f"{stamp},{x},{y}" for stamp,(x,y) in sorted(points.items()))
    return f"{start:.3f} | {end:.3f} | {layout} | {keyframes}" + (f" | {cue}" if cue else "")


def complete_hook(candidate,setup):
    draft = candidate.get("hook","").strip()
    if (4<=len(draft)<=55 and draft.casefold() not in ("what happens next?","watch what happens")
            and not re.search(r"(?i)\b(?:and|then|but|because|so|takes|the|a|to)[\W]*$",draft)):
        return draft
    intention = setup["quote"].strip()
    if 4<=len(intention)<=50 and intention.endswith(("?","!")) and not re.search(r"(?i)\b(?:this|that)\W*$",intention):
        return intention
    return ""


def callback_proof(candidate, all_candidates):
    """Find a previous, separately verified example named by the current spoken setup."""
    if candidate.get("kind")!="callback" or candidate.get("visual_check",{}).get("verdict")!="accept":
        return None
    spoken = candidate["evidence"][0]["quote"].casefold()
    if not CALLBACK.search(spoken):
        return None
    def terms(text):
        return set(re.findall(r"[a-z0-9]{4,}",text.casefold()))-GENERAL_TERMS
    anchor = terms(spoken)
    hits = []
    for earlier in all_candidates:
        proof = earlier.get("primary_payoff")
        if (proof is None or proof >= candidate["start"]-12 or earlier.get("merged_into")
                or earlier.get("visual_check",{}).get("verdict")!="accept"):
            continue
        related = anchor & terms(" ".join([earlier.get("event",""), *(e["quote"] for e in earlier.get("evidence",[]))]))
        if len(related)>=2:
            hits.append((len(related),proof,earlier["id"],earlier))
    return max(hits,key=lambda x:x[:3])[-1] if hits else None


def structures(candidate, transcript, config, duration, *, related=None):
    words = transcript["words"]
    setup,closing = candidate["evidence"]
    frames = candidate.get("visual_check",{})
    picture = frames.get("sample_seconds",[])
    indexes = frames.get("frames",[])
    valid = frames.get("verdict")=="accept" and len(indexes)==2 and all(type(i) is int and 1<=i<=len(picture) for i in indexes)
    resolution = closing["time"][1]
    proof = picture[indexes[1]-1] if valid else min(resolution-.13,closing["time"][0]+.2)
    low,high = max(0,candidate["start"]-3),min(duration,candidate["end"]+3)
    start = close_boundary(words,min(setup["time"][0],picture[indexes[0]-1] if valid else setup["time"][0])-.42,
                           low,high,opening=True)
    end = close_boundary(words,max(resolution,proof+.38)+.28,low,high,opening=False)
    if end-start<3:
        return []
    premise = max(start,setup["time"][0])
    action = max(premise,min(proof,end-.08))
    resolved = min(end-.025,max(action,resolution))
    context = config.get("game") or "GTA V"
    # Preserve the original quoted wording or use a short accountable factual hook.
    hook = complete_hook(candidate,setup)
    if not hook:
        return []
    credit = config["stream"]["credit"]
    post = candidate.get("post") or f"From {credit}'s {context} stream."
    common = dict(hook=hook,context=context[:45],post=post[:400],crossfade=".040",decision="candidate",
                  beats=f"{premise:.3f},{action:.3f},{resolved:.3f}",captions="")
    quote = closing["quote"].strip()
    if 1<=len(quote)<=52 and "|" not in quote and closing["time"][1]<=end:
        hook_terms = {term.casefold() for term in re.findall(r"[\w'-]{4,}",hook)}-GENERAL_TERMS
        term = next((word for word in re.findall(r"[\w'-]{4,}",quote) if word.casefold() in hook_terms),"")
        if term:
            common["captions"] = f"{closing['time'][0]} | {closing['time'][1]} | {quote} | {term}"
    layout = "wide"
    primary = [(start,end,layout,"")]
    if start+1.8 < action < end-1.8 and frames.get("verdict")=="accept":
        pivot = streamer_edits.frame_time(max(start+1.6,action-1.05))
        primary = [(start,pivot,"wide",""),(pivot,end,"focus","")]
    if valid and isinstance(frames.get("reaction"),(int,float)):
        pivot = streamer_edits.frame_time(max(action+.55,frames["reaction"]-.1))
        a,b,view,cue = primary[-1]
        if a+.7<pivot<b-.85:
            primary[-1:] = [(a,pivot,view,cue),(pivot,b,"reaction","")]
    rationale = f"{candidate.get('setup') or setup['quote']} {candidate.get('payoff') or closing['quote']}"
    def encode(edits):
        return "\n".join(keyed(edit,frames.get("focus",[])) for edit in edits)

    options = [dict(common,title="Chronological proof",rationale=rationale,cuts=encode(primary))]
    # Rejoin the same verified event after a pause while retaining the evidence at both ends.
    gap = speech_gap(words,setup["time"][1]+.28,min(action-.9,closing["time"][0]-.28))
    if valid and not gap and action-start>OPENING_LIMIT:
        out = close_boundary(words,setup["time"][1]+.22,start,action-.9,opening=False)
        back = close_boundary(words,min(action-3,closing["time"][0]-.35),out,end,opening=True)
        if back-out>=4:
            gap = out,back
    if gap and valid:
        out,back = map(streamer_edits.frame_time,gap)
        if out-start>=2 and end-back>=2:
            options.append(dict(common,title="Setup and payoff",rationale=rationale,
                                cuts=encode([(start,out,"wide",""),(back,end,"focus","Later")])))
    if valid and proof-start>4:
        # The teaser is the proven payoff from the same event, never a reaction from a different candidate.
        preview_start = close_boundary(words,proof-.55,max(start,proof-1.3),end,opening=True)
        preview_end = close_boundary(words,proof+.55,preview_start,end,opening=False)
        if .6 <= preview_end-preview_start <= 2.9:
            rewound = [(preview_start,preview_end,"focus","Preview")]
            rewound.extend((a,b,layout,"Earlier" if index==0 else "")
                          for index,(a,b,layout,_) in enumerate(primary))
            options.append(dict(common,title="Payoff preview and rewind",rationale=rationale,
                                cuts=encode(rewound)))
    if related and valid:
        prior = related["primary_payoff"]
        left = close_boundary(words,prior-.8,related["start"],prior,opening=True)
        right = close_boundary(words,prior+1.25,prior,related["end"],opening=False)
        if .8<=right-left<=3.8:
            # Show a separately labeled earlier example, followed by the actual current payoff.
            linked = [(left,right,"wide","Earlier"),(start,end,"wide","Later")]
            rationale = f"Earlier example {related['id']} matches the spoken callback. {rationale}"
            options.append(dict(common,title="Earlier example and current payoff",rationale=rationale,
                                cuts=encode(linked)))
    attempts = frames.get("attempts",[])
    if valid and attempts and setup["time"][0]<=attempts[0]<attempts[-1]<proof:
        intervals = [(
            close_boundary(words,stamp-1.1,start,end,opening=True),
            close_boundary(words,stamp+1.2,start,end,opening=False),
            "focus","Later") for stamp in attempts]
        objective_end = close_boundary(words,setup["time"][1]+.12,start,end,opening=False)
        resolution_start = close_boundary(words,min(proof-.9,closing["time"][0]-.35),start,end,opening=True)
        escalating = [(start,objective_end,"wide",""),*intervals,(resolution_start,end,"focus","Later")]
        if all(b[0]>a[1]+.5 for a,b in zip(escalating,escalating[1:])):
            options.append(dict(common,title="Escalating attempts",rationale=rationale,cuts=encode(escalating)))
    return options


def frames_at(root,plan,stamp):
    path, at = streamer_edits.source_input(root,plan,stamp,stamp)
    result = media.run([media.require("ffmpeg"),"-v","error","-ss",f"{max(0,at-.3):.3f}","-i",str(path),
                       "-t","1.1","-vf","crop=iw*0.66:ih*0.66,scale=48:48,format=gray,fps=4",
                       "-an","-threads","1","-f","rawvideo","-"],20)
    frame_size = 48*48
    return [result.stdout[i:i+frame_size] for i in range(0,len(result.stdout)-frame_size+1,frame_size)]


def rms(samples):
    return math.sqrt(fmean(v*v for v in samples)) if samples else 0


def qc(root,target,plan,transcript,*,audition):
    report = streamer_asr.read(target/"manifest.json")
    visual = {}
    hard,warnings = [],[]
    path = target/"portrait.mp4"
    length = shorts_release.inspect(path,bounds=shorts_release.bounds_for(plan))
    if not length["passed"]:
        hard.append(shorts_release.describe(length))
    words = transcript["words"]
    for key,stamp in (("opening",plan["cuts"][0]["start"]+.2),("payoff",plan["beats"]["payoff"]["source"])):
        images = frames_at(root,plan,stamp)
        black = not images or all(fmean(img)<25 or pstdev(img)<3 for img in images)
        motion = [fmean(abs(a-b) for a,b in zip(first,last)) for first,last in zip(images,images[1:])]
        frozen = bool(motion) and max(motion)<.65
        visual[key] = {"stamp":stamp,"black":black,"frozen":frozen}
        if black or frozen:
            hard.append(f"{key.title()} picture is {'black or blank' if black else 'frozen'}.")
    for i,cut in enumerate(plan["cuts"]):
        if cut["layout"]=="reaction" and report["shots"][i]["layout"]!="reaction":
            warnings.append(f"Shot {i+1} used gameplay because a face crop could not be verified.")
        next_start = plan["cuts"][i+1]["start"] if i+1<len(plan["cuts"]) else None
        prior_end = plan["cuts"][i-1]["end"] if i else None
        boundaries = ([cut["start"]] if prior_end!=cut["start"] else [])+(
            [cut["end"]] if next_start!=cut["end"] else [])
        for stamp in boundaries:
            if any(w[4]>=.7 and w[1]+.085<stamp<w[2]-.085 for w in words):
                hard.append(f"Cut at {stamp:.2f}s intersects a confident spoken word.")
    cues = report["captions"]
    if any(a["end"]>b["start"]+.001 for a,b in zip(cues,cues[1:])):
        hard.append("Rendered phrase captions overlap in presentation time.")
    if plan.get("render_style"):
        expiry = report["opening"]["hook_end"]
        if expiry>=plan["proof"]["edit"] or any(cue["start"]<expiry for cue in cues):
            hard.append("Opening hook overlaps the payoff or speech subtitles.")
        if not (target/"dialogue.ass").is_file() or report.get("rendered_style")!=plan["render_style"]:
            hard.append("The saved moving template or separated caption track failed verification.")
    if plan["beats"]["action"]["edit"]>OPENING_LIMIT:
        hard.append("The first meaningful action arrives after 12 seconds.")
    for value,size,limit in ((plan["hook"],65,25),(plan["credit"],33,45)):
        if max((len(x) for x in value.split()),default=0)*size>805 or len(value)>limit*3:
            hard.append("Overlay text risks crossing the portrait safe area.")
            break
    snd = media.run([media.require("ffmpeg"),"-v","error","-xerror","-i",str(path),"-map","0:a:0",
                    "-ac","1","-ar","8000","-c:a","pcm_s16le","-f","s16le","-"],90)
    samples = array("h",snd.stdout)
    if snd.returncode or not samples or rms(samples)<12:
        hard.append("Decoded original audio is unavailable or silent.")
    for left,right in zip(plan["cuts"],plan["cuts"][1:]):
        if left["end"]==right["start"]:
            continue
        clock = int(right["edit_start"]*8000)
        before = rms(samples[max(0,clock-1120):clock-160])
        after = rms(samples[clock+160:clock+1120])
        if min(before,after)>0 and abs(20*math.log10(after/before))>16:
            warnings.append(f"Audio levels change sharply at +{right['edit_start']:.2f}s.")
    if not audition:
        full = media.run([media.require("ffmpeg"),"-v","error","-xerror","-i",str(path),"-map","0:v:0",
                         "-map","0:a:0","-f","null","-"],120)
        if full.returncode:
            hard.append("Full master decode failed.")
    result = {"passed":not hard,"issues":hard,"warnings":warnings,"source_checks":visual,"release_length":length,
              "profile":"audition" if audition else "master"}
    streamer_asr.write(target/"qc.json",result)
    return result


def is_reordered(plan):
    return any(b["start"]<a["start"] for a,b in zip(plan["cuts"],plan["cuts"][1:]))


def process(root,row,selection,transcript,config,checkpoint,update,*,first_run=None,destination=None):
    job = row["id"]
    folder = root/"streamer-runs"/str(job)
    output = destination or folder/("production-revisit.json" if first_run else "production.json")
    if output.is_file():
        return streamer_asr.read(output)
    setting = dict(config["stream"])
    with connect(root) as db:
        saved = db.execute("""SELECT c.camera FROM streamer_creators c JOIN streamer_accounts a
                              ON a.creator=c.id WHERE a.id=?""",(setting.get("account"),)).fetchone()
    if saved and saved["camera"]:
        setting["camera"] = saved["camera"]
    available = {c["id"]:c for c in selection["proposals"]}
    duration = selection["asset"]["duration"]
    remaining = Counter({i:hour["qualified"] for i,hour in enumerate(first_run["hours"])}) if first_run else Counter()
    auditioned = Counter()
    status = []
    limit = ""
    for identity in selection["shortlist"]:
        candidate = available[identity]
        bucket = hour_index(payoff(candidate),duration)
        if remaining[bucket]>=config["hourly_target"]:
            status.append({"id":identity,"state":"held","reason":"Target already met in this source hour."})
            continue
        with connect(root) as db:
            times = json.loads(db.execute("SELECT timings FROM jobs WHERE id=?",(job,)).fetchone()[0])
        if sum(times.get(stage,0) for stage in ("Audition","Render","QC")) >= config.get("editor_minutes",60)*60:
            limit = "The configured editing time budget was reached."
            status.append({"id":identity,"state":"held","reason":limit})
            continue
        update(f"Planning {identity} for source hour {bucket+1}")
        plans = []
        from . import portal_templates
        with connect(root,True) as db:
            style = portal_templates.assign(db,job,identity,config.get("template_plan"))
        related = callback_proof(candidate,selection["proposals"])
        for fields in structures(candidate,transcript,config,duration,related=related):
            try:
                plans.append(dict(streamer_edits.pin(root,job,identity,fields,selection,row["revision"],
                                                     setting,planned_style=style),origin="automatic"))
            except ValueError:
                continue
        if not plans:
            status.append({"id":identity,"state":"held",
                           "reason":f"No evidence-aligned assembly within {shorts_release.RULE}."})
            continue
        audition_reports = []
        # Test up to three supported structures for the strongest two stories in each source hour.
        chosen = min(plans,key=lambda plan:(plan["beats"]["action"]["edit"]>OPENING_LIMIT,
                                           is_reordered(plan),plan["duration"]))
        if auditioned[bucket] < 2 and candidate.get("visual_check",{}).get("verdict")=="accept":
            auditioned[bucket] += 1
            for plan in plans:
                version,_ = streamer_edits.encoded_plan(plan)
                path = folder/identity/"auditions"/version
                def audition():
                    streamer_edit_render.render(root,path,plan,audition=True)
                    return qc(root,path,plan,transcript,audition=True)
                try:
                    check = checkpoint("Audition",audition)
                except media.MediaError as error:
                    check = {"passed":False,"issues":[str(error)],"warnings":[]}
                audition_reports.append({"id":version,"title":plan["title"],"reordered":is_reordered(plan),**check})
            tested = [(plan,test) for plan,test in zip(plans,audition_reports) if test["passed"]]
            if tested:
                # The proof-first alternate and the edited timeline are available for review, even when continuity wins.
                chosen,_ = min(tested,key=lambda pair:(len(pair[1]["warnings"]),
                    is_reordered(pair[0]),pair[0]["duration"]))
        with connect(root,True) as db:
            version = streamer_edits.register(db,chosen,queued=False)
        target = streamer_edits.directory(root,job,identity,version)
        try:
            checkpoint("Render",lambda:streamer_edit_render.render(root,target,chosen))
            check = checkpoint("QC",lambda:qc(root,target,chosen,transcript,audition=False))
        except media.MediaError as error:
            with connect(root,True) as db:
                db.execute("UPDATE streamer_editions SET render_state='failed',error=? WHERE id=?",(str(error),version))
            status.append({"id":identity,"state":"failed","edition":version,"reason":str(error),"auditions":audition_reports})
            continue
        verified = candidate.get("visual_check",{}).get("verdict")=="accept"
        qualified = check["passed"] and verified
        with connect(root,True) as db:
            db.execute("UPDATE streamer_editions SET render_state='exported',qualification=?,error='' WHERE id=?",
                       ("qualified" if qualified else "candidate",version))
        if qualified:
            remaining[bucket] += 1
        reason = "Visual evidence and automated QC passed." if qualified else (
            "; ".join(check["issues"]) or "Visual evidence requires a reviewer.")
        status.append({"id":identity,"hour":bucket+1,"state":"qualified" if qualified else "rework",
                       "edition":version,"reason":reason,"auditions":audition_reports,"qc":check})
    hours = [{"hour":index+1,"qualified":remaining[index],"target":config["hourly_target"],
              "gap":max(0,config["hourly_target"]-remaining[index]),"complete":(index+1)*HOUR<=duration}
             for index in range(math.ceil(duration/HOUR))]
    results = {"items":status,"hours":hours,"qualified":sum(remaining.values()),"source_seconds":duration}
    if limit:
        results["limit"] = limit
    streamer_asr.write(output,results)
    return results
