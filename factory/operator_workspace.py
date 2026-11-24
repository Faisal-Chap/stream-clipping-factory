"""Stream-link directory and job presentation over the existing factory worker."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (factory_events, factory_portal, flashdata_profiles, portal_settings, readiness, replay_cache, source_queue, store, streamer_intake,
               streamer_run, streamer_story_v4, streamer_transfer, views)
from .library import identify
from . import streamer_catalog

h, form, button = views.h, views.form, views.button
ACTIVE = ("queued", "running")
STATES = {"queued":"Queued", "running":"Processing", "complete":"Processed",
          "stopped":"Paused", "failed":"Needs attention"}
PHASES = (
    ("Prepare replay", ("Acquire", "Archive", "Resolve")),
    ("Download recording", ("Download",)),
    ("Prepare audio", ("Audio",)),
    ("Transcribe audio", ("Transcribe", "Audio continuity")),
    ("Find stories with DeepSeek", ("Judge", "Select", "Cached HD reserves")),
    ("Build storyboard", ("Preview", "Picture sheets")),
    ("Choose exact cuts", ("Picture selection", "Picture review", "Closer look", "Visual check")),
    ("Retrieve HD footage", ("HD interval", "Selected video", "Join picture")),
    ("Render portraits", ("Render",)),
    ("Check exports", ("QC", "Review", "Portrait proof")),
)


def listing(db):
    return db.execute("""SELECT s.*,st.title,st.platform_id,st.live_status,q.platform,q.readiness,
                          c.enabled,COALESCE(m.duration,
                            (SELECT MAX(a.duration) FROM jobs j JOIN assets a ON a.hash=j.asset
                             WHERE j.source=s.id AND j.revision=s.revision),0) AS duration,
                          (SELECT COUNT(*) FROM done_stories d WHERE d.source=s.id) AS reels
                          FROM sources s LEFT JOIN source_queue q ON q.source=s.id
                          LEFT JOIN streamer_streams st ON st.source=s.id
                          LEFT JOIN streamer_accounts a ON a.id=st.account
                          LEFT JOIN streamer_creators c ON c.id=a.creator
                          LEFT JOIN portal_source_metadata m ON m.source=s.id ORDER BY s.id DESC""")


def title(source):
    return source["tracking_title"] or source["title"] or f'Source {source["id"]}'


def attempts(db, source):
    return db.execute("""SELECT j.*,
                          (SELECT MAX(utc) FROM factory_events WHERE job=j.id AND kind='finished') AS finished,
                          EXISTS(SELECT 1 FROM provider_calls WHERE job=j.id AND response_id IS NOT NULL
                                 AND result IS NULL AND next_try>0 AND provider='sail') AS awaiting,
                          EXISTS(SELECT 1 FROM provider_calls WHERE job=j.id AND response_id IS NOT NULL
                                 AND result IS NULL AND next_try>0 AND provider='flashdata') AS awaiting_flashdata
                          FROM jobs j WHERE j.source=? AND json_extract(j.settings,'$.method')='streamer'
                          ORDER BY j.id DESC""",(source,)).fetchall()


def span(root, job, length):
    settings = json.loads(job["settings"])
    status = streamer_intake.read(streamer_run.directory(root,job["id"])/"audio-first.json",{})
    left = settings.get("coverage_start",0)
    right = left+settings.get("coverage_minutes",settings.get("remote_minutes",60))*60
    if length and settings.get("acquisition")!="audio-first":
        return 0,length
    return status.get("trial") or (left,min(right,length) if length else right)


def next_part(root, source, jobs, max_minutes):
    full = replay_cache.enabled() and source["link"].startswith("https://www.youtube.com/")
    if not source["link"].startswith("https://www.youtube.com/") or source["location"] and not full:
        return None if jobs else 0
    spans = [span(root,j,source["duration"]) for j in jobs if j["revision"]==source["revision"]]
    if not source["duration"]:
        return None if spans else 0
    hours = replay_cache.hours if full else streamer_transfer.replay_hours
    if full and replay_cache.state(root,source["id"],source["revision"]).get("state")=="expired":
        return None
    return next((start for start,end in hours(source["duration"])
                 if not any(left<min(start+min(60,max_minutes)*60,end) and right>start
                            for left,right in spans)),None)


def save(db, fields, sid=None):
    tracking = fields.get("title","").strip()
    if len(tracking)>120:
        raise ValueError("Enter a tracking title of up to 120 characters.")
    original = db.execute("SELECT * FROM sources WHERE id=? AND state!='archived'",(sid,)).fetchone() if sid else None
    if sid and not original:
        raise ValueError("Source is unavailable.")
    supplied = fields.get("link","").strip()
    if original and supplied==original["link"]:
        db.execute("UPDATE sources SET tracking_title=? WHERE id=?",(tracking,sid))
        return sid,"Source updated."
    _, local_path, route = identify(supplied)
    canonical, platform = (local_path,"file") if route=="file" else source_queue.link(supplied)
    if platform=="file" and not Path(canonical).is_file():
        raise ValueError("Enter the absolute path to an existing recording on the Factory machine.")
    existing = db.execute("SELECT id,state FROM sources WHERE identity=?",(identify(canonical)[0],)).fetchone()
    if existing and existing["id"]!=sid and (sid or existing["state"]!="archived"):
        return existing["id"],"This link is already in Sources."
    if sid and (not existing or existing["id"]!=sid) and any(j["state"] in ACTIVE for j in attempts(db,sid)):
        raise ValueError("Stop the active job before replacing its source link.")
    if existing:
        source = existing["id"]
        if existing["state"]=="archived":
            store.set_source_state(db,source,"queued")
    else:
        if platform=="file":
            account = db.execute("SELECT creator FROM streamer_accounts WHERE platform='file' "
                                 "AND platform_id='local-recordings'").fetchone()
            if account:
                creator = account["creator"]
            else:
                creator = db.execute("INSERT INTO streamer_creators(name,game,credit) "
                                     "VALUES('Local recordings','Gaming','Local recordings')").lastrowid
                db.execute("INSERT INTO streamer_accounts(creator,platform,platform_id,url) "
                           "VALUES(?,'file','local-recordings','')",(creator,))
            source = streamer_catalog.attach(db,creator,{"link":canonical,"location":canonical,"title":Path(canonical).name})
        else:
            source,_ = store.save_source(db,{"link":canonical})
        db.execute("""INSERT INTO source_queue(source,previous,platform,provenance,readiness)
                      VALUES(?,?,?,?,?)""",
                   (source,sid,platform,"Replaced stream link" if sid else "Pasted stream link",
                    {"youtube":"unresolved","file":"ready"}.get(platform,"unsupported")))
        if sid:
            store.set_source_state(db,sid,"archived")
    db.execute("UPDATE sources SET tracking_title=? WHERE id=?",(tracking,source))
    return source,"Source updated." if sid else "Source saved."


def source_link(row):
    link = row["link"]
    return (f'<a class="directory-url" href="{h(link)}" target="_blank" rel="noreferrer">{h(link)}</a>'
            if link.startswith(("https://","http://")) else f'<span class="directory-url">{h(link)}</span>')


def state_label(job):
    if job["stop"] and job["state"]=="running":
        return "Stopping"
    if job["state"]=="queued" and job["awaiting"]:
        return "Waiting for DeepSeek"
    if job["state"]=="queued" and job["awaiting_flashdata"]:
        return "Waiting for FlashData"
    return STATES.get(job["state"],job["state"])


def badge(label, state=""):
    return f'<span class="operator-state" data-state="{h(state)}">{h(label)}</span>'


def start_form(source, token, start, config, label):
    sid = source["id"]
    base = f"/sources/link/{sid}/start"
    field = f'<input type="hidden" name="start" value="{start}">'
    if config["connections"]["hosted"]:
        return form(base,token,field+button(label,"operator-primary"))
    consent = ('<label class="toggle"><input name="hosted" type="checkbox" required>'
               ' Allow Sail to use transcript text and selected JPEG frames</label>')
    return (f'<details class="operator-consent"><summary>{h(label)}</summary>'
            +form(base,token,field+consent+button("Queue processing","operator-primary"))+'</details>')


def replay_controls(root, row, token, page):
    route = identify(row["location"] or row["link"])[2]
    if row["state"]=="archived" or (route!="file" and not (replay_cache.enabled() and route=="youtube")):
        return ""
    source = row["id"]
    item = replay_cache.state(root,source,row["revision"])
    deadline = replay_cache.expires(item) if item.get("downloaded_at") else None
    base = f"/{page}/source/{source}/replay"
    expired = item.get("state") in ("expiring","expired")
    paused = row["state"]=="paused"
    toggle = "" if expired else form(f"{base}/{'resume' if paused else 'pause'}",token,
                                      button("Resume stream" if paused else "Pause stream"))
    extension = form(f"{base}/extend",token,
                     '<button name="hours" value="12">Add 12 hours</button>'
                     '<button name="hours" value="24">Add 24 hours</button>')
    initial = ("Recording deleted" if item.get("state")=="expired" else "Waiting for the recording")
    if deadline and not expired:
        minutes = max(0,math.ceil((deadline-time.time())/60))
        initial = f"{minutes//60}h {minutes%60:02d}m until deletion" if minutes else "Deletion due"
    return (f'<div class="operator-replay-controls" data-replay-controls="{source}">'
            f'<span class="operator-replay-countdown" data-replay-countdown>{h(initial)}</span>'
            f'<div class="directory-actions"><div data-replay-toggle>{toggle}</div><div data-replay-extension'
            f'{" hidden" if item.get("state")!="verified" else ""}>{extension}</div></div></div>')


def automation_panel(db, root, config):
    checks = readiness.automation(db,root,config)
    issues = sum(bool(issue) for _,issue,_,_ in checks)
    state = ("1 check needs attention" if issues==1 else
             f"{issues} checks need attention" if issues else "Ready for automatic operation")
    rows = []
    for label,issue,detail,url in checks:
        mark = "!" if issue else "✓"
        rows.append(f'<li data-status="{"attention" if issue else "ready"}">'
                    f'<span aria-hidden="true" class="automation-mark">{mark}</span>'
                    f'<div><a href="{url}">{h(label)}</a><span>{h(issue or detail)}</span></div></li>')
    return (f'<section class="operator-automation" aria-labelledby="automation-title">'
            f'<header><h2 id="automation-title">Automation readiness</h2>'
            f'{badge(state,"failed" if issues else "complete")}</header><ul>{"".join(rows)}</ul></section>')


def publication_panel(db, values):
    from . import daily_release

    now = time.time()
    counts = daily_release.publication_counts(db,values,now)
    month = daily_release.local_clock(values,now).strftime("%B %Y")
    return (f'<section class="operator-publication" aria-labelledby="publication-title">'
            f'<h2 id="publication-title">Publishing totals <span>{h(month)}</span></h2>'
            '<dl><div><dt>Individual reels posted</dt>'
            f'<dd><strong>{counts["month_reels"]:,}</strong><small>{counts["today_reels"]:,} today</small></dd></div>'
            '<div><dt>Posts across all accounts</dt>'
            f'<dd><strong>{counts["month_posts"]:,}</strong><small>{counts["today_posts"]:,} today</small></dd></div>'
            '</dl></section>')


def sources(db, root, token, query, posted):
    config,_ = portal_settings.current(db)
    zone = factory_portal.tz(config["resources"]["zone"])
    mode = query.get("edit","")
    editor = ""
    rows = []
    profiles = {row["video"]:row for row in db.execute("SELECT * FROM flashdata_profiles")}
    policy = flashdata_profiles.settings(db)
    for row in listing(db):
        if row["state"]=="archived":
            continue
        sid = row["id"]
        if mode==str(sid):
            editor = source_editor(token,row,posted)
        jobs = attempts(db,sid)
        current = [j for j in jobs if j["revision"]==row["revision"]]
        active = next((j for j in current if j["state"] in ACTIVE),None)
        last = next((j for j in current if j["state"]=="complete"),None)
        latest = active or next(iter(current),None)
        start = next_part(root,row,current,config["resources"]["remote_minutes"])
        available = row["state"]=="queued" and row["enabled"]!=0 and row["readiness"] in ("ready","unresolved",None)
        platform = source_queue.ADAPTERS.get(row["platform"],"Recording")
        label = state_label(latest) if latest else {
            "unsupported":"Saved · adapter needed", "waiting":"Waiting for replay"}.get(row["readiness"],"Saved")
        if latest and latest["state"]=="complete" and start is not None:
            label = "Partially processed"
        if row["state"]=="paused":
            label = "Stream paused"
        actions = f'<a class="operator-action" href="/sources?edit={sid}#link-editor">Edit</a>'
        if jobs:
            actions += f'<a class="operator-action" href="/jobs#job-source-{sid}">View jobs</a>'
        blocking = latest and latest["state"] in ("failed","stopped")
        if not active and available and not blocking and start is not None:
            actions += start_form(row,token,start,config,"Next hour" if last else "Start")
        if not active:
            actions += (f'<details class="operator-remove"><summary>Delete</summary><p>Remove this link from Sources?</p>'
                        +form(f"/sources/link/{sid}/delete",token,button("Delete link","operator-danger"))+'</details>')
        date = factory_portal.when(last["finished"] or last["created"],zone) if last else "—"
        video = row["identity"].removeprefix("youtube:") if row["identity"].startswith("youtube:") else ""
        profile = source_formats(profiles.get(video),policy,token,sid) if video else ""
        rows.append(f'<tr id="source-{sid}"><th scope="row"><strong>{h(title(row))}</strong>'
                    f'{source_link(row)}{profile}</th><td>{h(platform)}</td><td>{badge(label,latest["state"] if latest else "")}</td>'
                    f'<td class="operator-number">{row["reels"]}</td><td class="operator-time">{h(date)}</td>'
                    f'<td><div class="directory-actions">{actions}</div>{replay_controls(root,row,token,"sources")}</td></tr>')
    if query.get("add"):
        editor = source_editor(token,None,posted)
    empty = '<tr><td colspan="6" class="operator-empty">Add a stream link to start your directory.</td></tr>'
    return ('<div class="operator-workspace"><header class="operator-heading"><div><h1>Sources</h1>'
            f'<p>{views.count_label(len(rows),"source")}</p></div><a class="operator-primary" href="/sources?add=1#link-editor">'
            '<span aria-hidden="true">+</span> Add source</a></header>'
            +publication_panel(db,config["uploads"])+automation_panel(db,root,config)+editor+
            '<div class="directory-table" tabindex="0" role="region" aria-label="Saved stream links"><table>'
            '<thead><tr><th scope="col">Stream link</th><th scope="col">Platform</th><th scope="col">Status</th>'
            '<th scope="col" class="operator-number">Reels</th><th scope="col">Last processed</th><th scope="col">Actions</th>'
            '</tr></thead><tbody>'+("".join(rows) or empty)+'</tbody></table></div></div>')


def source_formats(saved, policy, token, source):
    profile = json.loads(saved["profile"]) if saved and saved["profile"] else None
    refresh = form(f"/sources/link/{source}/refresh",token,button("Refresh metadata","operator-action"))
    if not profile:
        return '<details><summary>Metadata pending</summary>'+refresh+'</details>'
    video = flashdata_profiles.rank(profile,"hd",policy)
    audio = flashdata_profiles.rank(profile,"audio",policy)
    highest = max((flashdata_profiles.height(item) for item in profile["formats"]),default=0)
    preferred = f'{flashdata_profiles.height(video[0])}p {flashdata_profiles.codec(video[0]["vcodec"]).upper()}' if video else "No suitable final format"
    age = time.time()-saved["fetched"]
    ttl = 120 if profile["live_status"] == "is_live" else policy["cache_hours"]*3600
    freshness = "Metadata stale" if saved["error"] or age>=ttl else "Metadata saved"
    checked = datetime.fromtimestamp(saved["fetched"],timezone.utc).strftime("%b %d %H:%M UTC")
    details = "".join(f'<li>{h(item["format_id"])} · {flashdata_profiles.height(item)}p '
                      f'{h(flashdata_profiles.codec(item.get("vcodec")))}'
                      + (f' · {h(item["fps"])} fps' if item.get("fps") else "") + '</li>'
                      for item in sorted((item for item in profile["formats"] if flashdata_profiles.height(item)),
                                         key=flashdata_profiles.height,reverse=True))
    thumb = profile["thumbnails"][0]["url"] if profile["thumbnails"] else None
    image = f'<img width="120" loading="lazy" referrerpolicy="no-referrer" alt="" src="{h(thumb)}">' if thumb else ""
    return (f'<details><summary>{freshness} · {clock(profile["duration"])} · {h(preferred)}</summary>'
            f'{image}<p>{h(profile["title"])} · {h(profile.get("channel") or "YouTube")}</p>'
            f'<p>Highest available {f"{highest}p" if highest else "unknown"} · '
            f'Audio {"available" if audio else "unavailable"} · Updated {h(checked)}</p>'
            f'<ul>{details}</ul>'+refresh+'</details>')


def source_editor(token, row, posted):
    sid = row["id"] if row else None
    values = posted if posted else {"link":row["link"],"title":row["tracking_title"]} if row else {}
    action = f"/sources/link/{sid}/edit" if sid else "/sources/link/add"
    return (f'<section class="directory-editor" id="link-editor"><h2>{"Edit source" if sid else "Add source"}</h2>'
            +form(action,token,
                  f'<label>Stream link or local recording<input name="link" required value="{h(values.get("link",""))}"'
                  ' placeholder="YouTube URL or absolute recording path"></label>'
                  f'<label>Tracking title <span>(optional)</span><input name="title" maxlength="120" '
                  f'value="{h(values.get("title",""))}"></label>'
                  '<div class="directory-actions"><a class="operator-action" href="/sources">Cancel</a>'
                  +button("Save source","operator-primary")+'</div>')+'</section>')


def safe_artifact(root, name, directory):
    path = (root/name).resolve()
    return path if path.is_relative_to((root/directory).resolve()) and path.is_file() else None


def range_artifact(root, job, name):
    path = (root/name).resolve()
    allowed = path.is_relative_to((root/"streamer-intake").resolve()) or replay_cache.is_local_range(root,job,path)
    return path if allowed and path.is_file() else None


def artifacts(root, job, asset=None):
    folder = streamer_run.directory(root,job)
    audio = streamer_intake.read(folder/"audio-first.json",{}).get("audio",[])
    selection = streamer_intake.read(folder/"selection.json",{})
    transcript = selection.get("transcript")
    if not transcript and asset:
        transcript = f"streamer-transcripts/{asset}/audio-first-run-{job}/transcript.json"
    return audio, safe_artifact(root,transcript,"streamer-transcripts") if transcript else None


def job_output(db, root, job):
    items = []
    candidates = set()
    for row in db.execute("""SELECT e.*,h.reason AS held FROM streamer_editions e
                             LEFT JOIN done_release_holds h ON h.edition=e.id
                             WHERE e.job=? AND e.render_state='exported' ORDER BY e.rowid DESC""",(job,)):
        candidate = row["candidate"]
        path = streamer_run.directory(root,job)/candidate/"editions"/row["id"]/"portrait.mp4"
        if candidate in candidates or not path.is_file():
            continue
        candidates.add(candidate)
        plan = json.loads(row["plan"])
        url = f'/streamer/run/{job}/{candidate}/edition/{row["id"]}'
        label = ("Editorial hold" if row["held"] else "Ready to publish"
                 if row["qa"] and row["qualification"]=="qualified" else "Ready to view")
        cuts = plan["cuts"]
        duration = plan.get("duration") or sum(cut["end"]-cut["start"] for cut in cuts)
        items.append((cuts[0]["start"],plan["title"],duration,url,label,candidate))
    return items


def output_panel(items, job, part):
    cards = []
    for _,name,duration,url,label,_ in items:
        cards.append(f'<article class="operator-reel"><video preload="none" controls playsinline '
                     f'data-job-media="{url}/portrait.mp4" poster="{url}/poster.jpg" aria-label="{h(name)}"></video>'
                     f'<div><strong>{h(name)}</strong><span>{duration:.1f} s · {h(label)}</span>'
                     f'<a href="{url}/portrait.mp4" target="_blank">Open video</a></div></article>')
    return (f'<details class="operator-outputs" id="outputs-{job}-{part}"><summary>Outputs'
            f'<span>{views.count_label(len(cards),"reel")}</span></summary><div class="operator-output-list">'
            +("".join(cards) or '<p>No reels saved for this part.</p>')+'</div></details>')


def story_progress(root, job, part, items):
    folder = streamer_run.directory(root,job)
    selection = streamer_intake.read(folder/"selection.json",{})
    if "shortlist" not in selection:
        return ""
    shortlisted = set(selection["shortlist"])
    rough = streamer_intake.read(folder/"audio-selection.json",{})
    starts = {edit["id"]:edit["shots"][0][0] for edit in rough.get("edits",[]) if edit.get("shots")}
    starts.update({story["id"]:story["start"] for story in selection["proposals"] if "start" in story})
    planned = {story for story in shortlisted if story in starts and part<=starts[story]<part+3600}
    rendered = planned & {item[-1] for item in items}
    outcomes = streamer_intake.read(folder/"hybrid-results.json",{})
    blocked = sum(outcomes.get(story,{}).get("outcome",{}).get("state") in
                  {"held","technical","budget","skipped"} for story in planned-rendered)
    label = f"{len(rendered)} of {len(planned)} selected reels rendered" if planned else "No stories selected"
    if blocked:
        label += f" · {blocked} unfinished"
    bar = (f'<progress aria-label="Reels rendered" value="{len(rendered)}" max="{len(planned)}"></progress>'
           if planned else "")
    return f'<div class="operator-story-progress" id="story-progress-{job}-{part}">{bar}<span>{label}</span></div>'


def phase_panel(db, root, job, part, items):
    number = job["id"]
    stage = job["stage"]
    state = job["state"]
    phase = next((i for i,(_,stages) in enumerate(PHASES) if stage in stages),0)
    finished = {r[0] for r in db.execute(
        "SELECT DISTINCT stage FROM factory_events WHERE job=? AND kind='finished'",(number,))}
    audio,transcript = artifacts(root,number,job["asset"])
    audio_players = []
    for i,item in enumerate(audio):
        if item["start"]<part+3600 and item["end"]>part and range_artifact(root,number,item["path"]):
            url = f"/jobs/evidence/{number}/audio/{i}"
            audio_players.append(f'<audio controls preload="none" data-job-media="{url}" '
                                 'aria-label="Saved source audio"></audio>')
    replies = db.execute("SELECT 1 FROM provider_calls WHERE job=? AND provider='sail' AND result IS NOT NULL",
                         (number,)).fetchone()
    full = json.loads(job["settings"]).get("full_replay")
    evidence = {
        "Audio": ("Audio", "".join(audio_players)),
        "Transcribe": ("Transcript",f'<a href="/jobs/evidence/{number}/transcript/{part}" target="_blank">'
            'Open word-timed transcript</a>' if transcript else ""),
        "Judge": ("DeepSeek response",f'<a href="/jobs/evidence/{number}/deepseek" target="_blank">'
            'Open saved DeepSeek responses</a>' if replies else ""),
    }
    lines = []
    for i,(label,stages) in enumerate(PHASES):
        name, content = evidence.get(stages[0],("",""))
        if full and stages[0]=="Audio":
            label = "Extract audio from cached replay"
        status = ("current" if state!="complete" and i==phase else
                  "done" if content or any(s in finished for s in stages) else "pending")
        mark = "✓" if status=="done" else ""
        artifact = ""
        if content:
            artifact = f'<details id="evidence-{number}-{part}-{i}"><summary>{name}</summary>{content}</details>'
        judge = stages[0]=="Judge"
        if judge:
            artifact += story_progress(root,number,part,items)
        anchor = f' id="stage-judge-{number}-{part}"' if judge else ""
        lines.append(f'<li data-phase="{status}"{" aria-current=step" if status=="current" else ""}>'
                     f'<span class="phase-mark" aria-hidden="true">{mark}</span>'
                     f'<div{anchor}><span>{label}</span>{artifact}</div></li>')
    info = job["error"] if state=="failed" else (job["progress"] if state in ("queued","stopped") else "")
    label = state_label(job)
    next_label = ("Extract audio from cached replay" if full and PHASES[phase+1][1][0]=="Audio" else PHASES[phase+1][0]
                  ) if phase+1<len(PHASES) else ""
    upcoming = (f'<p class="operator-next">Next <strong>{next_label}</strong></p>'
                if state in ACTIVE and phase+1<len(PHASES) else "")
    logs = (f'<details class="operator-live-log" id="live-log-{number}-{part}" data-job-log="{number}">'
            '<summary>View live logs</summary><pre tabindex="0" aria-label="Worker log in UTC">Loading…</pre></details>')
    return (f'<section class="operator-phases" aria-label="Processing stages"><h4>Processing stages</h4>{logs}'
            f'{badge(label,state)}{upcoming}'+(f'<p class="operator-log-note">{h(info)}</p>' if info else "")
            +f'<ol>{"".join(lines)}</ol>'+download_diagnostics(root,job,finished)+'</section>')


def download_diagnostics(root, job, finished):
    folder = root/"streamer-intake"/"flashdata"/f'run-{job["id"]}'
    labels = {"audio":"Audio", "preview":"Storyboard preview", "hd":"HD interval", "hd720":"HD interval (720p fallback)"}
    delivery = {"stalled":"Download blocked · wait limit reached", "failed":"Download blocked",
                "status unavailable":"Provider status unavailable", "result unavailable":"Delivery record unavailable"}
    waiting = ("Saved provider request · run paused" if job["state"]=="stopped" else
               "Awaiting FlashData delivery")
    counts, attempts = {}, []
    for file in folder.glob("*/request.json"):
        try:
            receipt = streamer_intake.read(file,{})
        except (OSError,ValueError):
            continue
        purpose = receipt.get("purpose")
        if purpose in labels:
            status = ("Download verified" if receipt.get("checksum") else
                      delivery.get(receipt.get("delivery_status"),waiting))
            counts[purpose,status] = counts.get((purpose,status),0)+1
            if item := receipt.get("format"):
                left,right = receipt["range"]
                duration = max(0,int(time.time()-receipt["submitted_at"]))
                duration = min(duration,int(receipt.get("verified_after",duration)))
                probe, report = receipt.get("probe",{}),receipt.get("result",{})
                actual = (f' · {probe["height"]}p {probe["codec"]} {probe["fps"]:.1f} fps'
                          if probe.get("height") else "")
                size = f' · {report["file_size"]/1024**2:.1f} MiB' if report.get("file_size") else ""
                credits = receipt.get("credits",report.get("credits"))
                price = f' · {credits:.3f} credits' if credits is not None else ""
                selection = (f'{flashdata_profiles.height(item)}p {flashdata_profiles.codec(item.get("vcodec"))}'
                             if purpose!="audio" else flashdata_profiles.codec(item.get("acodec")))
                attempts.append(f'<li>{h(labels[purpose])} · {clock(left)}–{clock(right)} · '
                                f'{h(selection)} · format {h(item["format_id"])} · {h(receipt.get("remote_status","pending"))} · '
                                f'{duration}s{h(actual+size+price)}</li>')
    detail = " ".join((job["error"],job["progress"])).casefold()
    authentication = any(signal in detail for signal in ("sign-in","sign in","requires verification"))
    if not counts and not authentication:
        return ""
    items = ["<li>Replay metadata available</li>"] if "Resolve" in finished else []
    if authentication:
        items.append("<li>YouTube authentication required for the downloader</li>")
    for (purpose,status),count in counts.items():
        suffix = f" ({count})" if count>1 else ""
        items.append(f"<li>{labels[purpose]} · {status}{suffix}</li>")
    return '<details><summary>Download diagnostics</summary><ul>'+''.join(items+attempts)+'</ul></details>'


def job_control(job, token, part, source):
    state = job["state"]
    action,label = {"queued":("pause","Stop"),"running":("pause","Stop"),
                    "stopped":("resume","Resume"),"failed":("resume","Retry")}.get(state,("",""))
    if not action or job["stop"] or source["state"]!="queued" or job["revision"]!=source["revision"]:
        return ""
    return form(f'/jobs/{job["id"]}/{action}',token,
                f'<input type="hidden" name="part" value="{part}">'+button(label,"operator-action"))


def part_panel(db, root, source, token, start, end, matching, open_by_default, output, available, config, *, historical=False):
    sid = source["id"]
    matching.sort(key=lambda j: (j["state"] not in ACTIVE, -j["id"]))
    key = f"job-part-{sid}-{start}"
    clips = 0
    bodies = []
    label = state_label(matching[0]) if matching else "Not started"
    for index,job in enumerate(matching):
        number = job["id"]
        items = [item for item in output[number] if start<=item[0]<end]
        clips += len(items)
        left,right = span(root,job,source["duration"])
        range_label = (f'<p>{clock(left)}–{clock(right)}</p>' if left!=start or right!=end else "")
        workflow = json.loads(job["settings"]).get("hybrid_prompt")
        method = ('<p>DeepSeek V4</p>' if workflow==streamer_story_v4.VERSION else
                  '<p>Legacy selection</p>' if workflow else '')
        past_cutoff = (source["platform"]=="youtube" and not source["location"]
                       and right>streamer_transfer.REPLAY_LIMIT and not json.loads(job["settings"]).get("full_replay"))
        controls = job_control(job,token,start,source) if not past_cutoff or job["state"] in ACTIVE else ""
        body = (f'<div class="operator-part-card"><div class="operator-part-intro"><p>Run {number}</p>{method}{range_label}'
                +controls+'</div>'
                +phase_panel(db,root,job,start,items)+output_panel(items,number,start)+'</div>')
        bodies.append(body if index==0 else
                      f'<details class="operator-older-run" id="part-{sid}-{start}-run-{number}">'
                      f'<summary>Earlier run {number} · {h(state_label(job))}</summary>{body}</details>')
    if not bodies:
        action = start_form(source,token,start,config,"Start this hour") if available else ""
        bodies.append('<div class="operator-part-pending">'+action+'</div>' if action else
                      '<p class="operator-part-pending">This part has no run yet.</p>')
    heading = "Earlier coverage" if historical else f"Part {start//3600+1}"
    return (f'<details class="operator-hour" id="{key}"{" open" if open_by_default else ""}>'
            f'<summary><strong>{heading}</strong><time>{clock(start)}–{clock(end)}</time>'
            f'{badge(label,matching[0]["state"] if matching else "")}'
            f'<span class="operator-hour-count">{views.count_label(clips,"reel")}</span></summary>'
            +''.join(bodies)+'</details>')


def clock(seconds):
    hours,rest = divmod(math.ceil(seconds),3600)
    minutes,seconds = divmod(rest,60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def job_source(db, root, token, row, jobs, config, query):
    sid = row["id"]
    current = [j for j in jobs if j["revision"]==row["revision"]]
    active = next((j for j in current if j["state"] in ACTIVE),None)
    length = row["duration"]
    ranges = {j["id"]:span(root,j,length) for j in jobs}
    end = max([length,*[right for left,right in ranges.values()]])
    selected = (int(ranges[active["id"]][0])//3600*3600 if active else
                int(min(left for left,right in ranges.values()))//3600*3600)
    if query.get("source")==str(sid) and query.get("part","").isdigit():
        selected = int(query["part"])
    output = {j["id"]:job_output(db,root,j["id"]) for j in jobs}
    full = any(json.loads(job["settings"]).get("full_replay") for job in current)
    youtube = row["platform"]=="youtube" and not row["location"] and not full
    availability = youtube and not active and row["state"]=="queued" and row["enabled"]!=0 and row["readiness"]=="ready"
    windows = streamer_transfer.replay_hours(end) if youtube else [
        (start,min(end,start+3600)) for start in range(0,math.ceil(end),3600)]
    parts = []
    for start,stop in windows:
        matching = [j for j in jobs if (left:=ranges[j["id"]][0])<stop and ranges[j["id"]][1]>start]
        parts.append(part_panel(db,root,row,token,start,stop,matching,start==selected,output,
                                availability and length>start,config))
    limit = streamer_transfer.REPLAY_LIMIT
    if youtube and (earlier := [j for j in jobs if ranges[j["id"]][1]>limit]):
        parts.append(part_panel(db,root,row,token,limit,end,earlier,selected>=limit,output,False,config,historical=True))
    if full:
        parts.insert(0, (f'<section class="operator-replay-progress" data-replay-progress="{sid}">'
                         '<label>Full replay download <progress data-download-progress></progress>'
                         '<span data-download-count></span></label>'
                         '<label>Local transcription <progress data-transcript-progress></progress>'
                         '<span data-transcript-count></span></label>'
                         +replay_controls(root,row,token,"jobs")+'</section>'))
    preview = next((j["id"] for j in jobs if streamer_intake.read(
        streamer_run.directory(root,j["id"])/"audio-first.json",{}).get("ranges")),None)
    thumb_url = (f"/streamer/run/{preview}/poster.jpg" if preview else
                 f'https://i.ytimg.com/vi/{row["platform_id"]}/hqdefault.jpg'
                 if row["platform_id"] and row["platform"]=="youtube" else "")
    thumbnail = f'<img src="{h(thumb_url)}" loading="lazy" alt="" referrerpolicy="no-referrer">' if thumb_url else ""
    return (f'<article class="operator-job" id="job-source-{sid}"><header><div class="operator-thumbnail">'
            f'{thumbnail}</div><div><h3>{h(title(row))}</h3>{source_link(row)}'
            f'<p>Source {sid} · {views.count_label(len(windows),"part")} · {views.count_label(row["reels"],"reel")}</p>'
            '</div></header><div class="operator-hours">'+''.join(parts)+'</div></article>')


def jobs_page(db, root, token, query):
    config,_ = portal_settings.current(db)
    active,history = [],[]
    for row in listing(db):
        jobs = attempts(db,row["id"])
        if jobs:
            target = active if any(j["state"] in ACTIVE for j in jobs) else history
            target.append(job_source(db,root,token,row,jobs,config,query))
    return ('<div class="operator-workspace"><header class="operator-heading"><div><h1>Jobs</h1>'
            f'<p data-job-count>{len(active)} active · {len(history)} in history</p></div></header>'
            f'<div data-operator-jobs data-busy="{int(bool(active))}"><section class="operator-job-group">'
            '<h2>Active</h2>'+ (''.join(active) or '<p class="operator-empty">No active jobs. Start a link from <a href="/sources">Sources</a>.</p>')
            +'</section><section class="operator-job-group"><h2>History</h2>'
            +(''.join(history) or '<p class="operator-empty">No job history yet.</p>')+'</section></div></div>')
