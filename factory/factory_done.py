"""Persistent source events, decoded editions, and account-level publication evidence."""

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import media, shorts_release, streamer_asr, views

h,form,button = views.h,views.form,views.button
PLATFORMS = ("youtube","instagram","tiktok","facebook")
DELIVERY = {"queued":"Queued","failed":"Failed","unknown":"Unknown","scheduled":"Scheduled",
            "published":"Published","manual":"Recorded manually"}


class ReconciliationRequired(media.MediaError):
    pass


def match(db,job,candidate,plan):
    source = db.execute("""SELECT s.id,s.identity,s.link,c.name,st.title FROM jobs j JOIN sources s ON s.id=j.source
                           JOIN streamer_streams st ON st.source=s.id JOIN streamer_accounts a ON a.id=st.account
                           JOIN streamer_creators c ON c.id=a.creator WHERE j.id=?""",(job,)).fetchone()
    known = db.execute("SELECT story FROM done_matches WHERE job=? AND candidate=?",(job,candidate)).fetchone()
    if known:
        return known["story"]
    source_key = source["identity"] if not source["identity"].startswith("file:") else "archive:"+plan["asset"]
    evidence = plan["proof"]
    marks = [evidence[key] for key in ("start","source","end")]
    if not all(math.isfinite(value) for value in marks) or not 0<=marks[0]<=marks[1]<=marks[2] or marks[0]==marks[2]:
        raise ReconciliationRequired("The verified source proof interval is missing or invalid.")
    found = []
    aliases = [(job,candidate),*evidence.get("earlier",[])]
    for earlier,number in aliases:
        row = db.execute("""SELECT m.story,s.source_key FROM done_matches m JOIN done_stories s ON s.id=m.story
                            WHERE m.job=? AND m.candidate=?""",(earlier,number)).fetchone()
        if row:
            if row["source_key"]!=source_key:
                raise ReconciliationRequired("Saved proof was matched to a different source.")
            found.append(row["story"])
    near = db.execute("""SELECT * FROM done_stories WHERE source_key=? AND
                         ((proof_from<? AND proof_to>?) OR (event_key=? AND ABS(proof-?)<=5))
                         ORDER BY id""",(source_key,evidence["end"],evidence["start"],evidence["event"],evidence["source"])).fetchall()
    same = [row["id"] for row in near if row["event_key"]==evidence["event"] and abs(row["proof"]-evidence["source"])<=5]
    matches = set(found+same)
    if len(matches)>1 or any(row["id"] not in matches for row in near):
        raise ReconciliationRequired("Overlapping source proof needs story reconciliation before this edition can enter Done.")
    if matches:
        story = matches.pop()
    else:
        story = db.execute("""INSERT INTO done_stories(source,source_key,event_key,proof,proof_from,proof_to,source_title,source_url,creator)
                              VALUES(?,?,?,?,?,?,?,?,?)""",(source["id"],source_key,evidence["event"],evidence["source"],
                              evidence["start"],evidence["end"],source["title"],source["link"],source["name"])).lastrowid
    for earlier,number in aliases:
        db.execute("""INSERT OR IGNORE INTO done_matches VALUES(?,?,?,?)""",(earlier,number,story,
                   "Verified pilot proof" if earlier==job else "Matched retained source evidence"))
    return story


def finish(root, db, job, candidate, edition, plan, target):
    manifest = streamer_asr.read(target/"manifest.json")
    check = streamer_asr.read(target/"qc.json")
    movie = target/"portrait.mp4"
    if (not movie.is_file() or not check["passed"] or manifest.get("rendered_style")!=plan.get("render_style")
            or manifest.get("profile")!="master" or media.checksum(movie)!=manifest["portrait_sha256"]):
        raise media.MediaError("The playable master and its successful editing check are required for Done.")
    shorts_release.require(movie,bounds=shorts_release.bounds_for(plan))
    present = db.execute("SELECT story FROM done_editions WHERE edition=?",(edition,)).fetchone()
    if present:
        return present["story"]
    story = match(db,job,candidate,plan)
    db.execute("INSERT INTO done_editions(edition,story,digest) VALUES(?,?,?)",(edition,story,manifest["portrait_sha256"]))
    return story


def set_hold(db, story, edition, reason):
    current = db.execute(shorts_release.CURRENT_DONE, (story,)).fetchone()
    if not current or current["edition"] != edition:
        raise ValueError("Reload Done to review the current edition.")
    reason = reason.strip()
    if not 1 <= len(reason) <= 2000:
        raise ValueError("Enter the playback or source observations behind the hold.")
    db.execute("""INSERT INTO done_release_holds(story,edition,reason) VALUES(?,?,?)
                  ON CONFLICT(story,edition) DO UPDATE SET reason=excluded.reason,recorded=CURRENT_TIMESTAMP""",
               (story,edition,reason))


def current_count(db, root, job):
    rows = db.execute("""SELECT e.* FROM streamer_editions e JOIN done_editions d ON d.edition=e.id
                         WHERE e.job=? AND d.rowid=(SELECT max(n.rowid) FROM done_editions n WHERE n.story=d.story)""",
                      (job,))
    return sum(shorts_release.eligible(db, root, row) for row in rows)


def account_intent(db,story,edition,platform,account):
    if platform not in PLATFORMS or not 1<=len(account)<=120:
        raise ValueError("Choose a platform and enter the destination account.")
    account = account.strip().casefold()
    if not db.execute("SELECT 1 FROM done_editions WHERE story=? AND edition=?",(story,edition)).fetchone():
        raise ValueError("Choose an edition that belongs to this story.")
    db.execute("""INSERT OR IGNORE INTO done_delivery_intents(story,edition,platform,account) VALUES(?,?,?,?)""",
               (story,edition,platform,account))
    return db.execute("""SELECT * FROM done_delivery_intents WHERE story=? AND platform=? AND account=?""",
                      (story,platform,account)).fetchone()


def post_identity(value):
    token = value.strip()
    url = urlsplit(token)
    if url.scheme=="https" and url.hostname and not url.username and not url.password:
        parts = url.path.strip("/").split("/")
        found = parse_qs(url.query).get("v",[]) if url.hostname in ("youtube.com","www.youtube.com","m.youtube.com") else []
        post_id = found[0] if found else parts[-1] or url.netloc
        return post_id[:160],token[:2000]
    if re.fullmatch(r"[\w.-]{2,160}",token):
        return token,None
    raise ValueError("Enter an HTTPS post link or the destination’s post ID.")


def record(db,story,edition,platform,account,status,remote="",occurred=None,evidence=""):
    if status not in DELIVERY:
        raise ValueError("Choose a valid delivery state.")
    if status in ("manual","published","scheduled") and (not remote or not occurred):
        raise ValueError("A post ID or link and an event timestamp are required for this status.")
    if status=="published" and not evidence:
        raise ValueError("Confirmed publication requires provider evidence.")
    if occurred:
        stamp = datetime.fromisoformat(occurred)
        if stamp.tzinfo is None:
            raise ValueError("Use a timestamp with its local timezone offset.")
        occurred = stamp.astimezone(timezone.utc).isoformat()
    intent = account_intent(db,story,edition,platform,account.strip())
    post_id,url = post_identity(remote) if remote else (None,None)
    existing = db.execute("SELECT * FROM done_delivery_receipts WHERE platform=? AND account=? AND remote_id=?",
                          (platform,intent["account"],post_id)).fetchone() if post_id else None
    if existing and existing["intent"]!=intent["id"]:
        raise ValueError("This remote post is already recorded for another story.")
    if existing:
        precedence = {"queued":0,"unknown":0,"failed":0,"scheduled":1,"manual":2,"published":3}
        if precedence[status]>precedence[existing["status"]]:
            db.execute("""UPDATE done_delivery_receipts SET status=?,occurred=?,remote_url=COALESCE(?,remote_url),evidence=?
                          WHERE id=?""",(status,occurred,url,evidence,existing["id"]))
        return existing["id"]
    return db.execute("""INSERT INTO done_delivery_receipts(intent,edition,platform,account,status,remote_id,
                          remote_url,occurred,evidence) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (intent["id"],edition,platform,intent["account"],status,post_id,url,occurred,evidence)).lastrowid


def status(receipts):
    # The strongest confirmed outcome per account determines its current label.
    priority = {"queued":0,"unknown":0,"failed":0,"scheduled":1,"manual":2,"published":3}
    states = {}
    for row in receipts:
        key = row["platform"],row["account"]
        state = row["status"] or "queued"
        if key not in states or priority[state]>=priority[states[key]]:
            states[key] = state
    values = set(states.values())
    if values=={"published"}:
        return "Published"
    if "published" in values:
        return "Partially published"
    if "manual" in values:
        return "Recorded manually"
    if "scheduled" in values:
        return "Scheduled"
    return "No recorded publication"


def indiepost_panel(db, story, current, token, eligible):
    from . import daily_release, indiepost

    identity = story["id"]
    owned = indiepost.deliveries(db, identity)
    endpoint = f"/factory/done/{identity}/indiepost"
    rows = []
    for delivery in owned:
        state = indiepost.STATES[delivery["state"]]
        stamp = datetime.fromisoformat(delivery["slot"]).strftime("%d %b %H:%M UTC")
        rows.append(f'<li><strong>{h(indiepost.PLATFORM_NAMES[delivery["platform"]])} · {h(delivery["account_name"])}</strong> '
                    f'· {state} · {stamp}'
                    + (f'<p>{h(delivery["detail"])}</p>' if delivery["detail"] else "")
                    + (f'<p>IndiePost group <code>{h(delivery["group_id"])}</code></p>'
                       + form(endpoint+"/refresh", token, f'<input type="hidden" name="intent" value="{delivery["intent"]}">'
                              + button("Refresh status")) if delivery["group_id"] else "")
                    + "</li>")
    local = daily_release.delivery_rows(db,identity)
    for delivery in local:
        title = indiepost.PLATFORM_NAMES[delivery["platform"]]
        status = daily_release.LABELS[delivery["state"]]
        stamp = (datetime.fromtimestamp(delivery["confirmed"],timezone.utc).strftime("%d %b %H:%M UTC")
                 if delivery["confirmed"] else delivery["day"])
        group = f'<p>IndiePost group <code>{h(delivery["group_id"])}</code></p>' if delivery["group_id"] else ""
        rows.append(f'<li><strong>{h(title)} · {h(delivery["account_name"])}</strong>'
                    f' · {status} · {h(delivery["visibility"])} · {stamp}'
                    f'{group}<p>{h(delivery["detail"])}</p></li>')
    controls = ""
    if eligible:
        values, version, targets = indiepost.destinations(db, current["creator_id"])
        if values["local_release"]:
            controls = '<p>Factory-owned daily release uses the <a href="/controls#uploads">local buffer</a>.</p>'
        elif not values["enabled"]:
            controls = '<p>Sending is paused. Update <a href="/controls#uploads">Uploads</a> to enable it.</p>'
        elif not targets:
            controls = '<p>Select connected accounts in <a href="/controls#uploads">Uploads</a> and allow their platforms for this creator.</p>'
        else:
            plan = json.loads(current["plan"])
            title, caption = indiepost.copy(values, plan)
            release = ("Next available slot · "+values["zone"] if values["release"]=="scheduled"
                       else "As soon as IndiePost can publish")
            for target in targets:
                previous = next((d for d in owned if d["account"]==target["id"].casefold()), None)
                if previous and not (previous["state"]=="failed" and not previous["first_submit"]
                                     and previous["edition"]==current["edition"] and previous["account_name"]==target["name"]):
                    continue
                visibility = indiepost.privacy(values, target["platform"])
                name = f'{indiepost.PLATFORM_NAMES[target["platform"]]} · {target["name"]}'
                fields = (f'<input type="hidden" name="edition" value="{h(current["edition"])}">'
                          f'<input type="hidden" name="account" value="{h(target["id"])}">'
                          f'<input type="hidden" name="username" value="{h(target["name"])}">'
                          f'<input type="hidden" name="version" value="{version}">'
                          f'<p>{h(release)}</p>'
                          + (f'<p>Title · {h(title)}</p>' if target["platform"]=="youtube" else "")
                          + (f'<p>Caption · {h(caption)}</p>' if caption else "")
                          + '<label class="toggle"><input type="checkbox" name="confirm" required> '
                            'I approve sending this edition to this account.</label>'
                          + button("Retry delivery" if previous else "Send to IndiePost", "primary"))
                controls += (f'<details><summary>{h(name)} · {h(visibility)}</summary>'
                             + form(endpoint+"/send", token, fields) + "</details>")
    else:
        controls = "<p>IndiePost requires a current qualified portrait with final QA and no release hold.</p>"
    return '<h3>IndiePost</h3>' + ("<ul>"+"".join(rows)+"</ul>" if rows else "") + controls


def row_view(story, versions, prior, receipts, token, length, hold, playable, publishing="", reclaimed=False,
             automatic=False):
    newest = versions[0]
    plan = json.loads(newest["plan"])
    style = plan["render_style"]
    edition = newest["edition"]
    base = f'/streamer/run/{newest["job"]}/archive#t='
    if plan.get("source_windows"):
        base = story["source_url"] + ("&" if "?" in story["source_url"] else "?") + "t="
    local = h(base+f'{story["proof"]:.3f}')
    video = f'/streamer/run/{newest["job"]}/{newest["candidate"]}/edition/{edition}'
    label = status(receipts)
    master = f'{video}/portrait.mp4'
    payoff = (plan.get("beats") or {}).get("payoff",{}).get("source")
    additional = (f' · <a href="{h(base+f"{payoff:.3f}")}">'
                  f'Later payoff at {views.timecode(payoff)}</a>' if payoff and payoff > story["proof"]+5 else "")
    qa = "Final QA recorded" if newest["qa"] else "Automated QC passed" if automatic else "Awaiting final QA"
    release = shorts_release.describe(length)
    status_line = f'<p>{h(release)}</p>' if release else ""
    if hold:
        status_line += f'<p>Editorial hold · {h(hold)}</p>'
    if not length["passed"]:
        status_line += '<p>Release length hold</p>'
    portrait = (f'<video controls playsinline preload="none" poster="{video}/poster.jpg" src="{master}" '
                f'aria-label="{h(plan["hook"])}"></video>' if playable else
                ("<p>Local portrait reclaimed after delivery. Playback links and receipts are below.</p>" if reclaimed
                 else "<p>Current MP4 unavailable</p>"))
    download = f' · <a href="{master}">Open MP4</a>' if playable else ""
    hold_form = form(f'/factory/done/{story["id"]}/hold', token,
                     f'<input type="hidden" name="edition" value="{edition}">'
                     '<label>Editorial hold reason<textarea name="reason" required maxlength="2000"></textarea></label>'
                     +button("Save editorial hold"))
    link = (f'<a href="{h(story["source_url"])}">Original video</a>' if story["source_url"].startswith("https://")
            else f'<span>{h(story["source_url"])}</span>')
    times = " · ".join(f'{views.timecode(c["start"])}–{views.timecode(c["end"])}' for c in plan["cuts"])
    history = "".join(f'<li><a href="/streamer/run/{x["job"]}/{x["candidate"]}/edit?edition={x["edition"]}">'
                      f'{h(json.loads(x["plan"])["title"])}</a> · {h(x["completed"])} UTC · SHA-256 {h(x["digest"])}</li>' for x in versions)
    earlier = "".join(f'<li><a href="/streamer/run/{r["job"]}/{r["candidate"]}/edit">Prior source evidence</a>'
                      f' · {h(r["provenance"])}</li>' for r in prior if r["job"]!=newest["job"])
    rows = []
    for receipt in receipts:
        reference = (f'<a href="{h(receipt["remote_url"])}">{h(receipt["remote_id"])}</a>' if receipt["remote_url"] else
                     h(receipt["remote_id"] or ("See IndiePost receipt" if receipt["group_id"] else "Evidence pending")))
        rows.append(f'<li>{h(receipt["platform"].title())} · {h(receipt["account"])} · '
                    f'{DELIVERY.get(receipt["status"],"Queued")} · {reference}'
                    f'{f" · {h(receipt["occurred"])}" if receipt["occurred"] else ""} · '
                    f'<a href="/streamer/run/{receipt["job"]}/{receipt["candidate"]}/edit?edition={receipt["edition"]}">Recorded edition</a></li>')
    fields = (f'<input type="hidden" name="edition" value="{edition}"><label>Platform<select name="platform">'
              +"".join(f'<option>{value}</option>' for value in PLATFORMS)
              +'</select></label><label>Destination account<input name="account" required maxlength="120"></label>'
              '<label>Post URL or ID<input name="remote" required maxlength="2000"></label>'
              '<label>Published timestamp with timezone offset<input name="occurred" placeholder="2026-11-20T18:30+08:00" required></label>')
    return (f'<article class="panel operator-panel done-row" id="story-{story["id"]}"><div class="done-layout">'
            f'{portrait}'
            f'<div><h2>{h(plan["hook"])}</h2><p>{h(story["creator"])} · {h(plan["context"])}</p>'
            f'<p>{h(story["source_title"])} · {link}</p><p><a href="{local}">Jump to source at {views.timecode(story["proof"])}</a>'
            f'{additional}{download}</p>'
            f'<p><a href="{video.rsplit("/edition/",1)[0]}/edit?edition={edition}">Open current edition in editor</a></p>'
            f'<p>Rendered style · {h(style["name"])} · revision {style["revision"]}</p>'
            f'{status_line}'
            f'<p>Completed {h(story["completed"])} UTC · {qa} · <strong>{label}</strong></p>'
            f'<details><summary>Evidence and edition history</summary><p>Source intervals · {h(times)}</p>'
            f'<p>Archive SHA-256 · {h(plan["asset"])}</p><p>Master · {h(master)}</p><ul>{history}{earlier}</ul></details>'
            f'<details><summary>Destination accounts</summary><ul>{"".join(rows) or "<li>No delivery receipts</li>"}</ul>'
            +publishing
            +form(f'/factory/done/{story["id"]}/manual',token,fields+button("Record external post"))+
            '</details><details><summary>Editorial hold</summary>'+hold_form+'</details></div></div></article>')


def page(db, root, token, query):
    from . import indiepost, portal_settings

    local_release = portal_settings.current(db)[0]["uploads"]["local_release"]
    clause = []
    args = []
    if query.get("attempt","").isdigit():
        clause += ["EXISTS(SELECT 1 FROM done_editions d JOIN streamer_editions e ON e.id=d.edition WHERE e.job=? AND d.story=s.id)"]
        args.append(int(query["attempt"]))
    if query.get("source","").isdigit():
        clause += ["s.source=?"]; args.append(int(query["source"]))
    if query.get("creator"):
        clause += ["s.creator=?"]; args.append(query["creator"])
    stories = db.execute("SELECT * FROM done_stories s"+(" WHERE "+" AND ".join(clause) if clause else "")+
                         " ORDER BY s.proof,s.id",args).fetchall()
    cards, held = [], []
    for story in stories:
        lookup = db.execute("""SELECT d.*,e.job,e.candidate,e.plan,e.qa,e.qualification,e.render_state,
            a.creator AS creator_id,
            (SELECT verdict FROM streamer_edition_feedback f WHERE f.edition=e.id ORDER BY id DESC LIMIT 1) AS verdict
            FROM done_editions d JOIN streamer_editions e ON e.id=d.edition
            JOIN jobs j ON j.id=e.job LEFT JOIN streamer_streams st ON st.source=j.source
            LEFT JOIN streamer_accounts a ON a.id=st.account WHERE d.story=? ORDER BY d.rowid DESC""",(story["id"],)).fetchall()
        if not lookup:
            continue
        prior = db.execute("SELECT * FROM done_matches WHERE story=? ORDER BY job,candidate",(story["id"],)).fetchall()
        receipts = db.execute("""SELECT i.platform,i.account,COALESCE(r.edition,i.edition) edition,
                                 COALESCE(r.status,CASE WHEN p.state IN ('pending','processing')
                                                            OR q.state IN ('scheduled','processing') THEN 'scheduled'
                                                       WHEN p.state='private_posted' THEN 'published'
                                                       WHEN q.state='confirmed' THEN 'published'
                                                       WHEN p.state='failed' OR q.state='failed' THEN 'failed'
                                                       WHEN p.state='unknown' OR q.state='unknown' THEN 'unknown' END) status,
                                 r.remote_id,r.remote_url,COALESCE(p.group_id,q.group_id) AS group_id,
                                 r.occurred,e.job,e.candidate FROM done_delivery_intents i LEFT JOIN done_delivery_receipts r ON r.intent=i.id
                                 LEFT JOIN indiepost_deliveries p ON p.intent=i.id
                                 LEFT JOIN factory_release_destinations q ON q.intent=i.id
                                 JOIN streamer_editions e ON e.id=COALESCE(r.edition,i.edition)
                                 WHERE i.story=? ORDER BY i.id,r.id""",(story["id"],)).fetchall()
        if query.get("publication") in ("",None,status(receipts)):
            current = lookup[0]
            path = shorts_release.portrait_path(root,current,current["edition"])
            reclaimed = db.execute("""SELECT 1 FROM factory_release_artifacts WHERE edition=?
                                     AND state='removed' AND path LIKE '%/portrait.mp4'""",
                                   (current["edition"],)).fetchone() is not None
            plan = json.loads(current["plan"])
            automatic = local_release and indiepost.factory_verified(plan)
            bounds = shorts_release.bounds_for(plan) if local_release else shorts_release.RELEASE_BOUNDS
            if reclaimed:
                length = {"passed":True,"issue":""}
            elif automatic:
                try:
                    qc = streamer_asr.read(path.with_name("qc.json"))
                except (OSError,ValueError):
                    qc = {}
                if qc.get("passed") and shorts_release.matches_master(path,current["digest"]):
                    length = qc.get("release_length") or {"passed":True,"issue":""}
                else:
                    length = shorts_release.inspect(path,bounds=bounds)
            else:
                length = shorts_release.inspect(path,bounds=bounds)
            reason = shorts_release.hold(db,story["id"],current["edition"])
            target = cards if length["passed"] and not reason else held
            eligible = (not reclaimed and length["passed"] and not reason and current["creator_id"]
                        and (current["qa"] or automatic) and current["qualification"]=="qualified"
                        and current["render_state"]=="exported"
                        and current["verdict"] in (None,"accepted"))
            publishing = indiepost_panel(db,story,current,token,eligible)
            target.append(row_view(story,lookup,prior,receipts,token,length,reason,path.is_file(),publishing,reclaimed,
                                   automatic=automatic))
    values = [(x[0],x[0]) for x in db.execute("SELECT DISTINCT creator FROM done_stories ORDER BY creator")]
    source_rows = [(str(x[0]),x[1]) for x in db.execute("SELECT DISTINCT source,source_title FROM done_stories ORDER BY source_title")]
    jobs = [(str(x[0]),f'Attempt {x[0]}') for x in db.execute("SELECT DISTINCT e.job FROM done_editions d JOIN streamer_editions e ON e.id=d.edition ORDER BY e.job DESC")]
    statuses = [(x,x) for x in ("No recorded publication","Scheduled","Recorded manually","Partially published","Published")]
    filters = '<input type="hidden" name="work" value="done">'
    for key,label,choices in (("creator","Creator",values),("source","Source",source_rows),("attempt","Attempt",jobs),("publication","Publication",statuses)):
        options = "".join(f'<option value="{h(v)}"{" selected" if query.get(key)==v else ""}>{h(name)}</option>' for v,name in choices)
        filters += f'<label>{label}<select name="{key}"><option value="">All</option>{options}</select></label>'
    return (f'<h1>Done</h1><form class="workspace-filters" action="/factory" method="get">{filters}'
            f'<button>Filter</button></form><p>{views.count_label(len(cards),"finished story") if len(cards)==1 else str(len(cards))+" finished stories"}</p>'
            +("".join(cards) or "<p>No eligible finished stories match these filters.</p>")
            +(f'<h2>Release holds ({len(held)})</h2>'+"".join(held) if held else ""))
