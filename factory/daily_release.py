"""Factory-owned story reservations and immediate IndiePost dispatch."""

import hashlib
import json
import logging
import secrets
import shutil
import time
from datetime import datetime, time as wall_time, timedelta, timezone

from . import factory_done, factory_portal, indiepost, media, portal_settings, provider_api, reclamation, shorts_release, source_queue, store

LABELS = {
    "local": "Held locally", "uploading": "Uploading", "creating": "Submitting",
    "scheduled": "Accepted", "processing": "Processing", "confirmed": "Posted",
    "retry": "Waiting to retry", "failed": "Failed", "unknown": "Needs reconciliation",
}

class ReleasePaused(ValueError):
    pass


def local_clock(values, now):
    return datetime.fromtimestamp(now, factory_portal.tz(values["zone"]))


def window_open(values, now):
    if values["release"] == "immediate":
        return True
    current = local_clock(values,now).time()
    start,end = (wall_time.fromisoformat(values[key]) for key in ("window_start","window_end"))
    return start==end or (start<=current<end if start<end else current>=start or current<end)


def day_bounds(values, now):
    wall = local_clock(values,now)
    start = datetime.combine(wall.date(),wall_time.min,wall.tzinfo)
    return start.timestamp(), (start+timedelta(days=1)).timestamp()


def eligible(db, root):
    latest = db.execute("""SELECT d.story,d.edition,ds.source FROM done_editions d
                         JOIN done_stories ds ON ds.id=d.story JOIN streamer_editions e ON e.id=d.edition
                         WHERE d.rowid=(SELECT MAX(n.rowid) FROM done_editions n WHERE n.story=d.story)
                         AND e.qualification='qualified' AND e.render_state='exported'
                         AND (e.qa IS NOT NULL OR (json_extract(e.plan,'$.story_v4')=1
                              AND json_extract(e.plan,'$.owner_viewing')=1))
                         AND NOT EXISTS (SELECT 1 FROM factory_release_stories r WHERE r.story=d.story)
                         AND NOT EXISTS (SELECT 1 FROM done_delivery_intents i WHERE i.story=d.story)
                         ORDER BY ds.completed,ds.id""")
    for item in latest:
        try:
            edition = indiepost.release_edition(db,root,item["story"],item["edition"],automatic=True)
        except (ValueError,media.MediaError):
            continue
        _,_,accounts = indiepost.destinations(db,edition["creator"])
        if accounts:
            yield item,edition,accounts


def ready(db, root):
    yield from eligible(db,root)
    reserved = db.execute("""SELECT r.story,r.edition,d.source FROM factory_release_stories r
                           JOIN done_stories d ON d.id=r.story
                           WHERE r.submitted IS NULL AND EXISTS (SELECT 1 FROM done_delivery_intents i
                             JOIN factory_release_destinations q ON q.intent=i.id WHERE i.story=r.story)
                           AND NOT EXISTS (SELECT 1 FROM done_delivery_intents i
                             JOIN factory_release_destinations q ON q.intent=i.id
                             WHERE i.story=r.story AND q.state!='local')""")
    for item in reserved:
        try:
            edition = indiepost.release_edition(db,root,item["story"],item["edition"],automatic=True)
        except (ValueError,media.MediaError):
            continue
        _,_,accounts = indiepost.destinations(db,edition["creator"])
        expected = {(row["account"],row["account_name"]) for row in delivery_rows(db,item["story"])}
        if expected <= {(account["id"].casefold(),account["name"]) for account in accounts}:
            yield item,edition,accounts


def visibility(body, platform):
    if platform=="youtube":
        return body[platform]["privacyStatus"]
    if platform=="tiktok":
        return body[platform]["privacyLevel"]
    return "reel"


def plan(db, root, values, now):
    day = local_clock(values,now).date().isoformat()
    db.execute("INSERT OR IGNORE INTO factory_release_days VALUES(?,?,?)",
               (day,values["zone"],values["daily_stories"]))
    current = db.execute("SELECT * FROM factory_release_days WHERE day=?",(day,)).fetchone()
    if current["zone"]!=values["zone"]:
        return day,0
    used = db.execute("SELECT COUNT(*) FROM factory_release_stories WHERE day=?",(day,)).fetchone()[0]
    available = max(0,min(current["target"],values["daily_stories"])-used)
    if not available:
        return day,0
    rolled = db.execute("""SELECT r.story FROM factory_release_stories r WHERE r.day<? AND r.submitted IS NULL
                        AND EXISTS (SELECT 1 FROM done_delivery_intents i JOIN factory_release_destinations d ON d.intent=i.id
                                    WHERE i.story=r.story AND d.state IN ('local','uploading','retry'))
                        ORDER BY r.day,r.created,r.story LIMIT ?""",(day,available)).fetchall()
    db.executemany("UPDATE factory_release_stories SET day=? WHERE story=?",[(day,row["story"]) for row in rolled])
    added = len(rolled)
    for item,edition,accounts in eligible(db,root):
        if added>=available:
            break
        story = item["story"]
        db.execute("INSERT INTO factory_release_stories(story,edition,day,created) VALUES(?,?,?,?)",
                   (story,item["edition"],day,now))
        reclamation.own_portrait(db,root,story,edition,now)
        edit = json.loads(edition["plan"])
        immediate = dict(values,release="immediate")
        for account in accounts:
            platform = account["platform"]
            intent = factory_done.account_intent(db,story,item["edition"],platform,account["id"])
            body = indiepost.payload(immediate,account,edit,None)
            db.execute("""INSERT INTO factory_release_destinations(intent,account_name,workspace,visibility,payload,request_id)
                        VALUES(?,?,?,?,?,?)""",(intent["id"],account["name"],account["workspace"],visibility(body,platform),
                                               json.dumps(body),"gta-factory-local-"+secrets.token_hex(24)))
        added += 1
    return day,added


def delivery_rows(db, story):
    return db.execute("""SELECT d.*,i.story,i.edition,i.platform,i.account,s.day FROM factory_release_destinations d
                        JOIN done_delivery_intents i ON i.id=d.intent JOIN factory_release_stories s ON s.story=i.story
                        WHERE i.story=? ORDER BY i.id""",(story,)).fetchall()


def time_limit(db, values, account, now, exclude=0):
    start,end = day_bounds(values,now)
    sent = db.execute("""SELECT COUNT(*),MAX(d.attempted) FROM factory_release_destinations d
                      JOIN done_delivery_intents i ON i.id=d.intent WHERE i.account=?
                      AND d.intent<>? AND d.attempted>=? AND d.attempted<?""",(account,exclude,start,end)).fetchone()
    legacy = db.execute("""SELECT COUNT(*),MAX(unixepoch(d.slot)) FROM indiepost_deliveries d
                         JOIN done_delivery_intents i ON i.id=d.intent WHERE i.account=? AND d.first_submit IS NOT NULL
                         AND unixepoch(d.slot)>=? AND unixepoch(d.slot)<?""",(account,start,end)).fetchone()
    if sent[0]+legacy[0]>=values["account_daily"]:
        return "Destination daily limit reached."
    # Spacing also spans midnight.
    last = db.execute("""SELECT MAX(d.attempted) FROM factory_release_destinations d
                      JOIN done_delivery_intents i ON i.id=d.intent WHERE i.account=? AND d.intent<>?""",
                      (account,exclude)).fetchone()[0]
    if max(last or 0,legacy[1] or 0)+values["spacing"]*60>now:
        return "Destination spacing interval."
    groups = db.execute("""SELECT (SELECT COUNT(*) FROM factory_release_destinations WHERE intent<>?
                                                    AND attempted>=? AND attempted<?)
                        +(SELECT COUNT(*) FROM indiepost_deliveries
                          WHERE unixepoch(first_submit)>=? AND unixepoch(first_submit)<?)""",
                        (exclude,start,end,start,end)).fetchone()[0]
    if groups>=values["provider_daily"]:
        return "Configured IndiePost group limit reached."
    return ""


def release_spacing(db, values, story, now):
    submitted = db.execute("SELECT submitted FROM factory_release_stories WHERE story=?",(story,)).fetchone()[0]
    if submitted is not None:
        return ""
    latest = db.execute("SELECT MAX(submitted) FROM factory_release_stories").fetchone()[0]
    if latest is not None and latest+values["spacing"]*60>now:
        return "Waiting for the next reel release slot."
    return ""


def claim(db, values, now):
    day = local_clock(values,now).date().isoformat()
    if not window_open(values,now):
        return None,"Outside the local publishing window."
    if db.execute("SELECT 1 FROM factory_release_days WHERE day=? AND zone<>?",(day,values["zone"])).fetchone():
        return None,"Timezone changed. Reservations reopen on the next local date."
    waiting = db.execute("""SELECT d.*,i.story,i.edition,i.platform,i.account FROM factory_release_destinations d
                          JOIN done_delivery_intents i ON i.id=d.intent
                          JOIN factory_release_stories r ON r.story=i.story
                          WHERE d.state IN ('local','retry') AND d.check_after<=?
                          AND (r.day=? OR r.submitted IS NOT NULL)
                          ORDER BY r.day,r.created,r.story,i.id""",(now,day)).fetchall()
    reason = ""
    for row in waiting:
        busy = db.execute("""SELECT 1 FROM factory_release_destinations d JOIN done_delivery_intents i ON i.id=d.intent
                           WHERE i.account=? AND d.state IN ('uploading','creating')""",(row["account"],)).fetchone()
        if busy:
            continue
        blocked = (release_spacing(db,values,row["story"],now) or
                   time_limit(db,values,row["account"],now,exclude=row["intent"]))
        if blocked:
            reason = reason or blocked
            db.execute("UPDATE factory_release_destinations SET detail=? WHERE intent=?",(blocked,row["intent"]))
            continue
        db.execute("UPDATE factory_release_destinations SET state='uploading',detail='' WHERE intent=?",(row["intent"],))
        return row,""
    return None,reason


def update(root, row, state, detail="", *, check_after=0, group=None):
    with store.connect(root,True) as db:
        db.execute("""UPDATE factory_release_destinations SET state=?,detail=?,check_after=?,group_id=COALESCE(?,group_id)
                    WHERE intent=? AND state NOT IN ('confirmed','failed')""",
                   (state,detail,check_after,group,row["intent"]))


def verify_target(db, root, row):
    values = portal_settings.current(db)[0]["uploads"]
    if not values["enabled"] or not values["local_release"]:
        raise ReleasePaused("Local publishing is paused. The saved delivery will resume when enabled.")
    edition = indiepost.release_edition(db,root,row["story"],row["edition"],automatic=True)
    _,_,accounts = indiepost.destinations(db,edition["creator"])
    account = next((a for a in accounts if a["id"].casefold()==row["account"] and a["name"]==row["account_name"]
                    and a["workspace"]==row["workspace"]),None)
    if not account:
        raise ValueError("The reserved destination is no longer permitted.")
    if db.execute("""SELECT 1 FROM done_delivery_intents i JOIN done_delivery_receipts r ON r.intent=i.id
                    WHERE i.story=? AND i.platform=? AND i.account IN (?,?,?)
                    AND r.status IN ('manual','published','scheduled')""",
                  (row["story"],row["platform"],row["account"],row["account_name"].casefold(),
                   "@"+row["account_name"].lstrip("@").casefold())).fetchone():
        raise ValueError("An external receipt already exists for this destination. Reconcile it before sending.")
    return values,account,edition


def dispatch(root, row, client, *, now=time.time):
    started = False
    uploaded = row["media_id"]
    try:
        with store.connect(root) as db:
            _,account,edition = verify_target(db,root,row)
        if account not in client.discover():
            raise ValueError("The account is no longer connected in the reserved workspace.")
        body = json.loads(row["payload"])
        ready_id = row["media_id"]
        if not ready_id:
            path = shorts_release.portrait_path(root,edition)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest()!=edition["digest"]:
                    raise ValueError("The portrait no longer matches final QA.")
                size = stream.tell()
                stream.seek(0)
                ready_id,upload = client.init_upload(f"gta-factory-short-{edition['digest'][:24]}.mp4",size)
                if upload:
                    client.put(upload,stream,size)
            client.confirm(ready_id)
            uploaded = ready_id
        body["mediaIds"] = [ready_id]
        if account not in client.discover():
            raise ValueError("The reserved account changed during the upload.")
        with store.connect(root,True) as db:
            stamp = now()
            values,_,_ = verify_target(db,root,row)
            db.execute("UPDATE factory_release_destinations SET payload=?,media_id=? WHERE intent=?",
                       (json.dumps(body),ready_id,row["intent"]))
            if not window_open(values,stamp):
                db.execute("UPDATE factory_release_destinations SET state='local',detail=? WHERE intent=?",
                           ("The local publishing window closed during upload.",row["intent"]))
                return
            blocked = (release_spacing(db,values,row["story"],stamp) or
                       time_limit(db,values,row["account"],stamp,exclude=row["intent"]))
            if blocked:
                db.execute("UPDATE factory_release_destinations SET state='local',detail=? WHERE intent=?",
                           (blocked,row["intent"]))
                return
            day = local_clock(values,stamp).date().isoformat()
            reservation = db.execute("SELECT day,submitted FROM factory_release_stories WHERE story=?",
                                     (row["story"],)).fetchone()
            if reservation["submitted"] is None and reservation["day"]!=day:
                db.execute("UPDATE factory_release_destinations SET state='local' WHERE intent=?",(row["intent"],))
                return
            db.execute("""UPDATE factory_release_destinations SET state='creating',attempts=attempts+1,
                          attempted=? WHERE intent=? AND state='uploading'""",(stamp,row["intent"]))
            db.execute("UPDATE factory_release_stories SET submitted=COALESCE(submitted,?) WHERE story=?",(stamp,row["story"]))
        started = True
        group = client.create(body,row["request_id"])
    except ReleasePaused as error:
        with store.connect(root,True) as db:
            db.execute("UPDATE factory_release_destinations SET state='local',detail=?,media_id=? WHERE intent=?",
                       (str(error),uploaded,row["intent"]))
        return
    except provider_api.ProviderError as error:
        if started and error.status==429 and row["attempts"]<2:
            update(root,row,"retry","IndiePost rate limit.",check_after=now()+max(60,error.retry or 0))
        elif started and (error.status is None or error.status>=500 or error.status in (408,409)):
            update(root,row,"unknown","IndiePost create may have succeeded. Review its Calendar before any replay.")
        else:
            update(root,row,"failed",str(error))
        return
    except (ValueError,OSError,media.MediaError) as error:
        update(root,row,"unknown" if started else "failed",
               "IndiePost create receipt was indeterminate." if started else
               str(error) if not isinstance(error,OSError) else "The local portrait could not be read.")
        return
    update(root,row,"scheduled","IndiePost accepted the immediate submission.",group=group,check_after=now()+30)


def verified_receipt(row, result):
    platform = row["platform"]
    if result.get("status")!="posted":
        return False
    if platform in ("youtube","tiktok"):
        reported = result.get(platform)
        field = "privacyStatus" if platform=="youtube" else "privacyLevel"
        return isinstance(reported,dict) and reported.get(field)==row["visibility"]
    return result.get("format")=="reel" and row["visibility"]=="reel"


def reconcile(root, row, client, now):
    try:
        result = client.status(row["group_id"],row["platform"])
    except (ValueError,OSError):
        update(root,row,row["state"],"IndiePost status is temporarily unavailable.",check_after=now+300)
        return
    state = result.get("status")
    if state in ("pending","scheduled","publishing","processing"):
        update(root,row,"processing" if state in ("publishing","processing") else "scheduled",check_after=now+60)
        return
    if state=="failed":
        update(root,row,"failed","IndiePost reported that this destination failed.")
        return
    external = result.get("externalPostId")
    try:
        stamp = datetime.fromisoformat(result.get("postedAt") or "")
        if stamp.tzinfo is None or not verified_receipt(row,result) or (not external and row["visibility"]!="SELF_ONLY"):
            raise ValueError()
        with store.connect(root,True) as db:
            if external:
                remote = indiepost.identifier(external)
                if row["platform"]=="youtube":
                    remote = f"https://www.youtube.com/watch?v={remote}"
                factory_done.record(db,row["story"],row["edition"],row["platform"],row["account"],"published",remote,
                                    stamp.isoformat(),f'IndiePost group {row["group_id"]} · {row["visibility"]}')
            db.execute("""UPDATE factory_release_destinations SET state='confirmed',detail=?,confirmed=?
                          WHERE intent=? AND state NOT IN ('confirmed','failed')""",
                       ("IndiePost posted at "+stamp.astimezone(timezone.utc).strftime("%d %b %H:%M UTC"),
                        max(now,stamp.timestamp()),row["intent"]))
    except (ValueError,TypeError):
        update(root,row,"unknown","Platform confirmation or intended visibility could not be verified.",check_after=now+900)


def recover(root):
    with store.connect(root,True) as db:
        db.execute("UPDATE factory_release_destinations SET state='local' WHERE state='uploading'")
        db.execute("""UPDATE factory_release_destinations SET state='unknown',
                      detail='Worker restarted after creating this post may have succeeded. Check IndiePost.'
                      WHERE state='creating'""")


def buffer_count(db, root):
    rows = db.execute("""SELECT e.id,e.job,e.candidate FROM done_editions d
        JOIN streamer_editions e ON e.id=d.edition
        WHERE d.rowid=(SELECT MAX(n.rowid) FROM done_editions n WHERE n.story=d.story)
          AND e.qualification='qualified' AND e.render_state='exported'
          AND NOT EXISTS (SELECT 1 FROM done_release_holds h WHERE h.edition=e.id)
          AND NOT EXISTS (SELECT 1 FROM factory_release_artifacts a WHERE a.edition=e.id
                          AND a.state IN ('deleting','removed') AND a.path LIKE '%/portrait.mp4')
          AND NOT EXISTS (SELECT 1 FROM factory_release_stories s WHERE s.story=d.story AND s.submitted IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM done_delivery_intents i WHERE i.story=d.story AND
              (EXISTS (SELECT 1 FROM done_delivery_receipts r WHERE r.intent=i.id AND r.status IN ('published','manual'))
               OR EXISTS (SELECT 1 FROM factory_release_destinations f WHERE f.intent=i.id AND f.group_id IS NOT NULL)
               OR EXISTS (SELECT 1 FROM indiepost_deliveries p WHERE p.intent=i.id AND p.group_id IS NOT NULL)))""")
    return sum(shorts_release.portrait_path(root,row).is_file() for row in rows)


def next_hour(db, root):
    from . import operator_workspace, streamer_transfer
    sources = sorted(operator_workspace.listing(db),key=lambda row:(row["priority"],row["id"]))
    partial,new = [],[]
    for source in sources:
        ready = source["readiness"]=="ready" or (
            source["readiness"] is None and source["live_status"] in ("was_live","archive_upload"))
        if (source["state"]!="queued" or not source["enabled"] or source["location"] or not ready or
                not source["link"].startswith("https://www.youtube.com/")):
            continue
        jobs = [j for j in operator_workspace.attempts(db,source["id"]) if j["revision"]==source["revision"]]
        if not jobs:
            new.append((source["id"],0))
            continue
        duration = source["duration"]
        if not duration:
            continue
        spans = [(j["state"],*operator_workspace.span(root,j,duration)) for j in jobs]
        for start,end in streamer_transfer.replay_hours(duration):
            if any(state=="complete" and left<=start and right>=end for state,left,right in spans):
                continue
            if not any(left<end and right>start for _,left,right in spans):
                partial.append((source["id"],start))
            break
    return partial+new


def refill(db, root, config, now):
    from . import factory_events, readiness
    values = config["uploads"]
    if not values["refill"]:
        return
    active = db.execute("SELECT active FROM factory_release_refill").fetchone()[0]
    size = buffer_count(db,root)
    active = size<values["refill_low"] or (active and size<values["refill_high"])
    resolve_pending = False
    running = db.execute("SELECT id FROM jobs WHERE state IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
    if running:
        detail = f"Run {running['id']} is in progress. The next hour is checked when it finishes."
    elif not active:
        detail = "Waiting for the buffer to fall below the minimum."
    elif not config["connections"]["hosted"]:
        detail = "Awaiting default Sail consent in Controls."
    elif config["resources"]["paused"]:
        detail = "New source processing is paused in Controls."
    elif config["resources"]["remote_minutes"]<60:
        detail = "Allow at least 60 minutes of audio coverage in Controls."
    elif shutil.disk_usage(root).free<config["resources"]["reserve_mib"]*media.CHUNK:
        detail = "The free-space reserve prevents a new hour."
    elif pressure := readiness.admission_hold():
        detail = pressure
    else:
        awaiting_replay = db.execute("""SELECT s.id,q.detail FROM source_queue q JOIN sources s ON s.id=q.source
            WHERE q.platform='youtube' AND q.readiness IN ('unresolved','waiting') AND s.state='queued'
            ORDER BY q.failures DESC,s.priority,s.id LIMIT 1""").fetchone()
        detail = "Waiting for a source with an unprocessed hour."
        if awaiting_replay:
            message = awaiting_replay["detail"] or "Waiting for a seekable YouTube replay."
            detail = f'Source {awaiting_replay["id"]} · {message}'
        resolve_pending = True
        for source,part in next_hour(db,root):
            try:
                job,created = portal_settings.start(db,source,dict(coverage_start=str(part),coverage_minutes="60",
                    directory_part=part),datetime.fromtimestamp(now,timezone.utc),buffer_admission=True)
            except ValueError as error:
                detail = str(error)
                continue
            if created:
                factory_events.record(db,job,"queued",item=f"Buffer scheduler admitted source {source}, part {part//3600+1}")
                detail = f"Queued run {job} for source {source}, part {part//3600+1}."
                resolve_pending = False
                break
    db.execute("UPDATE factory_release_refill SET active=?,detail=?",(bool(active),detail))
    return resolve_pending


def tick(root, vault, *, clock=time.time, client_type=indiepost.Client):
    now = clock()
    with store.connect(root,True) as db:
        config,_ = portal_settings.current(db)
        values = config["uploads"]
        reconcile_row = db.execute("""SELECT d.*,i.story,i.edition,i.platform,i.account
                    FROM factory_release_destinations d JOIN done_delivery_intents i ON i.id=d.intent
                    WHERE d.group_id IS NOT NULL AND d.state IN ('scheduled','processing','unknown')
                    AND d.check_after<=? ORDER BY d.check_after,i.id LIMIT 1""",(now,)).fetchone()
        reference = db.execute("SELECT reference FROM indiepost_connection").fetchone()[0]
        if values["local_release"]:
            plan(db,root,values,now)
        resolve_pending = refill(db,root,config,now)
        enabled = values["enabled"] and values["local_release"]
    reclamation.recover_manual_one(root,now)
    if values["reclaim"]:
        reclamation.reclaim_one(root,now,values["grace_hours"])
    if resolve_pending:
        source_queue.resolve_pending(root,now,config["resources"]["remote_height"])
    if not reference or not (reconcile_row or enabled):
        return
    key = vault.get(reference)
    if not key:
        return
    client = client_type(key)
    if reconcile_row:
        reconcile(root,reconcile_row,client,now)
    if enabled:
        with store.connect(root,True) as db:
            values = portal_settings.current(db)[0]["uploads"]
            row,_ = claim(db,values,now) if values["enabled"] and values["local_release"] else (None,"")
        if row:
            dispatch(root,row,client,now=clock)


def run(root, vault, shutdown):
    while not shutdown.is_set():
        try:
            tick(root,vault)
        except Exception as error:
            logging.error("Factory release tick failed with %s",type(error).__name__)
        shutdown.wait(5)


def counts(db, day):
    return db.execute("""SELECT COUNT(*) AS reserved,
           COALESCE(SUM(r.submitted IS NOT NULL),0) AS sent,
           COALESCE(SUM(NOT EXISTS (
               SELECT 1 FROM done_delivery_intents i JOIN factory_release_destinations d ON d.intent=i.id
               WHERE i.story=r.story AND d.state!='confirmed')),0) AS complete
           FROM factory_release_stories r WHERE day=?""",(day,)).fetchone()


def publication_counts(db, values, now):
    local = local_clock(values,now)
    today = local.replace(hour=0,minute=0,second=0,microsecond=0)
    month = today.replace(day=1)
    return db.execute("""WITH receipts AS (
        SELECT intent,MIN(unixepoch(occurred)) AS posted
        FROM done_delivery_receipts WHERE status IN ('published','manual') GROUP BY intent
    ), deliveries AS (
        SELECT i.story,COALESCE(r.posted,
            CASE WHEN f.state='confirmed' THEN f.confirmed END,
            CASE WHEN p.state='private_posted' THEN unixepoch(p.updated) END) AS posted
        FROM done_delivery_intents i
        LEFT JOIN receipts r ON r.intent=i.id
        LEFT JOIN factory_release_destinations f ON f.intent=i.id
        LEFT JOIN indiepost_deliveries p ON p.intent=i.id
    )
    SELECT COUNT(DISTINCT story) AS month_reels,COUNT(*) AS month_posts,
           COUNT(DISTINCT story) FILTER (WHERE posted>=?) AS today_reels,
           COUNT(*) FILTER (WHERE posted>=?) AS today_posts
    FROM deliveries WHERE posted>=? AND posted<=?""",
    (today.timestamp(),today.timestamp(),month.timestamp(),now)).fetchone()


def dashboard(db,root,values,now=None):
    from .views import h

    now = time.time() if now is None else now
    clock = local_clock(values,now)
    day = clock.date().isoformat()
    current = counts(db,day)
    buffered = sum(1 for _ in ready(db,root))
    refill_state = db.execute("SELECT * FROM factory_release_refill").fetchone()
    reclaimed = db.execute("SELECT COALESCE(SUM(bytes),0) FROM factory_release_artifacts WHERE state='removed'").fetchone()[0]
    waiting = next(iter(portal_settings.eligible(db)),None)
    next_source = f'Next in queue · {h(waiting["title"])}.' if waiting else "The source queue has no ready links."
    reason = db.execute("""SELECT detail FROM factory_release_destinations
                          WHERE state IN ('local','retry','unknown') AND detail<>'' ORDER BY state LIMIT 1""").fetchone()
    policy = "Active" if values["local_release"] else "Off"
    refill_label = ("Active" if refill_state["active"] else "Waiting") if values["refill"] else "Off"
    return (f'<div class="workspace-inset"><h3>Daily release buffer</h3><p>Local policy · {policy}'
            f' · {buffered} ready stories</p><p>{current["reserved"]} of {values["daily_stories"]} reserved today'
            f' · {current["sent"]} dispatched · {current["complete"]} fully posted'
            f' · Next reset {(clock.date()+timedelta(days=1)).strftime("%d %b")} 00:00 {h(values["zone"])}</p>'
            f'<p>Refill · {refill_label}. {h(refill_state["detail"])} {next_source}</p>'
            f'<p>Posted portrait storage reclaimed · {reclaimed:,} bytes</p>'
            '<p>Saving links stays available at every buffer level.</p>'
            +(f'<p>{h(reason[0])}</p>' if reason else "")+'</div>')
