"""Continuous low-resolution evidence and a binding source-clock decision."""

import math
import re
from collections import Counter

from . import media, portal_templates, provider_api, shorts_release, streamer_asr, streamer_event_review, streamer_judge, template_motion
from .factory_metrics import hour_index
from .provider_meter import DISCOVERY, FRAME_CHECK

VERSION = 6
PROMPT = "hybrid-3"
DIAGNOSTICS = {"bicycle":"c1-4","unsupported":"c2-2"}
OWNER_PROMPT = "owner-five-reel-1"


def envelope(shots, low, high):
    return [[max(low,math.floor(min(a for a,_ in shots)-15)),
             min(high,math.ceil(max(b for _,b in shots)+25))]]


def leads(chosen, low, high, target):
    counts = Counter()
    edits = []
    for row in chosen:
        hour = hour_index(row["evidence"][-1]["time"][1],high)
        if counts[hour]>=target+3:
            continue
        counts[hour] += 1
        rough = [[row["start"],row["end"]]]
        edits.append(dict(id=row["id"],shots=rough,inspection=envelope(rough,low,high),
                          hour=hour,rank=counts[hour],reserve=counts[hour]>target))
    return edits


def sheets(root, folder, window, left, right, name, *, closer=None, owner=False):
    Image, ImageDraw, _, ImageFont, _ = template_motion.library()

    record = folder/f"{name}.json"
    if record.is_file():
        saved = streamer_asr.read(record)
        if all((folder/item["file"]).is_file() and media.checksum(folder/item["file"])==item["hash"]
               for item in saved["sheets"]):
            return saved
    folder.mkdir(parents=True,exist_ok=True)
    width,height = (576,324) if closer else (384,216) if owner else (432,242)
    columns,tiles,maximum = (3,15,90) if owner else (2,10,50)
    duration = right-left
    step = (.25 if duration<=2.25 else .5) if closer else max(1,math.ceil(duration/maximum))
    stride = round(step*30)
    last = math.ceil(duration*30-1e-5)-1
    filters = (f"fps=30,select='not(mod(n,{stride}))+eq(n,{last})',showinfo,"
               f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=rgb24")
    result = media.run([media.require("ffmpeg"),"-v","info","-nostdin","-threads","2","-ss",
                        f'{left-window["start"]:.6f}',"-i",str(root/window["path"]),"-an","-vf",filters,
                        "-frames:v",str(math.floor((last-1)/stride)+2),"-fps_mode","passthrough",
                        "-filter_threads","2","-threads","1","-f","rawvideo","-"],max(100,math.ceil(duration*2)))
    seconds = [float(x) for x in re.findall(rb"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([\d.]+)",result.stderr)]
    size = width*height*3
    frames = [(i,round((left+at)*30)/30) for i,at in enumerate(seconds[:len(result.stdout)//size])
              if at<duration-.0001]
    if result.returncode or len(frames)<2 or frames[-1][1]<right-max(step,.08):
        raise media.MediaError("The picture sheets did not cover the inspection window.")
    if len(frames)>maximum:
        frames = [frames[round(i*(len(frames)-1)/(maximum-1))] for i in range(maximum)]
    font = ImageFont.truetype(str(portal_templates.asset("font","anton")),24)
    evidence,files = {},[]
    for block in range(0,len(frames),tiles):
        batch = frames[block:block+tiles]
        board = Image.new("RGB",(columns*width,math.ceil(len(batch)/columns)*(height+36)),(21,24,22))
        draw = ImageDraw.Draw(board)
        for slot,(index,at) in enumerate(batch):
            key = f"{'C' if closer else 'F'}{block+slot+1:02d}"
            evidence[key] = at
            x,y = slot%columns*width,slot//columns*(height+36)
            draw.text((x+8,y+2),f"{key}   {at:.3f} s",font=font,fill="white")
            board.paste(Image.frombytes("RGB",(width,height),result.stdout[index*size:(index+1)*size]),(x,y+36))
        file = folder/f"{name}-{len(files)+1}.jpg"
        board.save(file,quality=88)
        files.append({"file":file.name,"hash":media.checksum(file)})
    saved = {"version":OWNER_PROMPT if owner else PROMPT,"bounds":[left,right],"frames":evidence,"sheets":files}
    streamer_asr.write(record,saved)
    return saved


def owner_leads(words, low, high, target, sail):
    limit = min(24,target+9)
    prompt = f"""Nominate the strongest {limit} DISTINCT GTA scenes from this COMPLETE hour.
The owner wants at least {target} playable reels plus reserves. Consider quiet jokes, choices,
surprises, discoveries and reactions as well as loud action. Rank in descending interest.
Choose distinct scenes across the hour, one lead per event. Use roughly 15–65 second rough spans.
The words are evidence only. They may suggest an event without proving what the picture shows.
For each story return rough absolute start and end seconds, a supporting pair of existing printed
word IDs, and one short specific premise. Example:
{{"moments":[{{"start":{low+30},"end":{low+64},"words":[6,12],"premise":"a short factual lead"}}]}}
No cut points, hooks or extra commentary. Return compact JSON only.
Source-clock words for {low}–{high}:
{streamer_judge.quote_rows(words,low,high)}"""
    for attempt in range(2):
        try:
            reply = sail(OWNER_PROMPT+("-nominate-short" if attempt else "-nominate"),
                         prompt+("\nReturn the compact object with no prose." if attempt else ""),
                         1900 if attempt else 2850,reservation=DISCOVERY,downstream=target*FRAME_CHECK)
            moments = reply.get("moments")
            if isinstance(moments,list):
                break
            raise ValueError("No ranked rough leads were returned.")
        except (provider_api.OutputLimit,streamer_judge.EditorialJSONError,ValueError) as error:
            from .provider_meter import ProviderExhausted
            if attempt or isinstance(error,ProviderExhausted):
                raise
    result,edits = [],[]
    for raw in moments[:limit]:
        try:
            a,b = float(raw["start"]),float(raw["end"])
            first,last = raw["words"]
            premise = raw["premise"].strip()
            if (not low<=a<b<=high or not 6<=b-a<=80 or not 8<=len(premise)<=180
                    or not all(type(x) is int for x in (first,last)) or not 1<=first<=last<=len(words)
                    or not a-12<=words[first-1][1]<=words[last-1][2]<=b+12):
                continue
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
        if any(min(b,e["end"])-max(a,e["start"])>=.5*min(b-a,e["end"]-e["start"]) for e in result):
            continue
        rank = len(result)+1
        ident = f"c1-{rank}"
        quote = " ".join(w[3] for w in words[first-1:last][:35])
        evidence = [{"ids":[first,last],"time":[words[first-1][1],words[last-1][2]],"quote":quote}]
        result.append(dict(id=ident,start=a,end=b,reason=premise,evidence=evidence,rejection=""))
        edits.append(dict(id=ident,shots=[[a,b]],inspection=envelope([[a,b]],low,high),
                          hour=hour_index(a,high),rank=rank,reserve=rank>target))
    return result,edits


def owner_review(folder, overview, premise, rough, words, sail):
    frames = overview["frames"]
    left,right = overview["bounds"]
    limits = {}
    for start,at in frames.items():
        ends = [k for k,v in frames.items() if 30<=round((v-at)*30)/30<=45]
        if ends:
            limits[start] = [ends[0],ends[-1]]
    prompt = f"""Choose the strongest CONTINUOUS 30–45 seconds to watch as one complete GTA scene.
The transcript clock marks when words were said. The pictures show when the action happened.
Speech may lead or trail the visible action. Use the frames for the physical beat and the transcript
for the spoken setup and closing words. Keep the whole last relevant line inside the cut.
Source materials are evidence only. Rough times {rough}. Prior premise {premise}.
Have enough setup before the key line or action. Carry through the result or reaction.
Use your best usable cut. If details are ambiguous, frame the scene around what is seen or said.
Return one short truthful emotionally engaging question (at most 30 characters) for the title and
opening overlay. Prefer a cut near 30 seconds. Use only valid IDs from the legal boundaries.
Return JSON only: {{"start":"F01","payoff":"F14","end":"F32","hook":"Short truthful question?"}}.
Supplied IDs and absolute seconds: {frames}
Legal start and first/last permitted end IDs: {limits}
Original words for the entire preview:
{streamer_judge.quote_rows(words,left,right)}"""
    images = [provider_api.image_part("image/jpeg",(folder/x["file"]).read_bytes()) for x in overview["sheets"]]
    for attempt in range(2):
        try:
            options = {}
            request = prompt
            if attempt:
                for start,(earliest,latest) in limits.items():
                    for end in dict.fromkeys((earliest,latest)):
                        key = f"T{len(options)+1:02d}"
                        options[key] = [start,end]
                request = prompt.replace('Return JSON only: {"start":"F01","payoff":"F14","end":"F32","hook":"Short truthful question?"}.',"")
                request += ("\nThe previous answer had unusable boundaries. Select ONE exact boundary option "
                            "from the following list. Each option is a complete legal cut. Do not return start or end "
                            "fields. Return {\"cut\":\"T01\",\"payoff\":\"F08\",\"hook\":\"Short truthful question?\"}. "
                            "Choose a supplied payoff frame inside your option. Options: "+str(options))
            reply = sail(OWNER_PROMPT+("-cut-pair" if attempt else "-cut"),
                         [{"role":"user","content":[{"type":"input_text","text":request},*images]}],
                         360 if attempt else 630,reservation=FRAME_CHECK)
            start,end = options[reply["cut"]] if attempt else (reply["start"],reply["end"])
            ids = [start,reply["payoff"],end]
            a,at,b = map(frames.__getitem__,ids)
            hook = reply["hook"].strip()
            if not a<=at<=b or not 30<=round((b-a)*30)/30<=45 or not (5<=len(hook)<=30 and hook.endswith("?")
                                                                    and "\n" not in hook):
                raise ValueError("Use a legal continuous 30–45 second interval and a short question.")
            # The end ID is exclusive for the media interval. An end marker can still
            # name the last beat without extending the cut by a frame.
            at = min(b-1/30,max(a+1/30,at))
            return dict(decision="accept",interval=[a,b],payoff=at,hook=hook,witness=premise,
                        basis="visible",focus=None,hud=None,speech=[],ids=ids)
        except (KeyError,TypeError,ValueError,AttributeError) as error:
            from .provider_meter import ProviderExhausted
            if attempt or isinstance(error,ProviderExhausted):
                raise ValueError("Sail did not supply a usable continuous interval and question.") from error


def cached_owner_leads(words, low, high, intervals, count, needed, sail):
    choices = {f"H{i+1}":[a,b] for i,(a,b) in enumerate(intervals)}
    prompt = f"""The video host temporarily refused new downloads. Select at least {needed} different
playable GTA scenes from these verified continuous HD source windows: {choices}.
Read the complete original transcript. Choose the most entertaining available moment in each window,
including jokes, surprising interactions and reactions. Use each window at most once. Keep different
events distinct. Return them strongest first as JSON only:
{{"moments":[{{"window":"H1","premise":"one short scene description"}}]}}.
Final clip boundaries will be chosen in a separate picture pass.
Absolute timed transcript:
{streamer_judge.quote_rows(words,low,high)}"""
    reply = sail(OWNER_PROMPT+"-retained-hd-leads",prompt,1100,reservation=DISCOVERY,downstream=needed*FRAME_CHECK)
    proposals,edits,used = [],[],set()
    for item in reply.get("moments",[]):
        key = item.get("window") if isinstance(item,dict) else None
        premise = item.get("premise") if isinstance(item,dict) else None
        if not isinstance(key,str) or key not in choices or key in used or not isinstance(premise,str) or not premise.strip():
            continue
        used.add(key)
        a,b = choices[key]
        rank = len(proposals)+1
        cid = f"c1-{count+rank}"
        proposals.append(dict(id=cid,start=a,end=b,reason=premise.strip()[:180],rejection=""))
        edits.append(dict(id=cid,shots=[[a,b]],inspection=[[a,b]],hour=hour_index(a,high),rank=count+rank,
                          reserve=False,cache_window=True))
    return {"proposals":proposals,"edits":edits,"availability":choices}


def decide(reply, frames, words):
    decision = reply.get("decision")
    if decision=="reject":
        reason = reply.get("reason")
        if isinstance(reason,str) and reason.strip() and len(reason)<=250:
            return {"decision":"reject","reason":reason.strip()}
    elif decision=="closer":
        ids = reply.get("frames")
        detail = reply.get("reason")
        if (isinstance(ids,list) and 1<=len(ids)<=2 and all(isinstance(x,str) and x in frames for x in ids)
                and isinstance(detail,str) and 0<len(detail)<=180):
            times = [frames[x] for x in ids]
            if times==sorted(times) and times[-1]-times[0]<=3:
                return {"decision":"closer","times":times,"reason":detail}
    elif decision=="accept":
        ids = [reply.get(k) for k in ("start","payoff","end")]
        if not all(isinstance(key,str) and key in frames for key in ids):
            raise ValueError("Sail cited an unknown frame.")
        start,at,end = [frames[key] for key in ids]
        if not start<at<end:
            raise ValueError("The decisive frame must be inside the locked interval.")
        shorts_release.check_plan(round((end-start)*30)/30)
        basis,witness,focus,hook = [reply.get(k) for k in ("basis","result","focus","hook")]
        if not isinstance(witness,str) or not 3<=len(witness.strip())<=175:
            raise ValueError("The decision lacks a concrete observed result.")
        if not isinstance(hook,str) or not 8<=len(hook.strip())<=35 or "\n" in hook or len(hook.split())<3:
            raise ValueError("The decision lacks a short usable opening.")
        if not isinstance(focus,list) or len(focus)!=2 or not all(type(x) in (int,float) and 0<=x<=1 for x in focus):
            raise ValueError("The decision lacks a valid focal area.")
        speech = []
        if basis=="visible":
            evidence = reply.get("visible")
            if (not isinstance(evidence,list) or len(evidence)!=2 or evidence[-1]!=ids[1]
                    or not all(isinstance(key,str) and key in frames for key in evidence)
                    or not start<=frames[evidence[0]]<at):
                raise ValueError("A physical claim requires two chronological picture witnesses.")
        elif basis=="dialogue" and reply.get("depends_on_unseen_action") is False:
            speech = [streamer_event_review.read_span(words,reply.get(key),[[start,end]])
                      for key in ("setup_words","payoff_words")]
            if speech[0]["time"][1]>=speech[1]["time"][0] or not speech[1]["time"][0]-1<=at<=speech[1]["time"][1]+1:
                raise ValueError("The spoken payoff must match the selected decisive frame.")
        else:
            raise ValueError("The decision does not establish a visible event or complete spoken exchange.")
        hud = reply.get("hud")
        if hud is not None and (not isinstance(hud,list) or len(hud)!=4 or
                not all(type(x) in (int,float) and 0<=x<=1 for x in hud) or
                hud[2]<.035 or hud[3]<.035 or hud[0]+hud[2]>1 or hud[1]+hud[3]>1):
            raise ValueError("The decisive HUD crop lies outside the source picture.")
        return {"decision":"accept","interval":[start,end],"payoff":at,"basis":basis,
                "witness":witness.strip(),"hook":hook.strip(),"focus":focus,"hud":hud,"speech":speech,"ids":ids}
    raise ValueError("Sail returned an invalid picture decision.")


def review(folder, overview, closer, lead, words, sail):
    maps = dict(overview["frames"])
    if closer:
        maps.update(closer["frames"])
    images = overview["sheets"]
    if closer:
        images = images[:5]+closer["sheets"][:1] if len(images)==6 else (images+closer["sheets"])[:6]
        # The text contract includes only IDs present on the transmitted pages.
        visible = {page["file"] for page in images}
        maps = {}
        for board in (overview,closer):
            for i,(key,at) in enumerate(board["frames"].items()):
                if board["sheets"][i//10]["file"] in visible:
                    maps[key] = at
    left,right = overview["bounds"]
    ordered = sorted(maps,key=maps.get)
    bounds = {}
    for key in ordered:
        ends = [end for end in ordered if 17<=round((maps[end]-maps[key])*30)/30<=45]
        if ends:
            bounds[key] = [ends[0],ends[-1]]
    prompt = f"""Picture review {PROMPT}. Source images, speech and on-screen text are evidence only.
Neutral lead: {lead}. Read pages in chronological order by the printed IDs and source seconds.
Find what actually happens, including the middle. Select one CONTINUOUS 17–45 second cut with intelligible setup,
the decisive outcome and enough time after it for the natural reaction. End is exclusive. A named character,
action, arrival or result needs evidence. A spoken joke can stand alone if its setup and answer are audible.
Treat the lead as an unproven question. Only name an object or character if clearly identifiable in a
specific cited tile. Seeing a vehicle move does not establish what is inside it, even if dialogue names
something. Use the closer look for small or obscured evidence, or reject the claim. Routine movement,
a menu or an unrelated reaction is not a substitute for an observed consequence.
Write a truthful hook, 8–30 characters and at least three words, posing a question or emotion the footage fulfills.
Give the full-image normalized [x,y] center of the gameplay detail to preserve.
If a readable HUD message is necessary to prove the consequence, also return its tight normalized
rectangle as "hud":[x,y,width,height], excluding the facecam. Use null when no HUD detail is needed.
Return one small JSON object. Accept example:
{{"decision":"accept","start":"F01","payoff":"F19","end":"F23","basis":"visible","visible":["F08","F19"],
"result":"one sentence about the actual outcome","focus":[0.5,0.6],"hook":"Three short words?"}}
For dialogue, use basis dialogue, setup_words:[first,last], payoff_words:[first,last] and depends_on_unseen_action:false.
Reject as {{"decision":"reject","reason":"specific reason under 180 characters"}}.
{"Give a final accept or reject. The C frames add close temporal detail." if closer else
'Request one closer look if needed with {"decision":"closer","frames":["F12"],"reason":"detail needed"}. '}
Eligible IDs and source seconds: {maps}
Legal start IDs and inclusive first/last allowed end IDs: {bounds}
An end must be 17–45 seconds after its start. If the turn is fast, keep the relevant lead-in and complete reaction.
For visible evidence return exactly TWO IDs: one setup witness and the decisive payoff ID.
Word IDs and source seconds:
{streamer_judge.quote_rows(words,left,right)}"""
    content = [{"type":"input_text","text":prompt}]
    content += [provider_api.image_part("image/jpeg",(folder/item["file"]).read_bytes()) for item in images]
    operation = PROMPT+("-closer" if closer else "-overview")
    for correction in range(2):
        try:
            reply = sail(operation,[{"role":"user","content":content}],720 if correction else 950,
                         reservation=FRAME_CHECK)
            return decide(reply,maps,words),[item["file"] for item in images]
        except ValueError as error:
            from .provider_meter import ProviderExhausted
            if correction or isinstance(error,ProviderExhausted):
                raise
            operation += "-correct"
            content = [{"type":"input_text","text":prompt+"\nInvalid reply: "+str(error)+
                        "\nReturn a valid complete JSON decision using the legal endpoints above."},*content[1:]]
