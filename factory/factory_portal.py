"""Local portal for queue state, stage events, quality and hourly yield."""

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import factory_metrics, media, streamer_asr, views
from .provider_meter import report

h, form, button = views.h, views.form, views.button
ZONES = ("Asia/Shanghai","UTC","America/New_York","Europe/London")


def tz(name):
    if name not in ZONES:
        raise ValueError("Choose a supported timezone.")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        bundled = media.TOOLS/"python"/"tzdata"/"zoneinfo"/name
        if bundled.is_file():
            with bundled.open("rb") as zonefile:
                return ZoneInfo.from_file(zonefile,key=name)
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        if name == "UTC":
            return timezone.utc
        raise ValueError("That timezone requires local tzdata.") from None


def when(value, zone):
    if not value:
        return "Unavailable"
    moment = datetime.fromisoformat(value.replace("Z","+00:00"))
    return moment.replace(tzinfo=moment.tzinfo or timezone.utc).astimezone(zone).strftime("%d %b %H:%M:%S")


def age(value):
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z","+00:00"))
    return max(0,(datetime.now(timezone.utc)-moment.replace(tzinfo=moment.tzinfo or timezone.utc)).total_seconds())


def link(job, label, part=""):
    return f'<a href="/factory/run/{job}{part}">{h(label)}</a>'


def title(row):
    config = json.loads(row["settings"])
    stream = config.get("stream",{})
    return f"{stream.get('name') or row['creator']} · {stream.get('title') or 'Recording'}"


def supported(name):
    try:
        tz(name)
        return True
    except ValueError:
        return False


def run_card(db,row,zone,token=None):
    job = row["id"]
    event = db.execute("""SELECT * FROM factory_events WHERE job=? AND kind NOT LIKE 'edition%'
                          ORDER BY id DESC LIMIT 1""",(job,)).fetchone()
    progress = db.execute("""SELECT utc,item,kind FROM factory_events WHERE job=?
                             AND kind NOT LIKE 'edition%' AND kind<>'pulse' ORDER BY id DESC LIMIT 1""",
                          (job,)).fetchone()
    start = db.execute("""SELECT MIN(utc) FROM factory_events WHERE job=? AND attempt=? AND stage=?
                          AND kind='started'""",(job,row["attempt"],row["stage"])).fetchone()[0]
    current = age(start)
    label = row["state"].title()
    if row["state"]=="running" and event and age(event["utc"]) > 25:
        label = "Stale heartbeat"
    elif row["stop"]:
        label = "Pause requested"
    elif row["retry"]:
        label = "Waiting to retry"
    elif row["source_state"]!="queued" and row["state"]!="complete":
        label = "Source paused or archived"
    elif row["state"]=="stopped" and row["error"]:
        label = "Blocked"
    coverage = "Coverage unavailable"
    if event and event["covered_seconds"] is not None and event["total_seconds"]:
        coverage = f'{event["covered_seconds"]/60:.1f} / {event["total_seconds"]/60:.1f} source minutes'
    else:
        last_covered = db.execute("""SELECT covered_seconds,total_seconds FROM factory_events
                                     WHERE job=? AND stage=? AND covered_seconds IS NOT NULL AND total_seconds>0
                                     ORDER BY id DESC LIMIT 1""",(job,row["stage"])).fetchone()
        if last_covered:
            coverage = (f'Last saved coverage · {last_covered["covered_seconds"]/60:.1f} / '
                        f'{last_covered["total_seconds"]/60:.1f} source minutes')
    cost = report(db,job)["provider_usage"]["reserved_usd"]
    config = json.loads(row["settings"])
    cap = config.get("provider_cap_cents",0)/100
    cap_warning = ""
    if event and event["total_seconds"] and "hourly_target" in config:
        target = config["hourly_target"]
        reserve_floor = int(event["total_seconds"]//3600)*(5*.0045+target*.005)
        if cap<reserve_floor:
            cap_warning = (f"<p>Budget shortfall · ${reserve_floor:.3f} estimated for chapter discovery and "
                           "one evidence check per target story, before retries.</p>")
    stage = h(row["stage"])
    transfer = f' · {event["bytes"]/(2**20):.1f} MiB observed' if event and event["bytes"] is not None else ""
    since = f' · {int(current)} s in stage' if current is not None and row["state"]=="running" else ""
    last = (f"{when(progress['utc'],zone)} · {progress['item'] or progress['kind']}" if progress
            else "Events unavailable for this historical attempt")
    retry = when(datetime.fromtimestamp(row["retry"],timezone.utc).isoformat(),zone) if row["retry"] else None
    if retry:
        next_action = f"Retry at {retry}"
    elif row["state"]=="running":
        next_action = "Stop at the next saved checkpoint" if row["stop"] else "Continue current stage"
    elif row["state"]=="complete":
        next_action = "Open review"
    elif row["state"]=="queued":
        next_action = "Wait for worker claim"
    elif "free-space" in row["error"]:
        next_action = "Free local space or start with an appropriate reserve"
    elif row["source_state"]!="queued" or row["revision"]!=row["current"]:
        next_action = "Check the currently selected source revision"
    elif (row["error"] and row["state"]=="stopped") or any(
            code in row["error"].lower() for code in ("http 401","http 403")):
        next_action = "Check provider connection, then resume"
    else:
        next_action = "Inspect the last failure, then resume"
    actions = link(job,"Event timeline","#events")
    if row["asset"]:
        actions += f'<a href="/streamer/run/{job}/archive">Verified archive</a>'
    if token and row["state"] in ("failed","stopped") and row["source_state"]=="queued" and row["revision"]==row["current"]:
        actions += form(f"/factory/run/{job}/resume",token,button("Resume from checkpoint"))
    if "budget" in row["error"].lower() and token:
        actions += form(f"/factory/run/{job}/budget",token,
                        f'<label>New cap in cents<input name="cap" type="number" min="{int(cap*100)+1}" max="1000" required></label>'
                        + button("Save cap"))
    last_success = db.execute("SELECT utc,stage FROM factory_events WHERE job=? AND kind='finished' ORDER BY id DESC LIMIT 1",
                              (job,)).fetchone()
    heartbeat = db.execute("SELECT utc FROM factory_events WHERE job=? AND attempt=? AND kind='pulse' ORDER BY id DESC LIMIT 1",
                           (job,row["attempt"])).fetchone()
    success = f"{last_success['stage']} at {when(last_success['utc'],zone)}" if last_success else "Unavailable"
    return (f'<article class="source"><h3>{link(job, f"Run {job} · {title(row)}")}</h3>'
            f'<p>{h(label)} · {stage}{since} · Attempt {row["attempt"]}</p><p>Last progress · {h(last)}</p>'
            f'<p>Last heartbeat · {h(when(heartbeat["utc"] if heartbeat else None,zone))}</p>'
            f'<p>{h(coverage)}{transfer} · <a href="/report/{job}">Reserved ${cost:.3f} / ${cap:.2f} cap</a>'
            f' · ${max(0,cap-cost):.3f} headroom</p>'
            f'{cap_warning}<p>Last finished stage · {h(success)}</p>'
            f'<p>{h(row["error"])}</p><p>Next action · {h(next_action)}</p><div class="actions">{actions}</div></article>')


def detail(db,root,token,job,query):
    row = db.execute("""SELECT j.*,s.creator,s.state AS source_state,s.revision AS current,
                       (SELECT MAX(next_try) FROM provider_calls WHERE job=j.id AND result IS NULL
                        AND next_try>strftime('%s','now')+0) AS retry
                       FROM jobs j JOIN sources s ON s.id=j.source WHERE j.id=?""",(job,)).fetchone()
    if not row:
        raise ValueError("Run not found.")
    zone = tz(db.execute("SELECT zone FROM factory_controls").fetchone()[0])
    event_rows = db.execute("SELECT * FROM factory_events WHERE job=? ORDER BY id DESC",(job,)).fetchall()
    timeline = ''.join(
        f'<tr><td><time datetime="{h(e["utc"])}" title="{h(e["utc"])}">{h(when(e["utc"],zone))}</time></td>'
        f'<td>{e["attempt"]}</td><td>{h(e["stage"])} · {h(e["kind"])}</td>'
        f'<td>{h(e["item"])}</td><td>{f"{e['covered_seconds']/60:.1f}" if e["covered_seconds"] is not None else "—"}</td>'
        f'<td>{f"{e['bytes']:,}" if e["bytes"] is not None else "—"}</td>'
        f'<td>{f"${e['reserved_micros']/1e6:.3f}" if e["reserved_micros"] is not None else "—"}</td>'
        f'<td>{h(e["error_class"])} {h(when(e["retry_utc"],zone) if e["retry_utc"] else "")}</td></tr>'
        for e in event_rows)
    table = ('<div class="factory-table"><table><thead><tr><th>Local time</th><th>Attempt</th><th>Stage</th>'
             '<th>Item</th><th>Minutes covered</th><th>Bytes</th><th>Reserved</th><th>Exception or retry</th></tr></thead>'
             f'<tbody>{timeline}</tbody></table></div>') if timeline else '<p>Run completed before stage events were recorded.</p>'
    selection_path = root/"streamer-runs"/str(job)/"selection.json"
    funnel = funnel_view(db,root,job,streamer_asr.read(selection_path),query) if selection_path.is_file() else ""
    first = selection_path.parent/"production.json"
    revisit = selection_path.parent/"production-revisit.json"
    recovery = ""
    if row["state"]=="complete" and first.is_file() and not revisit.is_file() and any(
            hr["complete"] and hr["gap"] for hr in streamer_asr.read(first)["hours"]):
        recovery = form(f"/factory/run/{job}/revisit",token,button("Run bounded follow-up"))
    return (f'<div data-factory-refresh class="factory-run"><div class="hero"><h1>Run {job} monitor</h1>'
            f'<a class="button" href="/factory?stream={row["source"]}&amp;attempt={job}#stream-{row["source"]}">Back to Factory stream</a></div>'
            f'<section class="panel">{run_card(db,row,zone,token)}{recovery}</section>{funnel}'
            f'<section class="panel" id="events"><h2>Stage events</h2>{table}</section></div>')


def funnel_view(db,root,job,data,query):
    settings = json.loads(db.execute("SELECT settings FROM jobs WHERE id=?",(job,)).fetchone()[0])
    target = settings.get("hourly_target",7)
    hours = factory_metrics.funnel(root,db,job,data,target)
    kinds = ("discovered","merged","candidate","rejected","qualified","independent","awaiting","exported","qa","delivered")
    reserve = report(db,job)["provider_usage"]["reserved_usd"]
    capped = settings.get("provider_cap_cents",0)/100-reserve < .0045
    rows = []
    for index,hour in enumerate(hours):
        gap = max(0,target-len(hour["qualified"])) if hour["complete"] else None
        fields = "".join(f'<td>{link(job,len(hour[k]),f"?hour={index}&amp;filter={k}#clips")}</td>' for k in kinds)
        reason = ("Evidence and review shortfall" if gap else "Target met") if hour["complete"] else "Partial hour"
        if gap and capped:
            reason = "Reservation limit reached with a quality shortfall"
        if hour["covered"]+1 < hour["end"]-hour["start"]:
            reason = "Source coverage gap"
        rows.append(f'<tr><td>{index+1}{" · partial" if not hour["complete"] else ""}</td>'
                    f'<td>{hour["covered"]/60:.1f} / {(hour["end"]-hour["start"])/60:.1f} min</td>{fields}'
                    f'<td>{target if hour["complete"] else "—"}</td><td>{gap if gap is not None else "—"}</td><td>{reason}</td></tr>')
    qualified = sum(len(hr["qualified"]) for hr in hours)
    covered = sum(hr["covered"] for hr in hours)
    rate = qualified*3600/covered if covered else 0
    gaps = max(0,data["asset"]["duration"]-covered)
    gap_measure = f"{gaps/60:.1f}"
    per_story = f'${reserve/qualified:.3f} reserved per qualified story' if qualified else 'Per-story spend unavailable'
    snapshot = f"Production target {target}/hour" if "hourly_target" in settings else (
        f"Historical render target {settings.get('target',data.get('target',0))} · {target}/hour reference")
    browser = f'/streamer/run/{job}'
    production_path = root/"streamer-runs"/str(job)/"production.json"
    production = streamer_asr.read(production_path) if production_path.is_file() else {}
    revisit_path = production_path.with_name("production-revisit.json")
    revisit = streamer_asr.read(revisit_path) if revisit_path.is_file() else {}
    outcomes = {item["id"]:item for item in production.get("items",[])}
    outcomes.update({item["id"]:item for item in revisit.get("items",[])})
    lookup = {x["id"]:x for x in data["proposals"]}
    chosen = list(lookup)
    descriptor = "All reviewed evidence"
    expanded = ""
    if query.get("filter") in kinds and re.fullmatch(r"\d+",query.get("hour","")):
        i = min(len(hours)-1,int(query["hour"]))
        kind = query["filter"]
        descriptor = f"Hour {i+1} · {kind.title()}"
        chosen = hours[i][kind]
        expanded = " open" if len(chosen)<=5 else ""
    cards = []
    for key in chosen:
        candidate, _, edition = key.partition("/")
        c = lookup[candidate]
        outcome = outcomes.get(candidate,{})
        at = factory_metrics.payoff(c)
        review_path = root/"streamer-runs"/str(job)/candidate/"review.json"
        review = streamer_asr.read(review_path) if review_path.is_file() else {}
        reason = (outcome.get("reason") or review.get("reason") or c.get("rejection")
                  or (c.get("visual_check") or {}).get("reason") or c.get("reason") or "Review the source beat.")
        if not edition:
            edition = outcome.get("edition","")
        if not edition:
            version = db.execute("""SELECT id FROM streamer_editions WHERE job=? AND candidate=?
                                    AND qualification='qualified' AND render_state='exported' ORDER BY rowid DESC LIMIT 1""",
                                 (job,candidate)).fetchone()
            edition = version["id"] if version else ""
        direct = f'{browser}/{candidate}/edit' + (f'?edition={edition}' if edition else '')
        artifact = f'<a href="{direct}">Open version and source</a>' if c.get("evidence") else ""
        auditions = "".join(f'<li><a href="{browser}/{candidate}/audition/{a["id"]}/portrait.mp4">{h(a["title"])}</a>'
                             f' · {h("QC clear" if a["passed"] else "; ".join(a["issues"]))}</li>'
                             for a in outcome.get("auditions",[]) if (root/"streamer-runs"/str(job)/candidate/
                                 "auditions"/a["id"]/"portrait.mp4").is_file())
        cards.append(f'<details class="source"{expanded}><summary>{h(candidate)} · {views.timecode(at)} · '
                     f'{h(c.get("hook") or c.get("reason") or "Source evidence")}</summary><p>{h(reason)}</p>'
                     f'<img loading="lazy" alt="Source frame at {views.timecode(at)}" src="/factory/run/{job}/frame/{candidate}">'
                     f'<p>{artifact}</p>{f"<ul>{auditions}</ul>" if auditions else ""}</details>')
    independent = sum(len(hr["independent"]) for hr in hours)
    budget_notice = ""
    if "hourly_target" in settings:
        full_hours = sum(hr["complete"] for hr in hours)
        estimate = full_hours*(5*.0045+target*.005)
        if settings["provider_cap_cents"]/100<estimate:
            budget_notice = ("<p>The reservation cap is below the minimum estimate for one discovery pass "
                             "and one evidence check per target story.</p>")
    if revisit.get("limit"):
        budget_notice += f'<p>Follow-up ended · {h(revisit["limit"])}</p>'
    chapter_notes = "".join(
        f'<li>{views.timecode(c["start"])}–{views.timecode(c["end"])} · {h(c.get("judgment","historical"))}'
        f'{" · " + h(c["followup"]) if c.get("followup") else ""} · {h(c.get("abstention",""))}</li>'
        for c in data.get("chapters",[]))
    return (f'<section class="panel"><h2>Hourly story funnel</h2><p>{h(snapshot)}</p>'
            f'<p><a href="{browser}/archive">{data["asset"]["duration"]/60:.1f} min archived</a>'
            f' · <a href="#events">{covered/60:.1f} min analyzed · {gap_measure} min uncovered</a> · '
            f'<a href="#clips">{qualified} unique qualified</a> · {independent} independently accepted · {rate:.2f}/analyzed hour'
            f' · <a href="/report/{job}">{h(per_story)}</a></p>{budget_notice}'
            '<div class="factory-table"><table><thead><tr><th>Hour</th><th>Coverage</th>'
            + "".join(f"<th>{name}</th>" for name in ("Discovered","Merged","Candidates","Rejected","Qualified",
                                                      "Independent","Awaiting review","Editions","QA passing","Delivered",
                                                      "Target","Gap","Reason"))
            + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<details><summary>Chapter coverage and abstentions</summary><ul>{chapter_notes}</ul></details></section>'
            f'<section class="panel" id="clips"><h2>{h(descriptor)}</h2><a href="{browser}">Open run review</a>'
            + ("".join(cards) if cards else "<p>No items in this count.</p>") + "</section>")


def frame(root,job,identity):
    folder = root/"streamer-runs"/str(job)
    data = streamer_asr.read(folder/"selection.json")
    candidate = next((c for c in data["proposals"] if c["id"]==identity),None)
    if not candidate:
        raise ValueError("No such proposal.")
    at = min(data["asset"]["duration"]-.12,factory_metrics.payoff(candidate))
    master = (root/data["asset"]["path"]).resolve()
    if not master.is_relative_to((root/"masters").resolve()):
        raise ValueError("Source footage unavailable.")
    result = media.run([media.require("ffmpeg"),"-v","error","-ss",f"{max(0,at):.3f}","-i",str(master),
                        "-frames:v","1","-vf","scale=480:-2","-c:v","mjpeg","-threads","1","-f","image2pipe","-"],20)
    if result.returncode or not result.stdout:
        raise ValueError("Source frame unavailable.")
    return result.stdout
