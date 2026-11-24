"""Assemble a frame-quantized source edit with independently joined original audio."""

import json
import math
import subprocess
import sys
from textwrap import wrap

from . import media, replay_cache, streamer_asr, streamer_layout
from .streamer_render import ass_escape, ass_time, facecam, inspect_media

WIDTH, HEIGHT = 1080, 1920
GAME_ROWS = 1056
STYLES = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,65,&H00FFFFFF,&H00FFFFFF,&H000D0E10,&H000D0E10,-1,0,0,0,100,100,0,0,1,3.5,0,5,100,160,0,1
Style: Context,Arial,35,&H00CCD7DD,&H00FFFFFF,&H000D0E10,&H000D0E10,-1,0,0,0,100,100,0,0,1,2,0,5,100,160,0,1
Style: Caption,Arial,60,&H00FFFFFF,&H00FFFFFF,&H000D0E10,&H000D0E10,-1,0,0,0,100,100,0,0,1,5,0,5,100,160,0,1
Style: Credit,Arial,33,&H00CED2D5,&H00FFFFFF,&H000D0E10,&H000D0E10,0,0,0,0,100,100,0,0,1,2,0,5,100,160,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""


def phrase_cues(words, cuts, *, minimum_confidence=.45):
    cues = []

    def flush(group):
        if group:
            cues.append({"start": group[0][1], "end": group[-1][2],
                         "text": " ".join(w[3] for w in group), "emphasis": ""})

    blocks = []
    for cut in cuts:
        if blocks and abs(blocks[-1][1]-cut["start"]) < .001:
            blocks[-1][1] = cut["end"]
        else:
            blocks.append([cut["start"], cut["end"]])
    for start, end in blocks:
        group = []
        for word in words:
            if word[1] < start or word[2] > end or word[4] < minimum_confidence:
                continue
            if group and (word[1]-group[-1][2] > .36 or len(group) >= 7 or
                          len(" ".join(w[3] for w in group+[word])) > 38):
                flush(group)
                group = []
            group.append(word)
            if word[3].endswith((".", "?", "!")):
                flush(group)
                group = []
        flush(group)
    return cues


def captions(transcript, plan):
    corrections = plan["captions"]
    minimum_confidence = 0 if plan.get("story_v4") else .45
    cues = [cue for cue in phrase_cues(transcript["words"], plan["cuts"],minimum_confidence=minimum_confidence)
            if not any(other["start"] < cue["end"] and cue["start"] < other["end"] for other in corrections)]
    cues.extend(corrections)
    mapped = []
    for cue in sorted(cues, key=lambda item: item["start"]):
        for cut in plan["cuts"]:
            left, right = max(cut["start"], cue["start"]), min(cut["end"], cue["end"])
            if right <= left or not cue["text"]:
                continue
            out = dict(cue, source=[left,right], start=round(cut["edit_start"]+left-cut["start"],6),
                       end=round(cut["edit_start"]+right-cut["start"],6))
            mapped.append(out)
    mapped.sort(key=lambda cue:cue["start"])
    for prior,after in zip(mapped,mapped[1:]):
        prior["end"] = min(prior["end"],after["start"])
    return [cue for cue in mapped if cue["end"]-cue["start"] >= .04]


def initial_dialogue(cues,words,plan,expiry):
    later = [cue for cue in cues if cue["start"]>=expiry+.035]
    for index,cue in enumerate(later):
        start = cue["source"][0]
        before = next((word for word in reversed(words) if word[1]<start-.01),None)
        verified = any(abs(mark["start"]-start)<.02 and mark["text"] for mark in plan["captions"])
        if not before or verified or start-before[2]>=.26 or before[3].endswith((".","?","!")):
            return later[index:]
    return []


def subtitles(path, cues, plan, *, dialogue_only=False, notices_after=None):
    lines = [STYLES]
    standard = plan["recipe"] == streamer_layout.RECIPE

    def item(style, start, end, y, text):
        if ass_time(end) != ass_time(start):
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,"
                         + r"{\pos(" + f"510,{y}" + ")}" + text)

    if not dialogue_only:
        item("Hook", 0, min(2.9, plan["duration"]), 795 if standard else 170,
             r"\N".join(ass_escape(line) for line in wrap(plan["hook"], 25)))
        item("Context", 0, plan["duration"], 915 if standard else 281, ass_escape(plan["context"]))
        item("Credit", 0, plan["duration"], 1780 if standard else 1705, ass_escape(plan["credit"]))
        for cut in plan["cuts"]:
            if cue := cut.get("time_cue"):
                item("Context",cut["edit_start"],min(cut["edit_end"],cut["edit_start"]+1.8),1420,ass_escape(cue))
    for cue in cues:
        text = cue["text"]
        term = cue["emphasis"]
        if term:
            at = text.casefold().find(term.casefold())
            rendered = (ass_escape(text[:at]) + r"{\1c&H6CEAFF&}" + ass_escape(text[at:at+len(term)])
                        + r"{\1c&HFFFFFF&}" + ass_escape(text[at+len(term):]))
        else:
            rendered = ass_escape(text)
        if len(text) > 37:
            positions = [i for i,char in enumerate(text) if char == " " and
                         (not term or not at <= i < at+len(term))]
            if positions:
                split = min(positions, key=lambda pos: abs(pos-len(text)/2))
                split_at = split + (len(rendered)-len(text) if term and split > at+len(term) else 0)
                rendered = rendered[:split_at]+r"\N"+rendered[split_at+1:]
        if term and plan.get("origin")=="automatic":
            rendered = r"{\fad(65,75)}"+rendered
        item("Caption", cue["start"], cue["end"], 1548, rendered)
    if notices_after is not None:
        for cut in plan["cuts"]:
            if (cue := cut.get("time_cue")) and cut["edit_start"]>=notices_after:
                item("Context",cut["edit_start"],min(cut["edit_end"],cut["edit_start"]+1.4),1150,ass_escape(cue))
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def tracking(keys, axis, start):
    if not keys:
        return "0.5"
    last = f"{keys[-1][axis]:.6f}"
    for before, after in reversed(list(zip(keys, keys[1:]))):
        a, b = before[0]-start, after[0]-start
        x, y = before[axis], after[axis]
        last = f"if(lt(t,{b:.5f}),{x:.6f}+({y-x:.6f})*clip((t-({a:.5f}))/{b-a:.5f},0,1),{last})"
    return last


def game_filter(cut, camera, source_size, index):
    # Keep the subject inside the square gameplay window. Pan between source-time positions.
    wide = "scale=1080:608:force_original_aspect_ratio=decrease,pad=1080:608:(ow-iw)/2:(oh-ih)/2"
    if cut["layout"] == "focus":
        x = tracking(cut["keys"], 1, cut["start"])
        y = tracking(cut["keys"], 2, cut["start"])
        game = (f"crop=w='trunc(min(iw,ih)*0.5)*2':h='trunc(min(iw,ih)*0.5)*2':"
                f"x='clip(iw*({x})-ow/2,0,iw-ow)':y='clip(ih*({y})-oh/2,0,ih-oh)',scale=1080:{GAME_ROWS},setsar=1")
        return f"[v{index}]{game},pad=1080:1920:0:328:color=0x101215[shot{index}]"
    if camera and cut["layout"] == "reaction":
        width, height = source_size
        x, y, w, h = [math.floor(value*size/2)*2
                      for value,size in zip(camera,(width,height,width,height))]
        # Frame the game to the right of the original inset instead of painting over it.
        exclude = x+w if x+w < width*.45 else 0
        trim_right = x if x > width*.55 else width
        return (f"[v{index}]split[face{index}][play{index}];"
                f"[face{index}]crop={w}:{h}:{x}:{y},scale=880:556:force_original_aspect_ratio=decrease,"
                f"pad=1080:556:(ow-iw)/2:(oh-ih)/2:color=0x101215,setsar=1[top{index}];"
                f"[play{index}]crop={trim_right-exclude}:ih:{exclude}:0,{wide},setsar=1[game{index}];"
                f"[top{index}][game{index}]vstack,pad=1080:1920:0:317:color=0x101215[shot{index}]")
    return f"[v{index}]{wide},setsar=1,pad=1080:1920:0:505:color=0x101215[shot{index}]"


def split_filter(cut, camera, game, source_size, index, hud=None, hud_time=None):
    width, height = source_size
    x,y,w,h = streamer_layout.pixels(camera,width,height)
    gx,gy,gw,gh = streamer_layout.pixels(game,width,height)
    out, face, rows = streamer_layout.WIDTH, streamer_layout.FACE_ROWS, streamer_layout.GAME_ROWS
    # Use one decoded picture for both panels, so a seek, preview or reorder moves both together.
    center_x = tracking(cut["keys"],1,cut["start"])
    center_y = tracking(cut["keys"],2,cut["start"]) if cut["keys"] else ".60"
    source = (f"[v{index}]split=3[face{index}][play{index}][hud{index}];" if hud
              else f"[v{index}]split[face{index}][play{index}];")
    graph = (source +
            f"[face{index}]crop={w}:{h}:{x}:{y},scale={out}:{face}:force_original_aspect_ratio=increase:"
            f"flags=lanczos,crop={out}:{face},setsar=1[top{index}];"
            f"[play{index}]crop={gw}:{gh}:{gx}:{gy},"
            f"crop=w='trunc(min(iw,ih*{out}/{rows})/2)*2':h='trunc(min(ih,iw*{rows}/{out})/2)*2':"
            f"x='clip({width}*({center_x})-{gx}-ow/2,0,iw-ow)':"
            f"y='clip({height}*({center_y})-{gy}-oh/2,0,ih-oh)',"
            f"scale={out}:{rows}:flags=lanczos,setsar=1[game{index}];")
    game_output = f"game{index}"
    if hud:
        hx,hy,hw,hh = streamer_layout.pixels(hud,width,height)
        hud_width,hud_height,right = (530,178,1016) if hud_time is not None else (206,148,948)
        enable = f":enable='between(t,{max(0,hud_time-1):.3f},{hud_time+7:.3f})'" if hud_time is not None else ""
        graph += (f"[hud{index}]crop={hw}:{hh}:{hx}:{hy},scale={hud_width}:{hud_height}:"
                  f"force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1[status{index}];"
                  f"[game{index}][status{index}]overlay=x={right}-w:y=291{enable}[playhud{index}];")
        game_output = f"playhud{index}"
    return graph + f"[top{index}][{game_output}]vstack[shot{index}]"


def motion_graph(args, filters, art, plan, offset):
    def still(name):
        nonlocal offset
        args.extend(["-loop","1","-framerate",str(plan["fps"]),"-i",name])
        offset += 1
        return f"{offset-1}:v"

    video = "assembled"
    if art["gold"]:
        mask = still("camera-mask.png")
        filters.extend([
            "[assembled]split[wholeface][wholegame]",
            "[wholegame]crop=1080:1248:0:672[playarea]",
            "[wholeface]crop=1080:672:0:0,split[backing][inset]",
            "[backing]gblur=sigma=30,eq=brightness=-0.06:saturation=0.8[soft]",
            "[inset]scale=920:572:force_original_aspect_ratio=increase:flags=lanczos,crop=920:572,setsar=1[smallface]",
            f"[{mask}]format=gray[rounded]",
            "[smallface][rounded]alphamerge=shortest=1[cutout]",
            "[soft][cutout]overlay=80:36:shortest=1[framed]",
            "[framed][playarea]vstack[goldbase]",
        ])
        video = "goldbase"
    if art["persistent"]:
        decor = still("decoration.png")
        filters.append(f"[{video}][{decor}]overlay=shortest=1[decorated]")
        video = "decorated"
    graphic = still("opening.png")
    duration = art["hook_end"]
    fade = art["fade"]
    if fade:
        filters.append(f"[{graphic}]format=rgba,fade=t=out:st={duration-fade:.6f}:d={fade:.6f}:alpha=1[titlealpha]")
        graphic = "titlealpha"
    filters.append(f"[{video}][{graphic}]overlay=shortest=1:enable='lt(t,{duration:.6f})'[styled]")
    return "styled"


def render(root, target, plan, *, audition=False):
    from . import shorts_release, streamer_edits

    bounds = shorts_release.bounds_for(plan)
    shorts_release.check_plan(plan["duration"],bounds=bounds)
    master = (root / plan["archive"]).resolve()
    transcript_file = (root / plan["transcript"]).resolve()
    if (not master.is_relative_to((root/"masters").resolve()) or
            not transcript_file.is_relative_to((root/"streamer-transcripts").resolve()) or
            media.checksum(master) != plan["asset"] or media.checksum(transcript_file) != plan["transcript_hash"]):
        raise media.MediaError("The retained archive or transcript differs from the pinned source revision.")
    for window in plan.get("source_windows",[]):
        path = (root/window["path"]).resolve()
        allowed = path.is_relative_to((root/"streamer-intake").resolve()) or replay_cache.is_local_range(root,plan["job"],path)
        if not allowed or media.checksum(path)!=window["hash"]:
            raise media.MediaError("The selected video differs from the pinned source range.")
    if (target/"portrait.mp4").is_file() and (target/"manifest.json").is_file():
        manifest = streamer_asr.read(target/"manifest.json")
        if (manifest["plan"] == plan and media.checksum(target/"portrait.mp4") == manifest["portrait_sha256"]
                and (target/"captions.ass").is_file()):
            shorts_release.require(target/"portrait.mp4",bounds=bounds)
            return
        raise media.MediaError("A saved edition failed its integrity check.")
    target.mkdir(parents=True, exist_ok=True)
    first = plan["cuts"][0]
    picture, _ = streamer_edits.source_input(root,plan,first["start"],first["end"])
    source_width, source_height, _ = inspect_media(picture)
    source_sizes = {picture: (source_width,source_height)}
    standard = plan["recipe"] == streamer_layout.RECIPE
    local_asr = str(media.TOOLS/"streamer-asr")
    if local_asr not in sys.path:
        sys.path.insert(0, local_asr)
    transcript = streamer_asr.read(transcript_file)
    complete_cues = captions(transcript, plan)
    cues = complete_cues
    art = None
    if plan.get("render_style"):
        from . import template_motion

        art = template_motion.save_layers(target,plan)
        # Keep natural phrase boundaries and retain the original speech in the optional track.
        cues = [cue for cue in complete_cues if cue["start"]>=art["hook_end"]+.035]
        if plan["render_style"]["treatment"]==template_motion.DIALOGUE_VERSION:
            cues = initial_dialogue(complete_cues,transcript["words"],plan,art["hook_end"])
            optional = dict(plan,captions=[dict(mark,text=mark["text"] or "[unclear]") for mark in plan["captions"]])
            complete_cues = captions(transcript,optional)
        subtitles(target/"dialogue.ass",cues,plan,dialogue_only=True,notices_after=art["hook_end"])
    subtitles(target/"captions.ass", complete_cues, plan,dialogue_only=bool(art))
    args, filters = [], []
    camera_results = []
    for index, cut in enumerate(plan["cuts"]):
        start, end = cut["start"], cut["end"]
        length = end-start
        picture, local = streamer_edits.source_input(root,plan,start,end)
        if picture not in source_sizes:
            source_sizes[picture] = inspect_media(picture)[:2]
        width,height = source_sizes[picture]
        args += ["-threads","2","-ss",f"{local:.6f}","-t",f"{length+.14:.6f}","-i",str(picture)]
        camera, label = (None, "Gameplay")
        if standard:
            camera,label = streamer_layout.evidence(picture,dict(cut,start=local,end=local+length),plan["camera"])
            game = streamer_layout.gameplay(camera,plan.get("game"))
            hud = plan.get("hud")
            if hud and streamer_layout.overlap(hud,camera):
                raise streamer_layout.FramingRequired("The HUD crop includes the camera. Adjust the HUD rectangle.")
        elif cut["layout"] == "reaction":
            camera, label = facecam(picture, local, local+length, plan["camera"])
        result = {"camera": camera, "note": label,
                  "layout": "wide" if cut["layout"] == "reaction" and not camera else cut["layout"]}
        if standard:
            result.update(game=game, hud=hud, camera_source_pixels=streamer_layout.pixels(camera,width,height)[2:])
        camera_results.append(result)
        frames = round(length*plan["fps"])
        filters.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={plan['fps']},trim=end_frame={frames}[v{index}]")
        hud_time = plan["proof"]["source"]-start if plan.get("hybrid_locked") and hud else None
        filters.append(split_filter(cut,camera,game,(width,height),index,hud,hud_time) if standard else
                       game_filter(cut,camera,(width,height),index))
    video = "".join(f"[shot{i}]" for i in range(len(plan["cuts"])))
    scale = ",scale=540:960" if audition else ""
    filters.append(f"{video}concat=n={len(plan['cuts'])}:v=1:a=0[assembled]")

    groups = []
    for cut in plan["cuts"]:
        if groups and not plan.get("source_windows") and abs(groups[-1]["end"]-cut["start"]) < .001:
            groups[-1]["end"] = cut["end"]
        else:
            groups.append({"start": cut["start"], "end": cut["end"], "handle": cut.get("audio_handle",0)})
    offset = len(plan["cuts"])
    for index, group in enumerate(groups):
        start = group["start"]-group["handle"]
        tail = groups[index+1]["handle"] if index+1 < len(groups) else 0
        length = group["end"]+tail-start
        music, local = streamer_edits.source_input(root,plan,start,group["end"]+tail)
        args += ["-ss",f"{local:.6f}","-t",f"{length+.09:.6f}","-i",str(music)]
        filters.append(f"[{index+offset}:a]aresample=48000,asetpts=PTS-STARTPTS,atrim=duration={length:.6f}[a{index}]")
        if index:
            left = "a0" if index == 1 else f"join{index-1}"
            join = (f"acrossfade=d={2*group['handle']:.6f}:c1=tri:c2=tri" if group["handle"]
                    else "concat=n=2:v=0:a=1")
            filters.append(f"[{left}][a{index}]{join}[join{index}]")
    audio = "a0" if len(groups) == 1 else f"join{len(groups)-1}"
    assembled = motion_graph(args,filters,art,plan,len(plan["cuts"])+len(groups)) if art else "assembled"
    subtitle_file = "dialogue.ass" if art else "captions.ass"
    filters.append(f"[{assembled}]ass={subtitle_file}{scale}[vout]")
    pending = target/"portrait.partial.mp4"
    command = [media.require("ffmpeg"),"-v","error","-nostdin","-y",*args,"-filter_complex_threads","2",
               "-filter_complex",";".join(filters),"-map","[vout]","-map",f"[{audio}]",
               "-c:v","libx264","-preset","veryfast","-crf","19","-threads","4","-pix_fmt","yuv420p",
               "-c:a","aac","-b:a","160k","-movie_timescale","48000","-movflags","+faststart",str(pending)]
    try:
        result = subprocess.run(command,cwd=target,stdin=subprocess.DEVNULL,capture_output=True,
                                timeout=max(420,math.ceil(plan["duration"]*8)))
    except subprocess.TimeoutExpired:
        raise media.MediaError("Edition encoding timed out. Use Retry export.") from None
    if result.returncode:
        detail = result.stderr.decode(errors="replace").splitlines()
        raise media.MediaError("FFmpeg could not assemble the edition. " + (detail[-1][:220] if detail else ""))
    info = media.probe(pending)
    w,h,_ = inspect_media(pending)
    dimensions = (540,960) if audition else (WIDTH,HEIGHT)
    if (w,h) != dimensions or not info["audio"] or abs(info["duration"]-plan["duration"]) > .12:
        raise media.MediaError("Edition failed its portrait, duration or audio check.")
    duration_check = shorts_release.require(pending,bounds=bounds)
    loudness = media.run([media.require("ffmpeg"),"-nostdin","-hide_banner","-nostats","-i",str(pending),
                          "-vn","-af","ebur128=peak=true","-f","null","-"],75)
    summary = loudness.stderr.decode(errors="replace").rpartition("Summary:")[2].strip()
    streamer_asr.write(target/"manifest.json", {"plan": plan, "source_size": [source_width,source_height],
        "captions": cues, "shots": camera_results, "audio_groups": groups, "loudness": summary,
        "profile": "audition" if audition else "master", "release_length": duration_check,
        **({"panels": [336,624] if audition else [672,1248]} if standard else {}),
        **({"rendered_style":plan["render_style"],"opening":art,"caption_track":complete_cues} if art else {}),
        "portrait_sha256": media.checksum(pending)})
    pending.replace(target/"portrait.mp4")
