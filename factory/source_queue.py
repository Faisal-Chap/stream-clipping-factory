"""Durable pasted links, replay readiness and source-level inventory."""

import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import media, portal_settings, store, streamer_catalog, streamer_transfer, views
from .library import identify

REPLAY_STATES = ("is_live", "is_upcoming", "post_live")
PROVENANCE = {"paste": "Pasted stream link", "attachment": "Creator attachment", "edit": "Replaced stream link"}
ADAPTERS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "file": "Local recording"}


def link(value):
    _, canonical, route = identify(value)
    if route == "youtube":
        return canonical, route
    url = urlsplit(canonical)
    if url.scheme == "https" and url.netloc in ("twitch.tv", "www.twitch.tv", "kick.com", "www.kick.com"):
        platform = "twitch" if url.hostname.endswith("twitch.tv") else "kick"
        pathname = url.path.rstrip("/")
        pattern = (r"/(?:videos/[0-9]+|[a-zA-Z0-9_]{2,65})" if platform == "twitch"
                   else r"/[a-zA-Z0-9_-]{2,65}(?:/videos/[a-zA-Z0-9-]{1,65})?")
        if re.fullmatch(pattern, pathname):
            if platform == "twitch" and not pathname.startswith("/videos/") or platform == "kick":
                channel, *rest = pathname.strip("/").split("/")
                pathname = "/" + "/".join([channel.casefold(), *rest])
            return f"https://www.twitch.tv{pathname}" if platform == "twitch" else f"https://kick.com{pathname}", platform
    raise ValueError("Paste a YouTube video, Twitch channel or VOD, or Kick channel or VOD link.")


def resolve(value, allow_upload, height, *, root=None):
    canonical, platform = link(value)
    if platform != "youtube":
        return canonical, platform, None, "unsupported"
    video = identify(canonical)[0].split(":")[1]
    from . import flashdata
    from . import flashdata_profiles
    configured = flashdata.youtube_configuration(root) if root is not None else None
    data = (flashdata_profiles.source(root,configured,video) if configured else
            streamer_catalog.extract(canonical, "--no-playlist", "--skip-download", "-J"))
    if data.get("id") != video or not streamer_catalog.CHANNEL.fullmatch(data.get("channel_id") or ""):
        raise ValueError("YouTube did not verify the video and channel IDs.")
    archive = allow_upload and data.get("live_status") == "not_live" and (data.get("duration") or 0) >= 1200
    if data.get("live_status") not in (*REPLAY_STATES, "was_live") and not archive:
        raise ValueError("Use a full broadcast or explicitly identify a full-stream archive upload.")
    if configured:
        with store.connect(root) as db:
            policy = flashdata_profiles.settings(db)
        ready = (data.get("duration") and data["live_status"] not in REPLAY_STATES and not data.get("stale")
                 and flashdata_profiles.rank(data,"audio",policy) and flashdata_profiles.rank(data,"hd",policy))
        return canonical,platform,data,("ready" if ready else "waiting")
    try:
        available = streamer_transfer.replay_renditions(data, height)
        status = "ready" if available["audio"] and available["video"] else "waiting"
    except streamer_transfer.TransferHold:
        status = "waiting"
    return canonical, platform, data, status


def register(db, source, platform, origin, readiness, previous=None):
    message = {"ready": "", "waiting": "Waiting for a seekable replay.",
               "unsupported": "Link retained. This platform needs a local recording or a verified replay adapter."}[readiness]
    db.execute("""INSERT INTO source_queue(source,previous,platform,provenance,readiness,detail,next_check)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET readiness=excluded.readiness,
                  detail=excluded.detail,next_check=excluded.next_check,failures=0
                  WHERE source_queue.readiness!='ready' OR excluded.readiness='ready'""",
               (source,previous,platform,PROVENANCE[origin],readiness,message,
                time.time()+120 if readiness=="waiting" else 0))


def attach(db, fields, resolved):
    canonical, platform, info, readiness = resolved
    previous = int(fields["previous"]) if fields.get("previous") else None
    if previous and not db.execute("SELECT 1 FROM source_queue WHERE source=?", (previous,)).fetchone():
        raise ValueError("The original queue entry is unavailable.")
    if info:
        owner = db.execute("SELECT id,creator FROM streamer_accounts WHERE platform='youtube' AND platform_id=?",
                           (info["channel_id"],)).fetchone()
        if owner:
            creator, account = owner["creator"], owner["id"]
        else:
            creator = streamer_catalog.save_creator(db,fields,info["channel_id"])
            account = streamer_catalog.roster(db,creator)["account"]
        source = streamer_catalog.attach(db,creator,dict(fields,link=canonical,account=account),info)
        db.execute("""INSERT INTO portal_source_metadata VALUES(?,?) ON CONFLICT(source)
                      DO UPDATE SET duration=COALESCE(excluded.duration,duration)""", (source,info.get("duration")))
    else:
        creator = None
        source,_ = store.save_source(db,{"link":canonical})
    register(db,source,platform,"edit" if previous else "paste",readiness,previous if previous != source else None)
    if previous and previous != source:
        store.set_source_state(db, previous, "archived")
    return source,creator


def resolve_pending(root, now, height):
    with store.connect(root,True) as db:
        row = db.execute("""SELECT s.id AS source,COALESCE(q.failures,0) AS failures,s.link,s.revision
            FROM sources s LEFT JOIN source_queue q ON q.source=s.id
            WHERE s.state='queued' AND s.identity LIKE 'youtube:%'
              AND ((q.readiness='unresolved' AND q.next_check<=?) OR
                   (q.source IS NULL AND NOT EXISTS (SELECT 1 FROM streamer_streams st WHERE st.source=s.id)))
            ORDER BY s.priority,s.id LIMIT 1""",(now,)).fetchone()
        if row:
            db.execute("""INSERT OR IGNORE INTO source_queue(source,platform,provenance,readiness)
                          VALUES(?,'youtube','Existing saved link','unresolved')""",(row["source"],))
    if not row:
        return
    try:
        resolved = resolve(row["link"],True,height,root=root)
    except (ValueError,OSError,media.MediaError) as error:
        delay = min(3600,120*2**min(row["failures"],5))
        detail = str(error)
        if not detail.startswith(("YouTube ","FlashData ")):
            detail = "Waiting to verify a seekable YouTube replay."
        with store.connect(root,True) as db:
            db.execute("""UPDATE source_queue SET next_check=?,failures=failures+1,
                          detail=? WHERE source=? AND readiness='unresolved'""",(now+delay,detail,row["source"]))
        return
    info = resolved[2]
    name = (info.get("channel") or info.get("uploader") or info["channel_id"]).strip()[:100]
    with store.connect(root,True) as db:
        if db.execute("""SELECT 1 FROM sources s JOIN source_queue q ON q.source=s.id
                         WHERE s.id=? AND s.revision=? AND s.state='queued'
                         AND q.readiness='unresolved'""",(row["source"],row["revision"])).fetchone():
            attach(db,{"name":name,"game":"Gaming","archive_upload":"on"},resolved)


def check_one(root, *, now=None, resolve_replay=None):
    now = time.time() if now is None else now
    with store.connect(root) as db:
        row = db.execute("""SELECT q.*,s.link,s.location,st.platform_id,st.live_status,a.platform_id AS channel
                            FROM source_queue q JOIN sources s ON s.id=q.source
                            JOIN streamer_streams st ON st.source=s.id JOIN streamer_accounts a ON a.id=st.account
                            WHERE q.readiness='waiting' AND q.platform='youtube' AND s.state='queued'
                            AND q.next_check<=? ORDER BY q.next_check,q.source LIMIT 1""",(now,)).fetchone()
        height = portal_settings.current(db)[0]["resources"]["remote_height"] if row else None
    if not row:
        return False
    duration = None
    try:
        if row["location"] and Path(row["location"]).is_file():
            duration = media.probe(Path(row["location"]))["duration"]
        else:
            if resolve_replay is None:
                from . import flashdata, flashdata_profiles
                configured = flashdata.youtube_configuration(root)
                data = (flashdata_profiles.source(root,configured,row["platform_id"]) if configured else
                        streamer_catalog.extract(row["link"], "--no-playlist", "--skip-download", "-J"))
                if data.get("channel_id") != row["channel"]:
                    raise streamer_transfer.TransferHold("Replay channel identity changed.")
                if configured:
                    with store.connect(root) as db:
                        policy = flashdata_profiles.settings(db)
                    available = {"id":data["id"],"duration":data["duration"],
                                 "audio":not data.get("stale") and data["live_status"] not in REPLAY_STATES
                                 and flashdata_profiles.rank(data,"audio",policy),
                                 "video":flashdata_profiles.rank(data,"hd",policy)}
                else:
                    available = streamer_transfer.replay_renditions(data, height)
            else:
                available = resolve_replay(row["link"],height)
            if available["id"] == row["platform_id"] and available["audio"] and available["video"]:
                duration = available["duration"]
    except (ValueError, OSError, media.MediaError):
        pass
    with store.connect(root,True) as db:
        if duration:
            updated = db.execute("""UPDATE source_queue SET readiness='ready',detail='',checked=CURRENT_TIMESTAMP,
                                    failures=0,next_check=0 WHERE source=? AND readiness='waiting' AND next_check=?""",
                                 (row["source"],row["next_check"]))
            if updated.rowcount:
                db.execute("""INSERT INTO portal_source_metadata VALUES(?,?) ON CONFLICT(source)
                              DO UPDATE SET duration=excluded.duration""",(row["source"],duration))
                if row["live_status"] in REPLAY_STATES:
                    db.execute("UPDATE streamer_streams SET live_status='was_live' WHERE source=?",(row["source"],))
        else:
            delay = min(3600,120 * 2**min(row["failures"],5))
            db.execute("""UPDATE source_queue SET failures=failures+1,next_check=?,checked=CURRENT_TIMESTAMP
                          WHERE source=? AND readiness='waiting' AND next_check=?""",
                       (now+delay,row["source"],row["next_check"]))
    return True


def rows(db):
    return db.execute("""SELECT q.*,s.link,s.priority,s.state,s.location,c.name,st.title,c.id AS creator,
                         (SELECT state FROM jobs j WHERE j.source=s.id AND j.revision=s.revision
                          ORDER BY id DESC LIMIT 1) AS job_state,
                         (SELECT MAX(f.covered_seconds) FROM factory_events f JOIN jobs j ON j.id=f.job
                          WHERE j.source=s.id AND f.stage IN ('Judge','Select')) AS analyzed,
                         (SELECT COUNT(*) FROM done_stories d WHERE d.source=s.id) AS made,
                         COALESCE(m.duration,0) AS duration
                         FROM source_queue q JOIN sources s ON s.id=q.source
                         LEFT JOIN streamer_streams st ON st.source=s.id
                         LEFT JOIN streamer_accounts a ON a.id=st.account
                         LEFT JOIN streamer_creators c ON c.id=a.creator
                         LEFT JOIN portal_source_metadata m ON m.source=s.id
                         ORDER BY s.state='archived',s.priority,s.id DESC""")


def panel(db, token, selected=None, root=None):
    from collections import Counter
    from . import daily_release

    h, form, button = views.h, views.form, views.button
    new = form("/sources/queue/resolve",token,
               '<label>Stream link<input name="link" type="url" required></label>'
               '<label class="toggle"><input type="checkbox" name="archive_upload"> Full-stream archive upload</label>'
               +button("Resolve stream link","primary"))
    active, archived = [], []
    reveal_archive = False
    buffered = Counter(item["source"] for item,_,_ in daily_release.ready(db,root)) if root else Counter()
    progress = {r["source"]:r for r in db.execute("""SELECT source,
                    SUM(published>0 AND published<targets) AS partial, SUM(published=targets) AS full
                    FROM (SELECT s.source,COUNT(*) AS targets,SUM(d.state='confirmed') AS published
                          FROM factory_release_destinations d JOIN done_delivery_intents i ON i.id=d.intent
                          JOIN done_stories s ON s.id=i.story GROUP BY i.story) GROUP BY source""")}
    for row in rows(db):
        sid = row["source"]
        state = {"running":"Processing","complete":"Completed","failed":"Held","stopped":"Held"}.get(row["job_state"],"Queued")
        if row["state"] != "queued":
            state = row["state"].capitalize()
        elif row["readiness"] != "ready":
            state = "Held"
        lineage = f' · <a href="#queue-source-{row["previous"]}">Replaces source {row["previous"]}</a>' if row["previous"] else ""
        actions = form(f"/sources/queue/{sid}/update",token,
                       f'<label>Queue priority<input type="number" name="priority" min="1" max="9999" value="{row["priority"]}"></label>'
                       +button("Save priority"))
        for target,label in ((("queued","Restore source"),) if row["state"]=="archived" else
                             ((("paused","Pause source"),) if row["state"]=="queued" else (("queued","Resume source"),))
                             + (("archived","Remove from queue"),)):
            actions += form(f"/sources/queue/{sid}/update",token,f'<input type="hidden" name="state" value="{target}">'+button(label))
        if row["platform"]!="file":
            actions += form("/sources/queue/resolve",token,
                            f'<input type="hidden" name="previous" value="{sid}">'
                            f'<label>Replace stream link<input name="link" type="url" value="{h(row["link"])}" required></label>'
                            '<label class="toggle"><input type="checkbox" name="archive_upload"> Full-stream archive upload</label>'
                            +button("Resolve replacement"))
        target = archived if row["state"]=="archived" else active
        reveal_archive |= target is archived and selected==str(sid)
        stats = (f'{row["made"]} distinct reels · {views.timecode(row["analyzed"] or 0)} analyzed'
                 + (f' of {views.timecode(row["duration"])}' if row["duration"] else ""))
        delivery = progress.get(sid)
        stats += (f' · {buffered[sid]} ready in buffer · {delivery["partial"] if delivery else 0} partly delivered'
                  f' · {delivery["full"] if delivery else 0} fully posted')
        open_source = (f'<a href="/sources?creator={row["creator"]}#roster-stream-{sid}">Creator and recording</a> · '
                       if row["creator"] else '')
        target.append(f'<article id="queue-source-{sid}" class="roster-video"><h3>{h(row["title"] or row["link"])}</h3>'
                      f'<p>{h(ADAPTERS[row["platform"]])} · {h(row["name"] or "Creator unverified")} · {h(state)} · {stats}</p>'
                      f'<p>{h(row["detail"])}</p><p>{h(row["provenance"])}{lineage}</p>'
                      + (f'<p><a href="{h(row["link"])}">{h(row["link"])}</a></p>' if row["platform"]!="file" else "")
                      +
                      f'<p>{open_source}<a href="/factory?work=done&amp;source={sid}">Reels and receipts</a></p>'
                      f'<details><summary>Manage queue entry</summary>{actions}</details></article>')
    return ('<section id="stream-queue" class="panel operator-panel"><h2>Paste stream link</h2>'
            '<p>Saving links leaves them in the queue. Processing limits and buffer thresholds apply when a source starts.</p>' + new
            +f'<details{" open" if selected and not reveal_archive else ""}><summary>Source queue · {len(active)} active</summary>'
             f'{"".join(active) or "<p>No links queued.</p>"}</details>'
            +f'<details{" open" if reveal_archive else ""}><summary>Removed sources · {len(archived)}</summary>'
             f'{"".join(archived)}</details></section>')
