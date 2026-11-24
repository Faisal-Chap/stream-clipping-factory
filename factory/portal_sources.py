"""Operator roster and confirmed YouTube and file intake."""

from pathlib import Path
from urllib.parse import urlencode

from . import portal_settings, portal_templates, source_queue, streamer_catalog as catalog, views
from .library import identify

h,form,button = views.h,views.form,views.button


def field(key,label,value="",kind="text",required=False):
    return (f'<label>{h(label)}<input name="{key}" type="{kind}" value="{h(value)}"'
            f'{" required" if required else ""}></label>')


def add_channel(db, fields, channel, creator=None):
    if creator is None:
        return catalog.save_creator(db,fields,channel)
    existing = db.execute("SELECT creator FROM streamer_accounts WHERE platform='youtube' AND platform_id=?",
                          (channel,)).fetchone()
    if existing:
        if existing["creator"]!=creator:
            raise ValueError("That channel already belongs to another creator in Sources.")
    else:
        db.execute("INSERT INTO streamer_accounts(creator,platform,platform_id,url) VALUES(?,'youtube',?,?)",
                   (creator,channel,f"https://www.youtube.com/channel/{channel}"))
    return creator


def resolve_video(db,creator,fields):
    owner = catalog.roster(db,creator,fields.get("account") or None)
    _,canonical,route = identify(fields["link"])
    if route=="file":
        if not Path(canonical).is_file():
            raise ValueError("The recording is missing on the factory machine. Fix the path or choose a file from this browser.")
        return owner,canonical,None
    if route!="youtube":
        raise ValueError("Choose an attached YouTube broadcast or a local recording.")
    cached = db.execute("""SELECT st.title,st.live_status,st.account,a.creator,a.platform_id,
                                COALESCE(m.duration,(SELECT max(a.duration) FROM jobs j JOIN assets a ON a.hash=j.asset
                                                     WHERE j.source=s.id)) AS duration
                           FROM sources s JOIN streamer_streams st ON st.source=s.id
                           JOIN streamer_accounts a ON a.id=st.account LEFT JOIN portal_source_metadata m ON m.source=s.id
                           WHERE s.link=?""",(canonical,)).fetchone()
    if cached and cached["live_status"] in ("was_live","archive_upload"):
        if cached["creator"]!=creator:
            raise ValueError("This video belongs to a different creator’s attached source account.")
        owner = catalog.roster(db,creator,cached["account"])
        return owner,canonical,{"title":cached["title"],"channel_id":cached["platform_id"],"duration":cached["duration"],
                                "live_status":"was_live" if cached["live_status"]=="was_live" else "not_live",
                                "known_attachment":True}
    info = catalog.extract(canonical,"--skip-download","--no-playlist","-j")
    if info.get("channel_id")!=owner["platform_id"]:
        match = db.execute("SELECT id FROM streamer_accounts WHERE platform='youtube' AND platform_id=? AND creator=?",
                           (info.get("channel_id"),creator)).fetchone()
        if match:
            owner = catalog.roster(db,creator,match["id"])
        else:
            raise ValueError("This video belongs to a different channel. Add that channel to the correct creator, then select it here.")
    archive = fields.get("archive_upload")=="on" and info.get("live_status")=="not_live" and (info.get("duration") or 0)>=1200
    if info.get("live_status") not in ("was_live","is_live","is_upcoming","post_live") and not archive:
        raise ValueError("Use a broadcast, or select the archive-upload option for a full stream upload.")
    return owner,canonical,info


def confirm(token, path, fields, title, detail):
    saved = "".join(f'<input type="hidden" name="{h(key)}" value="{h(value)}">' for key,value in fields.items())
    return (f'<section class="panel operator-panel"><h2>{h(title)}</h2><p>{h(detail)}</p>'
            +form(path,token,saved+button("Save attachment","primary"))+'<p><a href="/sources">Back to Sources</a></p></section>')


def channel_form(token, creator=None):
    fields = (field("name","Creator display name",required=True)+field("game","Game or category","Gaming",required=True)
              if creator is None else "")
    base = f"/sources/creator/{creator}/channel" if creator else "/sources/channel"
    return form(base+"/preview",token,fields+field("account","YouTube channel URL",kind="url",required=True)+button("Resolve channel"))


def video_intake(token, creator, accounts):
    base = f"/sources/creator/{creator}/video"
    options = "".join(f'<option value="{r["id"]}">YouTube · {h(r["platform_id"])}</option>' for r in accounts)
    common = '<label>Source account<select name="account">'+options+'</select></label>'
    body = (common+field("link","Completed YouTube broadcast URL or server-side recording path",required=True)
            +field("title","Local recording title")+field("location","Optional server-side recording override")
            +field("chat_path","Optional server-side chat path")
            +'<label class="toggle"><input type="checkbox" name="archive_upload"> Full-stream upload on an archive channel</label>'
            +button("Resolve video","primary"))
    browser = ('<div class="workspace-actions"><label>Choose a recording from this browser'
               '<input type="file" accept="video/*,.mkv" data-recording-file></label>'
               '<button type="button" data-recording-upload>Attach browser file</button></div><output data-recording-status></output>')
    return '<div data-recording-intake>'+form(base+"/preview",token,body)+browser+'</div>'


def video_row(db, token, row, config):
    sid = row["source"]
    remote = row["link"].startswith("https://") and not row["location"]
    current = db.execute("""SELECT id,state,settings FROM jobs WHERE source=? AND revision=?
                         AND json_extract(settings,'$.method')='streamer' ORDER BY id DESC LIMIT 1""",(sid,row["revision"])).fetchone()
    archive = db.execute("""SELECT j.id,a.duration,a.path FROM jobs j JOIN assets a ON a.hash=j.asset
                           WHERE j.source=? AND json_extract(j.settings,'$.method')='streamer' ORDER BY j.id DESC LIMIT 1""",
                         (sid,)).fetchone()
    if current:
        action = f'<a class="button" href="/factory?stream={sid}&amp;attempt={current["id"]}#stream-{sid}">View in Factory</a>'
        if current["state"] in ("complete","failed","stopped"):
            action += '<details><summary>Start a new attempt</summary>'+form(
                f"/sources/video/{sid}/start",token,
                f'<input type="hidden" name="rerun_from" value="{current["id"]}">'+start_fields(config,remote)+button("Start a new attempt"))+'</details>'
    elif row["enabled"] and row["source_state"]=="queued" and row["readiness"]=="ready":
        action = form(f"/sources/video/{sid}/start",token,start_fields(config,remote)+button("Start clipping","primary"))
    elif row["readiness"]=="waiting":
        action = "<p>Start becomes available when the replay is seekable.</p>"
    else:
        action = '<p>Resume this creator and source before starting.</p>'
    ranged = bool(archive and archive["path"].endswith("/range-index.json"))
    play = (f'<a href="/streamer/run/{archive["id"]}/archive">Play original</a>' if archive and not ranged else
            f'<a href="{h(row["link"])}">Play original on YouTube</a>' if remote else "")
    duration = views.timecode(archive["duration"] or row["duration_hint"]) if archive else (
        views.timecode(row["duration_hint"]) if row["duration_hint"] else "Duration pending")
    pending = row["live_status"] in ("is_live","is_upcoming","post_live") and not archive and remote
    archive_status = ('Waiting for a completed replay' if pending else
                      'Audio-first intake' if ranged else 'Archive retained' if archive else
                      'Local recording attached' if not remote else "Audio-first intake pending")
    fix = '<details><summary>Fix source or attach evidence</summary>'+form(
        f"/sources/video/{sid}/evidence",token,field("location","Server-side recording override",row["location"])
        +field("chat_path","Server-side chat path",row["chat_path"] or "")+button("Save source evidence"))+'</details>'
    return (f'<article class="roster-video" id="roster-stream-{sid}"><h3>{h(row["title"])}</h3>'
            f'<p>{h(row["platform"].title())} · {h(duration)} · {archive_status}</p>'
            f'<div class="workspace-actions">{action}{play}</div>{fix}</article>')


def start_fields(config,remote=False):
    consent = ('' if config["connections"]["hosted"] else
               '<label class="toggle"><input name="hosted" type="checkbox" required> Enable Sail analysis for this start '
               '(transcript text and selected JPEG frames)</label>')
    hourly = config["production"]["hourly_target"]
    trial = ('<details><summary>Limit audio coverage for a trial</summary>'
             '<label>Trial start in source seconds<input name="coverage_start" type="number" min="0" placeholder="0"></label>'
             f'<label>Audio trial length in minutes<input name="coverage_minutes" type="number" min="1" '
             f'max="{config["resources"]["remote_minutes"]}"></label></details>') if remote else ""
    return (consent+trial+'<details><summary>Hourly target override</summary>'
            f'<label>Qualified reels per full hour for this attempt<input name="hourly_target" type="number" '
            f'min="7" max="24" placeholder="Factory default: {hourly}"></label></details>')


def page(db, token, query, posted=None, root=None):
    config,_ = portal_settings.current(db)
    selected = query.get("creator")
    if not selected and query.get("stream","").isdigit():
        row = db.execute("""SELECT a.creator FROM streamer_streams st JOIN streamer_accounts a ON a.id=st.account
                            WHERE st.source=?""",(query["stream"],)).fetchone()
        selected = str(row["creator"]) if row else None
    groups = []
    for creator in db.execute("SELECT * FROM streamer_creators ORDER BY name COLLATE NOCASE,id"):
        cid = creator["id"]
        style = portal_settings.policy(db,cid)
        effective = config["production"] if style["mode"]=="inherit" else style
        try:
            labels = ", ".join(x["name"] for x in portal_templates.choices(db,effective))
            if query.get("style") and query["style"] not in {x["template"] for x in portal_templates.choices(db,effective)}:
                continue
        except ValueError:
            labels = "Choose active replacements"
        accounts = list(db.execute("SELECT * FROM streamer_accounts WHERE creator=? ORDER BY id",(cid,)))
        videos = list(db.execute("""SELECT st.*,s.link,s.location,s.revision,s.state AS source_state,c.enabled,
                                  a.platform,COALESCE(q.readiness,'ready') AS readiness,
                                  COALESCE(m.duration,0) AS duration_hint FROM streamer_streams st JOIN sources s ON s.id=st.source
                                  JOIN streamer_accounts a ON a.id=st.account JOIN streamer_creators c ON c.id=a.creator
                                  LEFT JOIN portal_source_metadata m ON m.source=st.source
                                  LEFT JOIN source_queue q ON q.source=st.source WHERE a.creator=? ORDER BY st.id DESC""",(cid,)))
        account_cards = []
        for account in accounts:
            aid = account["id"]
            discovered = ""
            for entry in db.execute("SELECT * FROM streamer_discovery WHERE account=? ORDER BY rowid",(aid,)):
                hidden = (f'<input type="hidden" name="link" value="https://www.youtube.com/watch?v={h(entry["video"])}">'
                          f'<input type="hidden" name="account" value="{aid}">')
                discovered += f'<li>{h(entry["title"])}'+form(f"/sources/creator/{cid}/video/preview",token,hidden+button("Choose broadcast"))+'</li>'
            account_cards.append(f'<div class="roster-account"><strong>YouTube</strong> · <a href="{h(account["url"])}">'
                                 f'{h(account["platform_id"])}</a><p>Completed broadcasts and archive uploads. '
                                 f'Last discovery {h(account["checked"] or "not yet checked")}.</p>'
                                 +form(f"/sources/creator/{cid}/discover/{aid}",token,button("Discover broadcasts"))
                                 +(f'<details><summary>Discovered broadcasts</summary><ul>{discovered}</ul></details>' if discovered else "")+'</div>')
        profile = (field("name","Display name",creator["name"],required=True)+field("game","Game filter",creator["game"],required=True)
                   +field("credit","Attribution credit",creator["credit"],required=True)
                   +f'<label class="toggle"><input type="checkbox" name="enabled"{" checked" if creator["enabled"] else ""}> Accept new work</label>')
        policy_fields = (portal_templates.policy_fields(db,style,inherit=True)
                         +field("daily_limit","Daily video ceiling override",style["daily_limit"] or "","number")
                         +field("platforms","Enabled publishing platforms (comma separated)",", ".join(style["platforms"])))
        groups.append(f'<details class="panel operator-panel roster-creator" id="creator-{cid}" data-roster-creator'
                      f'{" open" if str(cid)==selected else ""}><summary><strong>{h(creator["name"])}</strong>'
                      f'<span>YouTube · {"Enabled" if creator["enabled"] else "Paused"} · {views.count_label(len(videos),"video")}'
                      f' · {h(portal_templates.policy_summary(db,effective))}</span></summary>'
                      f'<div class="roster-columns"><div><h2>Source accounts</h2>{"".join(account_cards)}'
                      f'<details><summary>Add another channel</summary>{channel_form(token,cid)}</details>'
                      f'<details><summary>Creator details</summary>{form(f"/sources/creator/{cid}/details",token,profile+button("Save creator"))}</details>'
                      f'<h2>Creator style</h2><p>{h(labels)} · {"Factory default" if style["mode"]=="inherit" else "Creator override"}. '
                      '<a href="/templates">Manage designs</a></p>'
                      +form(f"/sources/creator/{cid}/policy",token,policy_fields+button("Save creator policy"))+
                      '</div><div><h2>Videos</h2><details><summary>Add video</summary>'
                      f'{video_intake(token,cid,accounts)}</details>'
                      +("".join(video_row(db,token,video,config) for video in videos) or "<p>No videos attached.</p>")
                      +'</div></div></details>')
    unassigned = ""
    for row in db.execute("""SELECT s.* FROM sources s LEFT JOIN streamer_streams st ON s.id=st.source
                             LEFT JOIN source_queue q ON q.source=s.id WHERE st.source IS NULL AND q.source IS NULL"""):
        unassigned += (f'<li>{h(row["creator"] or "Unassigned recording")} · '
                       f'<a href="/sources?source={row["id"]}#source-form">Source details</a></li>')
    details = ""
    if query.get("source","").isdigit() or query.get("add") or posted is not None:
        sid = int(query["source"]) if query.get("source","").isdigit() else None
        details = views.library(db,token,sid,posted,detail_only=True)
        options = "".join(f'<option value="{r["id"]}">{h(r["name"])}</option>'
                          for r in db.execute("SELECT id,name FROM streamer_creators ORDER BY name,id"))
        if sid and options and not db.execute("SELECT 1 FROM streamer_streams WHERE source=?",(sid,)).fetchone():
            fields = (f'<input type="hidden" name="source" value="{sid}">'
                      f'<label>Creator<select name="creator">{options}</select></label>')
            details += ('<section class="panel operator-panel"><h2>Assign this recording</h2>'
                        +form("/sources/assign",token,fields+button("Resolve for selected creator"))+'</section>')
    return ('<div class="hero"><div><h1>Sources</h1><p>Creators, channels and recordings</p></div>'
            '</div>'
            +source_queue.panel(db,token,query.get("queue"),root)+
            '<details id="add-creator" class="panel operator-panel"><summary>Add creator</summary>'
            +channel_form(token)+'</details><label class="roster-search">Search creators or videos<input data-roster-search type="search"></label>'
            +("".join(groups) or '<p>No creators match this view.</p>')
            +f'<details id="unassigned" class="panel operator-panel"><summary>Unassigned recordings</summary><ul>{unassigned}</ul>'
             '<p><a href="/sources?add=1#source-form">Add an unassigned recording</a></p></details>'
            +details)
