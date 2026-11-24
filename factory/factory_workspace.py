"""Creator-grouped operating view. Detailed event history lives on the run page."""

import json
import math
from collections import defaultdict

from . import factory_done, factory_metrics, factory_portal, shorts_release, streamer_asr, streamer_edits, streamer_layout, streamer_run, views
from .provider_meter import DISCOVERY, DOWNSTREAM, report

h, form, button, timecode = views.h, views.form, views.button, views.timecode
STEPS = (("Source",("Acquire","Archive","Resolve","Audio")),("Transcribe",("Transcribe","Audio continuity")),
         ("Select",("Judge","Revisit","Visual map")),("Edit",("Selected video","Audition","Render")),
         ("Check",("Visual check","QC","Review")))
ICONS = {"Finished":"✓","Active":"▶","Waiting":"○","Skipped":"–","Blocked":"!","Needs review":"○"}


def load(path):
    return streamer_asr.read(path) if path.is_file() else {}


def short_time(seconds):
    hours, remainder = divmod(int(seconds),3600)
    return (f"{hours}h " if hours else "") + f"{remainder//60}m"


def reel_count(n):
    return views.count_label(n,"reel")


def player(url, poster, name, extra=""):
    return (f'<video controls playsinline preload="none" data-workspace-poster="{poster}" src="{url}" '
            f'aria-label="{h(name)}"{extra}></video>')


def available_reels(db, root, folder, job, selection):
    reels = defaultdict(list)
    prefix = f"/streamer/run/{job}"
    for edition in db.execute("""SELECT e.*,EXISTS(SELECT 1 FROM done_editions d WHERE d.edition=e.id) AS finished,
                                   (SELECT verdict FROM streamer_edition_feedback
                                   WHERE edition=e.id ORDER BY id DESC LIMIT 1) AS verdict
                                   FROM streamer_editions e WHERE job=? AND render_state='exported'
                                   ORDER BY rowid DESC""",(job,)):
        candidate, identity = edition["candidate"],edition["id"]
        if not (folder/candidate/"editions"/identity/"portrait.mp4").is_file():
            continue
        plan = json.loads(edition["plan"])
        url = f"{prefix}/{candidate}/edition/{identity}"
        release_note, qualified = "", False
        if not reels[candidate]:
            release = shorts_release.inspect(shorts_release.portrait_path(root,edition))
            restriction = shorts_release.restriction(db,identity)
            release_note = restriction or (shorts_release.describe(release) if not release["passed"] else "")
            qualified = edition["qualification"]=="qualified" and release["passed"] and not restriction
        automatic = plan.get("origin")=="automatic"
        name = plan["hook"] if automatic and plan.get("hook") else plan["title"]
        reels[candidate].append(dict(url=url,name=name,version_name=plan["title"],identity=identity,verdict=edition["verdict"],
            reviewed=bool(edition["verdict"] or edition["qa"]),qualified=qualified,machine=qualified and automatic,
            ready=bool(edition["qa"] and qualified and edition["verdict"] in (None,"accepted")),
            release_note=release_note,
            standard=plan["recipe"]==streamer_layout.RECIPE,start=plan["cuts"][0]["start"],planned=plan.get("planned_style"),
            ranged=bool(plan.get("source_windows")),
            rendered=plan.get("render_style") if edition["finished"] else None))
    for candidate in selection.get("proposals",[]):
        cid = candidate["id"]
        if (folder/cid/"portrait.mp4").is_file():
            verdict = load(folder/cid/"review.json").get("decision")
            reels[cid].append(dict(url=f"{prefix}/{cid}",identity="original",start=candidate["start"],verdict=verdict,
                name=candidate.get("hook") or candidate.get("event") or "Original portrait",
                reviewed=bool(verdict),ready=False,standard=False,qualified=False,machine=False))
    return reels


def review_form(job, cid, item, token):
    path = f'/factory/run/{job}/reel/{cid}/{item["identity"]}'
    choices = '<option value="accepted">Accept</option><option value="rejected">Reject</option>'
    checks = qa = ""
    if item["identity"]!="original":
        choices += '<option value="rework">Request rework</option>'
        checks = "".join(f'<label><input type="checkbox" name="{key}"> {h(label)}</label>'
                         for key,label in streamer_edits.REVIEW_CHECKS)
        if item["qualified"] and not item["ready"] and item["verdict"] in (None,"accepted"):
            qa = form(path+"/qa",token,'<label>Picture, audio and caption observations'
                      '<textarea name="reason" required maxlength="2000"></textarea></label>'+button("Confirm final QA"))
    fields = (f'<label>Decision<select name="verdict">{choices}</select></label>'
              '<label>What you saw and heard<textarea name="reason" required maxlength="2000"></textarea></label>')
    return '<details><summary>Record review or QA</summary>'+form(path+"/feedback",token,fields+checks+button("Save review"))+qa+'</details>'


def hourly(job, selection, reels, target):
    if not selection:
        return ""
    duration = selection["asset"]["duration"]
    intervals = [(c["start"],c["end"]) for c in selection.get("chapters",[]) if c.get("judgment")!="blocked"]
    candidates = {c["id"]:c for c in selection["proposals"]}
    buckets = defaultdict(list)
    for cid, versions in reels.items():
        timestamp = factory_metrics.payoff(candidates[cid]) if cid in candidates else versions[0]["start"]
        buckets[factory_metrics.hour_index(timestamp,duration)].append(versions)
    rows = []
    for index in range(math.ceil(duration/3600)):
        start, end = index*3600, min(duration,(index+1)*3600)
        covered = factory_metrics.coverage(intervals,start,end)
        versions = buckets[index]
        exported = sum(len(items) for items in versions)
        qualified = sum(items[0]["qualified"] for items in versions)
        title = f'Hour {index+1}{" · partial" if end-start<3600 else ""}'
        rows.append(f'<tr><th scope="row">{title}</th><td>{covered/60:.1f} / {(end-start)/60:.1f}</td>'
                    f'<td>{exported}</td><td>{qualified}</td></tr>')
    target_text = f'<p>Target of {target} qualified moments per full source hour.</p>' if target else ""
    return (f'<details><summary>Hourly yield</summary>{target_text}'
            '<div class="workspace-hourly"><table><thead><tr><th scope="col">Source hour</th>'
            '<th scope="col">Judged min</th><th scope="col">Exports</th><th scope="col">Qualified</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<p><a href="/factory/run/{job}#clips">Open evidence and full metrics</a></p></details>')


def totals(reels):
    reviewed = sum(items[0]["reviewed"] for items in reels.values())
    ready = sum(items[0]["ready"] for items in reels.values())
    return f"{reel_count(len(reels))} · {reviewed} of {len(reels)} reviewed · {ready} ready"


def gallery(job, selection, reels, token, target, selected=""):
    yield_details = hourly(job,selection,reels,target)
    if not reels:
        proposals = (f'<p>{len(selection["proposals"])} saved proposals. '
                     f'<a href="/factory/run/{job}#clips">Inspect source evidence and hourly yield</a>.</p>'
                     if selection else '<p>Exports will appear here after processing.</p>')
        return proposals+yield_details
    machine = sum(items[0]["machine"] for items in reels.values())
    cards, posters = [], []
    selected = selected if selected in reels else next(iter(reels))
    for index,(cid, items) in enumerate(reels.items()):
        item = items[0]
        title = item["name"].strip()
        if (not title or title.startswith(("…","...")) or title.endswith(("…","..."," and"," then"," the"," to"))):
            title = f'Moment at {timecode(item["start"])}'
        state = ("ready" if item["ready"] else "rework" if item.get("release_note") or item["verdict"] in ("rework","rejected") else
                 "reviewed" if item["reviewed"] else "unreviewed")
        edit = f"/streamer/run/{job}/{cid}/edit"
        if item["identity"]!="original":
            edit += f'?edition={item["identity"]}'
        old = "".join(f'<figure><figcaption>{h(prior.get("version_name",prior["name"]))} · '
                      f'{"35/65" if prior["standard"] else "Earlier layout"}'
                      f'{" · Earlier review saved" if prior["reviewed"] else ""}</figcaption>'
                      +player(prior["url"]+"/portrait.mp4",prior["url"]+"/poster.jpg",prior["name"])+'</figure>'
                      for prior in items[1:])
        anchor = f"reel-{job}-{cid}"
        posters.append(f'<button type="button" data-reel-pick="{anchor}" data-reel-state="{state}" '
                       f'aria-pressed="{str(selected==cid).lower()}"{" hidden" if index>=4 else ""}>'
                       f'<img loading="lazy" src="{item["url"]}/poster.jpg" alt=""><span>{h(title)}</span></button>')
        cards.append(f'<article class="workspace-reel" data-reel-card data-reel-state="{state}" id="{anchor}"'
                     f'{" hidden" if selected!=cid else ""}>'
                     f'<h4>{h(title)}</h4><p>{h(state.title())} · {"35/65" if item["standard"] else "Earlier layout"}</p>'
                     + (f'<p>{h(item["release_note"])}</p>' if item.get("release_note") else "")
                     +player(item["url"]+"/portrait.mp4",item["url"]+"/poster.jpg",title)
                     +'<div class="workspace-actions">'
                     f'<a href="{edit}">Edit</a>'
                     + (f'<a href="#source-ranges-{job}">Source ranges</a>' if item.get("ranged") else
                        f'<a data-source-at="{item["start"]:.3f}" href="/streamer/run/{job}/archive#t={item["start"]:.3f}">'
                        f'Source at {timecode(item["start"])}</a>')
                     + '</div>'+ (f'<p>Rendered style · {h(item["rendered"]["name"])}</p>' if item.get("rendered") else
                                f'<p>Planned style · {h(item["planned"]["name"])}</p>' if item.get("planned") else "")
                     +review_form(job,cid,item,token)+
                     (f'<details><summary>Compare earlier versions ({len(items)-1})</summary>{old}</details>' if old else "")+
                     '</article>')
    more = f'<button type="button" data-reel-more aria-expanded="false">View all {len(reels)}</button>' if len(reels)>4 else ""
    return ('<label>Show reels<select data-reel-filter><option value="">All</option><option value="unreviewed">Unreviewed</option>'
            '<option value="ready">Ready</option><option value="rework">Rework</option></select></label>'
            '<p hidden data-no-reels>No reels match this filter.</p>'
            f'<div class="workspace-posters">{"".join(posters)}</div>{more}'
            '<div class="workspace-actions"><button type="button" data-reel-step="-1">Previous</button>'
            '<button type="button" data-reel-step="1">Next</button></div>'
            f'<div class="workspace-gallery">{"".join(cards)}</div>'
            f'<details><summary>How counts are calculated</summary><p>{machine} machine-qualified moments. '
            'Reviewed counts use the visible edition of each moment.</p>'
            '<p>Earlier editions are included in exports. Their reviews stay attached to those editions.</p></details>'
            +yield_details)


def processing(row, asset, events, selection, duration, covered, partial, budget, framing, edition_work,
               exported, moment_total, ready, zone, token, reserved, cap):
    if not row:
        return '<p>Queue a run to start archiving this stream.</p>'
    job,state = row["id"],row["state"]
    audio_first = json.loads(row["settings"]).get("acquisition")=="audio-first" and (
        not asset or selection.get("asset",{}).get("kind")=="audio-first")
    local_zone = factory_portal.tz(zone)
    stage = next((i for i,(_,labels) in enumerate(STEPS) if row["stage"] in labels),0)
    steps = []
    measured = next((e for e in events if e["stage"] in STEPS[stage][1] and e["covered_seconds"] is not None
                     and e["total_seconds"]),None)
    if measured and state in ("running","queued"):
        total = short_time(measured["total_seconds"])
        progress = f'{short_time(measured["covered_seconds"])} / {total} source time'
    else:
        progress = f"{short_time(covered)} / {short_time(duration)} judged" if selection and duration else ""
    for i,(label, labels) in enumerate(STEPS):
        last = next((e for e in events if e["stage"] in labels and e["kind"] in
                     ("finished","failed","stopped","started","deferred")),None)
        detail = last["item"] if last else ""
        step = "Waiting"
        if state=="running" and stage==i:
            step = "Active"
        elif state in ("failed","stopped") and stage==i:
            step,detail = "Blocked",row["error"] or "Resume this attempt when ready."
        elif i==2 and partial:
            step,detail = "Blocked","The saved analysis reached its reservation limit." if budget else "Some chapters are incomplete."
        elif i==3 and edition_work:
            step = "Active" if edition_work=="rendering" else "Waiting"
            detail = "A saved edition is rendering." if step=="Active" else "An edition is queued."
        elif i>=3 and state=="complete":
            step = ("Blocked" if i==3 and framing else "Needs review" if i==4 and ready<moment_total else
                    "Finished" if exported else "Skipped")
            detail = framing if i==3 and framing else ("Inspect the exported reels." if exported else "No portrait reached this step.")
        elif i==0 and row["asset"] or i in (1,2) and selection or last and last["kind"]=="finished":
            step = "Finished"
        if i==0 and asset and step=="Finished":
            detail = ("Audio and selected video are reported under Original stream." if audio_first else
                      f'Archive available · {timecode(duration)} · {asset["bytes"]/2**20:,.1f} MiB.')
        elif i==1 and selection and step=="Finished" and "transcript_words" in selection:
            detail = f'{selection["transcript_words"]:,} words transcribed locally.'
        elif i==2 and selection:
            detail += f' {len(selection["proposals"])} saved proposals.'
        elif i==3 and exported and step=="Finished":
            detail = f'{reel_count(exported)} available to play.'
        elif i==4 and exported:
            detail = f'{ready}/{moment_total} moments ready after final QA.'
            if step=="Needs review":
                detail += ' Open an output reel to record a review or final QA.'
        if measured and i==stage and state in ("running","queued"):
            coverage = f'<progress max="{measured["total_seconds"]}" value="{measured["covered_seconds"]}" aria-label="{h(progress)}"></progress>'
        else:
            coverage = (f'<progress max="{duration}" value="{covered}" aria-label="{h(progress)}"></progress>'
                        if i==2 and selection and duration and progress else "")
        elapsed = (f'<small>{factory_portal.when(last["utc"],local_zone)} local</small>'
                   if last else "")
        steps.append(f'<details class="workspace-step" data-step="{i}"><summary>'
                     f'<span class="step-symbol" aria-hidden="true">{ICONS[step]}</span>'
                     f'<span>{label}<small>{step}</small></span></summary><p>{h(detail or "No result recorded yet.")}</p>{coverage}{elapsed}</details>')
    action = ""
    if state in ("failed","stopped"):
        action = form(f"/factory/run/{job}/workspace-resume",token,button("Resume saved attempt"))
    elif budget and partial and not audio_first:
        needed = DOWNSTREAM+(DISCOVERY if any(c.get("judgment")=="blocked" for c in selection["chapters"]) else 0)
        action = ('<p>USD {:.4f} reserved of {:.2f}. USD {:.4f} remains. Resume requires USD {:.4f} for the next work.</p>'
                  .format(reserved/1_000_000,cap/1_000_000,max(0,cap-reserved)/1_000_000,needed/1_000_000)
                  +form(f"/factory/run/{job}/continue-analysis",token,button("Check and resume within cap")))
    elif budget:
        action = f'<p>Follow-up reached the reservation limit. <a href="/factory/run/{job}#clips">See hourly yield</a>.</p>'
    elif framing:
        action = '<p>Open the saved edition under History to adjust the camera or choose a neighboring interval.</p>'
    elif state=="running":
        action = form(f"/factory/run/{job}/workspace-pause",token,button("Pause attempt"))
    current = row["progress"] if state in ("queued","running") else row["error"] if state in ("failed","stopped") else progress
    if progress and state in ("queued","running"):
        current = (current+" · " if current else "")+progress
    updated = factory_portal.when(events[0]["utc"] if events else row["created"],local_zone)
    return (f'<p>{h(current)}</p><p class="workspace-time">Updated {h(updated)} · {h(zone)}</p>'
            f'<div class="workspace-steps">{"".join(steps)}</div>{action}')


def audio_source(folder, job, original, state):
    transfers = load(folder/"media-transfers.json")
    totals = {kind:sum(row["received"] for row in transfers if row["kind"]==kind) for kind in ("audio","video")}
    pending = sum(row.get("pending",0)-row["received"] for row in transfers if row.get("pending",0))
    complete = sum(any(a["start"]<=s and a["end"]>=e for a in state["audio"]) for s,e in state["parts"])
    audio = sum(a["actual"] for a in state["audio"])
    video = sum(w["actual"] for w in state["ranges"])
    hd = sum(w["actual"] for w in state["ranges"] if (w.get("height") or 0)>=720)
    disk = sum(a["bytes"]+a.get("asr_audio_bytes",0) for a in state["audio"])
    disk += sum(w["bytes"]+w.get("picture_bytes",0) for w in state["ranges"])
    start,stop = state["trial"]
    ranges = "".join(f'<li><a href="/streamer/run/{job}/selection-range/{index}.mp4">'
                     f'{timecode(item["start"])}–{timecode(item["end"])} · {item["height"]}p</a></li>'
                     for index,item in enumerate(state["ranges"]))
    capabilities = state.get("capabilities",{})
    return (f'<p><a href="{h(original)}">Original platform replay</a></p>'
            f'<p>Audio-first · {complete}/{len(state["parts"])} full audio parts · {audio/60:.1f} min retained · '
            f'{state["analyzed"]/60:.1f} of {state["duration"]/60:.1f} min analyzed</p>'
            f'<p>Requested coverage {timecode(start)}–{timecode(stop)}'
            f'{" · Partial coverage" if not state.get("complete") else ""}</p>'
            f'<p>Audio-only {"available" if capabilities.get("seekable_audio") else "unavailable"} · Bounded video '
            f'{"available" if capabilities.get("bounded_video") else "unavailable"}</p>'
            f'<p>{state.get("selected",0)} selected stories · {len(state.get("planned_windows",[]))} planned windows · '
            f'{video/60:.2f} min video retained · {hd/60:.2f} min HD</p>'
            f'<p>Measured audio transfer {totals["audio"]:,} bytes · video {totals["video"]:,} bytes · '
            f'{disk:,} retained intake bytes on disk'
            f'{f" · {pending:,} bytes reserved by interrupted reads" if pending else ""}</p>'
            f'<details id="source-ranges-{job}"><summary>Acquired source ranges ({len(state["ranges"])})</summary>'
            f'<ul>{ranges or "<li>No selected footage acquired</li>"}</ul></details>'
            + (f'<p><a href="/streamer/run/{job}/audio-selection.json">Saved selection</a></p>'
               if (folder/"audio-selection.json").is_file() else ""))


def candidate_results(db, folder, row, intake, selection, production, spend, zone):
    from . import provider_api, streamer_hybrid

    if not intake and not selection and not json.loads(row["settings"]).get("hybrid_version"):
        return ""
    job = row["id"]
    config = json.loads(row["settings"])
    hybrid = load(folder/"hybrid-results.json")
    historical = load(folder/"clip-repair.json")
    proposed = {p["id"]:p for p in selection.get("proposals",[])}
    shortlist = {e["id"]:e for e in load(folder/"audio-selection.json").get("edits",[])}
    outcomes = {p["id"]:p for p in production.get("items",[])}
    for key,item in hybrid.items():
        if "outcome" in item:
            outcomes[key] = item["outcome"]
    holds = {r["edition"]:r["reason"] for r in db.execute("""SELECT h.edition,h.reason
                 FROM done_release_holds h JOIN streamer_editions e ON e.id=h.edition WHERE e.job=?""",(job,))}
    totals = {key:0 for key in ("reviewed","ready","held","technical","pending","budget")}
    candidates = []
    for key in dict.fromkeys([*shortlist,*outcomes,*proposed]):
        item = outcomes.get(key,{})
        verdict = item.get("state","pending" if key in shortlist else "unselected")
        manual_hold = holds.get(item.get("edition"))
        if manual_hold:
            verdict = "held"
        if verdict in ("held","technical","budget","pending"):
            totals[verdict] += 1
        if verdict=="qualified":
            totals["ready"] += 1
        current = hybrid.get(key,{})
        if verdict in ("qualified","held") or "first" in current or key in historical and "first" in historical[key]:
            totals["reviewed"] += 1
        rough = current.get("rough",shortlist.get(key,{}).get("shots",[]))
        decision = current.get("decision",{})
        intervals = " · ".join(f"{timecode(a)}–{timecode(b)}" for a,b in rough)
        locked = decision.get("interval")
        if locked:
            intervals += f" · Locked {timecode(locked[0])}–{timecode(locked[1])}"
        reason = manual_hold or item.get("reason") or decision.get("witness") or proposed.get(key,{}).get("rejection") or proposed.get(key,{}).get("reason","")
        links = ""
        if "preview_range" in current:
            url = f'/streamer/run/{job}/selection-range/{current["preview_range"]}.mp4'
            offset = current.get("preview_offset",0)
            length = current["requested"][1]-current["requested"][0]
            links += f'<video controls playsinline preload="none" src="{url}#t={offset:.3f},{offset+length:.3f}" aria-label="Continuous preview"></video>'
        elif rough and not config.get("hybrid_version"):
            original = []
            for i,w in enumerate(intake.get("ranges",[])):
                if any(max(a,w["start"])<min(b,w["end"]) for a,b in rough):
                    original.append(f'<li><a href="/streamer/run/{job}/selection-range/{i}.mp4">'
                                    f'{timecode(w["start"])}–{timecode(w["end"])}</a></li>')
            links += '<p>Historical ranges · no continuous preview.</p>'+("<ul>"+"".join(original)+"</ul>" if original else "<p>Picture unavailable.</p>")
        for page in dict.fromkeys(page for request in current.get("sent",[]) for page in request):
            links += f'<a href="/streamer/run/{job}/{key}/hybrid/{page}">{h(page)}</a> · '
        if "hd_range" in current:
            links += f'<a href="/streamer/run/{job}/selection-range/{current["hd_range"]}.mp4">Locked HD source</a>'
        if verdict=="qualified" and item.get("edition"):
            links += f'<p><a href="/factory?work=done&amp;attempt={job}#story-{item["story"]}">Play final Short</a></p>'
        if decision.get("payoff") is not None:
            intervals += f' · Payoff {timecode(decision["payoff"])}'
        label = {"held":"Not accepted","qualified":"Ready","technical":"Technical failure",
                 "budget":"Budget stop","pending":"Pending","unselected":"Proposed","skipped":"Skipped"}.get(verdict,verdict)
        candidates.append(f'<details><summary>{h(key)} · {label} · {h(intervals)}</summary><p>{h(reason)}</p>{links}</details>')
    trial = intake.get("trial",[0,0])
    requested = trial[1]-trial[0]
    analyzed = intake.get("analyzed",0)
    if config.get("hybrid_diagnostic") and shortlist:
        requested = sum(b-a for e in shortlist.values() for a,b in e["inspection"])
        analyzed = requested if totals["reviewed"] else 0
    tier = config.get("sail_tier","")
    queue = bool(tier=="flex" and row["state"]=="queued" and db.execute(
        "SELECT 1 FROM provider_calls WHERE job=? AND response_id IS NOT NULL AND result IS NULL",(job,)).fetchone())
    clocks = db.execute("SELECT MIN(utc),MAX(utc) FROM factory_events WHERE job=?",(job,)).fetchone()
    waiting = db.execute("""SELECT COALESCE(SUM((julianday(COALESCE(next,'now'))-julianday(utc))*86400),0)
         FROM (SELECT kind,utc,LEAD(utc) OVER(ORDER BY id) AS next FROM factory_events
               WHERE job=? AND (kind='deferred' OR kind='started')) WHERE kind='deferred'""",(job,)).fetchone()[0]
    transfers = load(folder/"media-transfers.json")
    bytes_by = {key:sum(x["received"] for x in transfers if x.get("purpose",x["kind"])==key)
                for key in ("audio","preview","hd","video")}
    calls = [x["usage"] for x in spend["metered_calls"] if x["usage"]]
    tokens = {key:sum(x.get(key,0) for x in calls) for key in ("input_tokens","cached_input_tokens","output_tokens")}
    rate = {"flex": "0.05 / 0.004 / 0.15", "balanced":"0.07 / 0.005 / 0.22"}.get(tier,"Unavailable")
    stages = json.loads(row["timings"])
    elapsed = round(sum(stages.values()),1)
    prompt = next((record["overview"]["version"] for record in hybrid.values() if record.get("overview")),
                  config.get("hybrid_prompt",streamer_hybrid.PROMPT))
    reused = []
    if intake.get("audio") and not bytes_by["audio"]:
        reused.append("retained audio")
    if any("preview_range" in record for record in hybrid.values()) and not bytes_by["preview"]:
        reused.append("verified preview")
    if any("hd_range" in record for record in hybrid.values()) and not bytes_by["hd"]:
        reused.append("verified HD")
    return (f'<section class="workspace-inset"><h3>Attempt {job} results</h3>'
            f'<p>{analyzed/60:.1f} / {requested/60:.1f} minutes · {h(row["stage"])}</p>'
            f'<p>{len(proposed)} proposed · {totals["reviewed"]} reviewed · {totals["ready"]} ready · '
            f'{totals["held"]} not accepted · {totals["technical"]} technical failures · '
            f'{totals["pending"]} pending · {totals["budget"]} budget stops</p>'
            + ('<p role="status">Waiting for Sail Flex</p>' if queue else "")
            + f'<details><summary>Timing, transfer and provider usage</summary><p>Started {h(clocks[0] or row["created"])} UTC'
            + (f' · Finished {h(clocks[1])} UTC' if row["state"] in ("failed","complete") and clocks[1] else "")
            + f' · {elapsed} s recorded worker time · {waiting:.1f} s deferred</p><p>Sail {h(tier.title())} · {h(provider_api.SAIL_MODEL)}'
            + (f' · {h(prompt)}' if config.get("hybrid_version") else "")
            + f'</p><p>{tokens["input_tokens"]:,} input · {tokens["cached_input_tokens"]:,} cached · {tokens["output_tokens"]:,} output tokens · '
            f'{spend["unpriced_calls"]} calls missing usage</p><p>USD per million uncached input / cached input / output {rate}. '
            f'USD {spend["reserved_usd"]:.4f} reserved · USD {spend["known_estimate_usd"] or 0:.6f} estimated token cost.</p>'
            f'<p>{bytes_by["audio"]:,} audio bytes · {bytes_by["preview"]:,} preview bytes · {bytes_by["hd"]:,} HD bytes'
            f' · {bytes_by["video"]:,} earlier video bytes</p>'
            + (f'<p>Reused {h(", ".join(reused))}.</p>' if reused else "")
            + '</details>'
            + "".join(candidates) + '</section>')


def section(db, root, source, attempts, row, zone, token, query):
    sid, job = source["id"],row["id"] if row else None
    title = source["title"] or (source["creator"]+"'s recording" if source["creator"] else "Unassigned recording")
    asset = next((r for r in ([row] if row else [])+attempts if r["asset"]),None)
    duration = asset["duration"] if asset else 0
    folder = root/"streamer-runs"/str(job)
    intake = load(folder/"audio-first.json")
    selection = load(folder/"selection.json")
    production = streamer_run.latest_production(folder)
    reels = available_reels(db,root,folder,job,selection)
    exported = sum(len(items) for items in reels.values())
    ready = sum(items[0]["ready"] for items in reels.values())
    intervals = [(c["start"],c["end"]) for c in selection.get("chapters",[]) if c.get("judgment")!="blocked"]
    covered = factory_metrics.coverage(intervals,0,duration)
    config = json.loads(row["settings"]) if row else {}
    cap = config.get("provider_cap_cents",0)*10000
    spend = report(db,job)["provider_usage"]
    reserved = round(spend["reserved_usd"]*1_000_000)
    pending = db.execute("""SELECT e.id,e.candidate,e.error,e.render_state FROM streamer_editions e
                            WHERE e.job=? AND e.render_state IN ('queued','rendering','failed')
                            AND NOT EXISTS(SELECT 1 FROM streamer_editions newer WHERE newer.job=e.job
                          AND newer.candidate=e.candidate AND newer.rowid>e.rowid AND newer.render_state='exported')
                            ORDER BY e.rowid DESC""",(job,)).fetchall()
    edition_work = next((name for name in ("rendering","queued") if any(r["render_state"]==name for r in pending)),"")
    framing = next((r["error"] for r in pending if r["render_state"]=="failed"),"")
    if not framing:
        for r in db.execute("""SELECT id,candidate FROM streamer_editions e WHERE job=? AND render_state='exported'
                            AND NOT EXISTS (SELECT 1 FROM factory_release_artifacts a
                                WHERE a.edition=e.id AND a.state='removed' AND a.path LIKE '%/portrait.mp4')""",(job,)):
            if not (folder/r["candidate"]/"editions"/r["id"]/"portrait.mp4").is_file():
                framing = "A saved master is missing. Restore it or regenerate it from edition history."
                break
    state = row["state"] if row else ""
    blocked = [c.get("abstention","") for c in selection.get("chapters",[]) if c.get("judgment")=="blocked"]
    blocked += [c.get("rejection","") for c in selection.get("proposals",[]) if "Global ranking pending" in c.get("rejection","")]
    blocked += [c["visual_check"].get("reason","") for c in selection.get("proposals",[])
                if c.get("visual_check",{}).get("verdict")=="unreviewed"]
    budget = bool(selection) and (any("budget" in note.lower() for note in blocked+[production.get("limit","")])
                                  or bool(blocked and cap-reserved<DOWNSTREAM))
    partial = bool(selection) and (bool(blocked) or duration>covered+1)
    if state in ("queued","running") or edition_work:
        category,status = "active",{"queued":"In queue","running":"Processing"}.get(state,"Edition processing")
    elif state in ("failed","stopped"):
        category,status = "attention","Processing blocked" if state=="failed" else "Paused"
    elif partial:
        category,status = "attention",(f'Budget limit · {short_time(covered)} of {short_time(duration)} judged'
                                        if budget else "Partial analysis")
    elif framing or any(hour["complete"] and hour["gap"] for hour in production.get("hours",[])):
        label = "Needs camera framing" if "Needs camera framing" in framing else "Edition needs attention" if framing else "Below hourly target"
        category,status = "attention",label
    elif row:
        category,status = "complete","Ready for review" if exported else "No reels exported"
    else:
        category,status = "empty","Ready to start"
    source_html = '<p>Original available after archiving.</p>'
    if intake:
        source_html = audio_source(folder,job,source["source_url"],intake)
        from .streamer_intake import can_retry_selection
        if state=="complete" and (saved := load(folder/"audio-selection.json")) and not saved["edits"]:
            if can_retry_selection(saved):
                source_html += form(f"/factory/run/{job}/refine-audio-selection",token,button("Refine saved audio selection"))
            options = "".join(f'<option value="{h(c["id"])}">{h(c["id"])} · {h(c.get("reason",""))}</option>'
                              for c in saved["proposals"] if c.get("evidence"))
            if options:
                fields = (f'<p><a href="/streamer/run/{job}/transcript.json">Open timed transcript and word IDs</a></p>'
                          f'<label>Audio proposal<select name="candidate">{options}</select></label>'
                          '<label>Source shots in seconds, one in | out pair per line<textarea name="shots" required></textarea></label>'
                          '<label>Setup word-ID range<input name="setup_ids" required placeholder="10-14"></label>'
                          '<label>Payoff word-ID range<input name="payoff_ids" required placeholder="35-40"></label>'
                          '<label>What the transcript supports<input name="reason" maxlength="160" required></label>')
                source_html += ('<details><summary>Plan selected footage from transcript</summary>'
                                +form(f"/factory/run/{job}/select-from-transcript",token,fields+button("Save source intervals"))
                                +'</details>')
    elif asset:
        prefix = f'/streamer/run/{asset["id"]}'
        stamp = "?at=12998" if source["creator"]=="DougDoug" and duration>13000 else ""
        source_html = player(prefix+"/archive",prefix+"/poster.jpg"+stamp,title+" source"," data-stream-source")
        source_html += '<button type="button" data-source-play>Play original</button>'
    if not row:
        source_html += f'<p><a href="/sources#roster-stream-{sid}">Start from Sources</a></p>'
    history = "".join(f'<li><a href="/factory?attempt={r["id"]}#stream-{sid}">{h(r["created"])} · '
                      f'{"Worker exited" if r["state"]=="complete" else h(r["state"].title())}{" · Selected" if r == row else ""}</a> · '
                      f'<a href="/factory/run/{r["id"]}">Details #{r["id"]}</a></li>' for r in attempts)
    edits = "".join(f'<li><a href="/streamer/run/{job}/{r["candidate"]}/edit?edition={r["id"]}">'
                    f'{h(r["render_state"].title())} edition</a> {h(r["error"])}</li>' for r in pending)
    events = db.execute("""SELECT * FROM factory_events WHERE job=? AND attempt=? AND kind NOT LIKE 'edition%'
                           AND kind<>'pulse' ORDER BY id DESC""",(job,row["attempt"] if row else None)).fetchall()
    receipts = ""
    if job:
        estimate = spend["known_estimate_usd"]
        measured = f"USD {estimate:.6f} metered token estimate" if estimate is not None else "Usage estimate unavailable"
        planned = config.get("template_plan",{})
        style_names = ", ".join(x["name"] for x in planned.get("revisions",[]))
        effective = ""
        if "portal_version" in config:
            overrides = config.get("portal_overrides",{})
            changed = f'Hourly target override · {h(overrides["hourly_target"])}.' if "hourly_target" in overrides else "Factory hourly target."
            effective = (f'<details><summary>Effective controls and planned styles</summary><p>Controls version '
                         f'{h(config["portal_version"])} · {h(config["hourly_target"])} reels per full hour · {changed}</p>'
                         f'<p>Sail {h(config["sail_tier"].title())} · USD {cap/1_000_000:.2f} cap · '
                         f'{h(config["asr_model"])} local ASR · day {h(config["admission_day"])}.</p>'
                         f'<p>Planned styles · {h(style_names)}</p></details>')
        receipts = (f'<p><a href="/factory/run/{job}">Attempt events</a> · <a href="/report/{job}">Provider receipts</a></p>'
                    f'<p>{exported} exports including versions.</p>'
                    +effective+
                    f'<p>USD {reserved/1_000_000:.4f} reserved · {measured} · {spend["unpriced_calls"]} unpriced submissions</p>')
    latest = factory_portal.when(events[0]["utc"] if events else row["created"],factory_portal.tz(zone)) if row else ""
    previous = row and row != attempts[0]
    priority = {"active":0,"attention":2,"complete":3,"empty":4}[category]
    if category=="attention" and (partial and not exported or framing or state in ("failed","stopped")):
        priority = 1
    indicator = (f'<strong data-gallery-count>{totals(reels)}</strong>'
                 f'<span class="workspace-status {category}">{h(status)}</span>'
                 f'{f"<small>Updated {h(latest)}</small>" if latest else ""}')
    attempt_link = f"&amp;attempt={job}" if job else ""
    shortcut = (f'<a class="factory-shortcut" data-quick-stream="{sid}" href="/factory?stream={sid}{attempt_link}#stream-{sid}">'
                f'<span><strong>{h(source["creator"] or "Unassigned")}</strong><small>{h(title)}</small></span>'
                f'<span class="workspace-summary" data-quick-status>{indicator}</span></a>')
    return priority,shortcut,(f'<details id="stream-{sid}" class="workspace-stream" data-stream="{sid}" data-category="{category}"'
        f' data-attempt="{job or ""}">'
        f'<summary><span><strong>{h(title)}</strong><small>{short_time(duration) if duration else "Awaiting archive"}'
        f'{" · Prior attempt selected" if previous else ""}</small></span>'
        f'<span data-workspace-status class="workspace-summary">{indicator}</span></summary>'
        + (f'<p class="workspace-consequence">Ranking or frame checks may still be outstanding. '
           f'<a href="/controls?stream={sid}&amp;attempt={job}#connections">Budget defaults for future jobs</a></p>'
           if budget and partial else "")
        + (f'<p class="workspace-consequence"><a href="/factory?work=done&amp;attempt={job}">Open this attempt in Done</a></p>'
           if config.get("north_yankton") or intake else "")
        + '<div class="workspace-columns">'
        f'<section><h3>Original stream</h3><div data-workspace-source>{source_html}</div></section>'
        '<section><h3>Processing</h3><div data-workspace-processing>'
        +processing(row,asset,events,selection,duration,covered,partial,budget,framing,edition_work,
                    exported,len(reels),ready,zone,token,reserved,cap)
        +'</div></section>'
        f'<section><h3>Output reels</h3><div data-workspace-gallery>'
        f'{gallery(job,selection,reels,token,config.get("hourly_target"),query.get("reel",""))}'
        f'{candidate_results(db,folder,row,intake,selection,production,spend,zone) if row else ""}</div></section></div>'
        f'<details class="workspace-history"><summary>History and diagnostics</summary>{receipts}'
        f'<ul>{edits}</ul><ol>{history}</ol></details></details>')


def current_runs(db,root):
    sources = db.execute("""SELECT s.id,s.link,st.title,c.name AS creator FROM sources s
                           JOIN streamer_streams st ON st.source=s.id JOIN streamer_accounts a ON a.id=st.account
                           JOIN streamer_creators c ON c.id=a.creator
                           WHERE EXISTS (SELECT 1 FROM jobs j WHERE j.source=s.id
                                         AND json_extract(j.settings,'$.method')='streamer')
                           ORDER BY (SELECT MAX(j.id) FROM jobs j WHERE j.source=s.id) DESC""").fetchall()
    groups = []
    for source in sources:
        attempts = db.execute("""SELECT * FROM jobs WHERE source=? AND json_extract(settings,'$.method')='streamer'
                               ORDER BY created DESC,id DESC""",(source["id"],)).fetchall()
        runs = []
        for index,row in enumerate(attempts):
            job = row["id"]
            folder = root/"streamer-runs"/str(job)
            settings = json.loads(row["settings"])
            intake = load(folder/"audio-first.json")
            start = settings.get("coverage_start",0)
            window = intake.get("trial")
            if not window and settings.get("coverage_minutes"):
                window = [start,start+settings["coverage_minutes"]*60]
            timeframe = f'{timecode(window[0])}–{timecode(window[1])}' if window else "Full source"
            results = streamer_run.latest_production(folder)
            kept,cards = set(),[]
            ready = 0
            for edition in db.execute("""SELECT e.*, h.reason AS held FROM streamer_editions e
                                         LEFT JOIN done_release_holds h ON h.edition=e.id
                                         WHERE e.job=? AND e.render_state='exported' ORDER BY e.rowid DESC""",(job,)):
                key = edition["candidate"]
                path = folder/key/"editions"/edition["id"]/"portrait.mp4"
                if key in kept or not path.is_file():
                    continue
                kept.add(key)
                plan = json.loads(edition["plan"])
                url = f'/streamer/run/{job}/{key}/edition/{edition["id"]}'
                good = edition["qa"] and edition["qualification"]=="qualified" and not edition["held"]
                ready += bool(good)
                label = "Ready to publish" if good else "Editorial hold" if edition["held"] else "Ready to view"
                cards.append(f'<article class="factory-reel-card"><video preload="none" controls playsinline '
                             f'poster="{url}/poster.jpg" src="{url}/portrait.mp4" aria-label="{h(plan["title"])}"></video>'
                             f'<h4>{h(plan["title"])}</h4><p>{plan["duration"]:.3f} s · {label}</p>'
                             f'<a href="{url}/portrait.mp4">Open MP4</a></article>')
            state = row["state"]
            waiting = db.execute("SELECT 1 FROM provider_calls WHERE job=? AND response_id IS NOT NULL AND result IS NULL",
                                 (job,)).fetchone()
            busy = state in ("queued","running")
            stage = "Waiting for Sail Flex" if state=="queued" and waiting else row["stage"]
            failures = sum(item["state"] in ("technical","budget") for item in results.get("items",[]))
            if busy:
                cards.append(f'<article class="factory-reel-card factory-placeholder"><p>{h(stage)}</p>'
                             f'<small>{h(row["progress"] or "Preparing this run")}</small></article>')
            elif not cards:
                cards.append(f'<p>{h(row["error"] or "No playable reels saved.")}</p>')
            usage = report(db,job)["provider_usage"]
            cost = usage["known_estimate_usd"]
            summary = f'Run {job} · {timeframe} · {len(kept)} reels'
            badge = ("Processing blocked" if state in ("failed","stopped") else
                     "Ready to publish" if ready and ready==len(kept) and not busy else stage)
            runs.append(f'<details class="factory-source-run" id="run-{job}" data-current-run="{job}" '
                        f'data-busy="{int(busy)}"{" open" if index==0 else ""}>'
                        f'<summary><strong>{h(summary)}</strong><span>{h(badge)}</span></summary>'
                        f'<p>Started {h(row["created"])} UTC · {ready} ready to publish'
                        + (f' · USD {cost:.6f} estimated' if cost is not None else "")
                        + (f' · {failures} technical failures' if failures else "") + '</p>'
                        + f'<div class="factory-reel-grid">{"".join(cards)}</div>'
                        + f'<details><summary>Run details</summary><p>USD {usage["reserved_usd"]:.4f} reserved · '
                          f'<a href="/report/{job}">Provider and job report</a> · <a href="/factory/run/{job}">Events</a>'
                          f' · <a href="/factory?stream={source["id"]}&amp;attempt={job}#stream-{source["id"]}">Candidate evidence</a>'
                          '</p></details></details>')
        groups.append(f'<section class="factory-source" id="source-{source["id"]}"><h2>{h(source["title"])}</h2>'
                      f'<p>{h(source["creator"])} · <a href="{h(source["link"])}">Source video</a></p>'
                      + "".join(runs) + '</section>')
    return ('<div class="hero"><h1>Factory</h1></div><nav class="factory-work-tabs" aria-label="Factory work area">'
            '<a href="/factory" aria-current="page">Current work</a><a href="/factory?work=done">Done</a>'
            '<a href="/factory?work=history">History</a></nav><div data-factory-current>'
            + ("".join(groups) or '<section class="panel operator-panel"><h2>No runs yet</h2>'
               '<p>Start a source in <a href="/sources">Sources</a>.</p></section>') + '</div>')


def home(db, root, token, query):
    if not any(query.get(key) for key in ("stream","attempt","pair","review")) and query.get("work","current")=="current":
        return current_runs(db,root)
    focus = db.execute("SELECT job FROM factory_focus").fetchone()[0]
    tab = query.get("work","current")
    tabs = (("current","Current work","/factory"),("done","Done","/factory?work=done"),
            ("history","History","/factory?work=history"))
    navigation = '<nav class="factory-work-tabs" aria-label="Factory work area">'+"".join(
        f'<a href="{h(url)}"{" aria-current=page" if tab==key else ""}>{label}</a>'
        for key,label,url in tabs)+'</nav>'
    if tab=="done":
        return navigation+factory_done.page(db,root,token,query)
    query = dict(query)
    focus_only = focus and tab!="history" and query.get("attempt")==str(focus)
    if focus_only:
        target = db.execute("SELECT source FROM jobs WHERE id=?",(focus,)).fetchone()
        if query.get("stream") and query["stream"]!=str(target["source"]):
            focus_only = False
        else:
            query.update(stream=str(target["source"]),attempt=str(focus))
    settings = db.execute("SELECT * FROM factory_controls").fetchone()
    sources = db.execute("""SELECT s.id,s.link AS source_url,st.title,c.id AS creator_id,COALESCE(c.name,s.creator) AS creator
                            FROM sources s LEFT JOIN streamer_streams st ON st.source=s.id
                            LEFT JOIN streamer_accounts a ON a.id=st.account
                            LEFT JOIN streamer_creators c ON c.id=a.creator ORDER BY s.id""").fetchall()
    history = defaultdict(list)
    for row in db.execute("SELECT j.*,a.duration,a.bytes FROM jobs j LEFT JOIN assets a ON a.hash=j.asset ORDER BY j.id DESC"):
        history[row["source"]].append(row)
    groups,names,shortcuts = defaultdict(list),{},[]
    for source in sources:
        attempts = history[source["id"]]
        runs = [r for r in attempts if json.loads(r["settings"]).get("method")=="streamer"]
        if focus_only and not any(r["id"]==focus for r in runs):
            continue
        if source["creator_id"] is None and not runs:
            continue
        chosen = next((r for r in attempts if str(r["id"])==query.get("attempt")), (runs or [None])[0])
        key = f'creator-{source["creator_id"]}' if source["creator_id"] is not None else "unassigned"
        names[key] = source["creator"] if source["creator_id"] is not None else "Unassigned"
        history_rows = [chosen] if focus_only else attempts
        priority,shortcut,body = section(db,root,source,history_rows,chosen,settings["zone"],token,query)
        groups[key].append((priority,body))
        if attempts:
            shortcuts.append((priority,source["creator"] or "",shortcut))
    creators = "".join(f'<option value="{key}">{h(name)}</option>' for key,name in sorted(names.items(),key=lambda x:x[1]))
    streams = "".join(f'<section class="workspace-group" data-creator="{key}"><h2>{h(names[key])}</h2>'
                      + "".join(body for _,body in sorted(groups[key],key=lambda x:x[0]))+"</section>"
                      for key in sorted(groups,key=lambda key:(min(p for p,_ in groups[key]),names[key])))
    chosen = query.get("stream","")
    if not chosen and query.get("attempt","").isdigit():
        selected = db.execute("SELECT source FROM jobs WHERE id=?",(query["attempt"],)).fetchone()
        chosen = str(selected["source"]) if selected else ""
    legacy = [row for attempts in history.values() for row in attempts if json.loads(row["settings"]).get("method")!="streamer"]
    links = "".join(f'<li><a href="/factory/run/{row["id"]}">Attempt {row["id"]}</a> · '
                    f'<a href="/report/{row["id"]}">Evidence and report</a></li>' for row in legacy)
    review = ""
    selected = next((row for row in legacy if str(row["id"])==query.get("attempt")),None)
    if selected:
        review = views.review(db,token,selected["id"])
    pair = query.get("pair","")
    if len(pair)==32 and db.execute("SELECT 1 FROM blind_pairs WHERE id=?",(pair,)).fetchone():
        review += (f'<p><a href="/blind/{h(pair)}/a">Saved comparison A</a> · '
                   f'<a href="/blind/{h(pair)}/b">Saved comparison B</a> · '
                   f'<a href="/blind/{h(pair)}/results">Recorded observations</a></p>')
    legacy_details = (f'<details class="workspace-settings" id="legacy-work"{" open" if review else ""}>'
                      f'<summary>Historical jobs and comparison evidence</summary><ol>{links}</ol>{review}</details>')
    overview = ('<div class="factory-overview" aria-label="Stream overview">'
                +"".join(item[2] for item in sorted(shortcuts))+"</div>" if shortcuts else "")
    return (f'<div class="workspace" data-creator-workspace data-selected-stream="{h(chosen)}" '
            f'data-initial-review="{h(query.get("review",""))}"><div class="hero"><h1>Factory</h1></div>{navigation}'
            + ('<p>Focused on the selected editing attempt. <a href="/factory?work=history">Show history / All streams</a></p>'
               if focus_only else "") +
            '<div class="workspace-filters"><label>Creator<select data-creator-filter>'
            f'<option value="">All creators</option>{creators}</select></label>'
            '<label>Status<select data-stream-filter><option value="">All streams</option><option value="active">Active</option>'
            '<option value="attention">Needs attention</option><option value="complete">Finished</option>'
            '<option value="empty">Ready to start</option></select></label>'
            '<button type="button" data-collapse-streams>Collapse idle streams</button></div>'
            '<p hidden data-no-streams>No streams match these filters.</p>'
            f'{overview}'
            f'<div data-workspace-streams>{streams or "<p>Add a creator and attach a stream in Sources.</p>"}</div>'
            + ('' if focus_only else f'{legacy_details}<p><a href="/sources#unassigned">Unassigned recordings</a></p>')+'</div>')
