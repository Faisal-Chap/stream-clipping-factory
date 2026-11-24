"""Versioned control groups and atomic source-level admission."""

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import factory_portal, portal_templates, settings, store, streamer_catalog
from .provider_api import validate_tier

PLATFORMS = ("youtube","tiktok","instagram","facebook")
STORAGE_FIELDS = {"refill","refill_low","refill_high","reclaim","grace_hours"}
COMMON_FIELDS = {
    "resources": ("paused","zone"),
    "uploads": ("enabled","local_release","daily_stories","accounts","youtube_privacy","tiktok_privacy"),
    "connections": ("hosted","provider_cap_cents"),
}
SECTIONS = {
    "production": {"hourly_target":7,"daily_videos":5,"daily_creators":3,"daily_per_creator":2,
                   "mode":"even","ids":[],"editor_minutes":60},
    "resources": {"zone":"Asia/Shanghai","reset":"00:00","window_start":"00:00","window_end":"00:00",
                  "paused":False,"concurrency":1,"remote_minutes":600,"remote_height":1080,"remote_mib":8192,
                  "reserve_mib":2048,"asr_threads":6,"asr_model":"base.en","bandwidth":0},
    "uploads": {"enabled":False,"local_release":False,"daily_stories":15,"provider_daily":100,
                "refill":False,"refill_low":45,"refill_high":60,"reclaim":False,"grace_hours":96,
                "accounts":[],"release":"scheduled","zone":"Asia/Shanghai","window_start":"10:00",
                "window_end":"20:00","spacing":30,"account_daily":5,"caption":"","title":"",
                "youtube_privacy":"private","tiktok_privacy":"SELF_ONLY"},
    "connections": {"hosted":False,"sail_tier":"balanced","provider_cap_cents":25,"google_embeddings":False},
}
NUMBERS = {"hourly_target":(7,24),"daily_videos":(1,100),"daily_creators":(1,100),"daily_per_creator":(1,100),
           "editor_minutes":(1,240),"concurrency":(1,4),"remote_minutes":(1,1440),"remote_height":(240,2160),
           "remote_mib":(64,20480),"reserve_mib":(256,65536),"asr_threads":(1,32),"bandwidth":(0,100000),
           "spacing":(0,1440),"account_daily":(1,100),"provider_cap_cents":(1,1000),
           "daily_stories":(1,100),"provider_daily":(1,500),"refill_low":(0,1000),
           "refill_high":(1,1000),"grace_hours":(72,96)}
OPTIONS = {"release":("immediate","scheduled"),"asr_model":("base.en","small.en"),
           "youtube_privacy":("private","unlisted","public"),
           "tiktok_privacy":("SELF_ONLY","FOLLOWER_OF_CREATOR","MUTUAL_FOLLOW_FRIENDS","PUBLIC_TO_EVERYONE")}


def current(db):
    data = {key:dict(defaults) for key,defaults in SECTIONS.items()}
    old = settings.get(db)
    data["connections"].update({key:old[key] for key in ("hosted","sail_tier","provider_cap_cents")})
    worker = db.execute("SELECT zone,paused FROM factory_controls").fetchone()
    data["resources"].update(zone=worker["zone"],paused=bool(worker["paused"]))
    versions = {}
    for row in db.execute("""SELECT * FROM portal_settings WHERE id IN
                             (SELECT MAX(id) FROM portal_settings GROUP BY section)"""):
        if row["section"] in data:
            data[row["section"]].update(json.loads(row["data"]))
            versions[row["section"]] = row["id"]
    if data["uploads"]["grace_hours"] < NUMBERS["grace_hours"][0]:
        data["uploads"]["grace_hours"] = SECTIONS["uploads"]["grace_hours"]
    return data,versions


def clock(value):
    try:
        time = datetime.strptime(value,"%H:%M").time()
    except ValueError:
        raise ValueError("Use a local time from 00:00 through 23:59.") from None
    return time.strftime("%H:%M")


def save(db, group, fields):
    previous,versions = current(db)
    if group not in SECTIONS or int(fields["version"])!=versions.get(group,0):
        raise ValueError("This group changed in another tab. Reopen it before saving.")
    data = dict(previous[group])
    for key, default in SECTIONS[group].items():
        if fields.get("common")=="on" and key not in COMMON_FIELDS.get(group,()):
            continue
        if group=="uploads" and key in STORAGE_FIELDS:
            continue
        field = fields.get(key,"").strip()
        if key in NUMBERS:
            if key not in fields:
                continue
            low,high = NUMBERS[key]
            try:
                data[key] = int(field)
            except ValueError:
                raise ValueError(key.replace("_"," ").capitalize()+" expects a whole number.") from None
            if not low <= data[key] <= high:
                raise ValueError(f'{key.replace("_"," ").capitalize()} must be {low}–{high}.')
        elif isinstance(default,bool):
            data[key] = field=="on"
        elif key in OPTIONS:
            if field not in OPTIONS[key]:
                raise ValueError("Choose one of the available options.")
            data[key] = field
        elif key=="zone":
            factory_portal.tz(field)
            data[key] = field
        elif key in ("reset","window_start","window_end"):
            data[key] = clock(field)
        elif key=="sail_tier":
            data[key] = validate_tier(field)
        elif key=="accounts":
            requested = upload_accounts(fields)
            known = json.loads(db.execute("SELECT accounts FROM indiepost_connection").fetchone()[0])
            if set(requested)-{item["id"] for item in known}:
                raise ValueError("Choose destination accounts listed by a successful connection test.")
            data[key] = requested
        elif key in ("title","caption"):
            if len(field) > (100 if key=="title" else 2000):
                raise ValueError("Shorten the default title or caption.")
            data[key] = field
    if group=="production":
        if max(data["daily_creators"],data["daily_per_creator"])>data["daily_videos"]:
            raise ValueError("Creator and per-creator ceilings must fit within the daily video ceiling.")
        data.update(portal_templates.validate_policy(db,fields,inherit=False))
    if group=="resources":
        db.execute("UPDATE factory_controls SET zone=?,paused=?",(data["zone"],data["paused"]))
    if group=="uploads" and data["refill_low"]>=data["refill_high"]:
        raise ValueError("The refill stop threshold must be greater than the refill start threshold.")
    return db.execute("INSERT INTO portal_settings(section,data) VALUES(?,?)",(group,json.dumps(data))).lastrowid


def ids(text):
    return list(dict.fromkeys(item.strip() for item in text.split(",") if item.strip()))


def save_scheduler(db, fields):
    groups,versions = current(db)
    if int(fields["version"])!=versions.get("uploads",0):
        raise ValueError("Settings changed in another tab. Reload Storage and try again.")
    low,high = int(fields["minimum"]),int(fields["maximum"])
    if not 1<=low<high<=1000:
        raise ValueError("Set a minimum below the maximum, between 1 and 1,000 reels.")
    minimum_gap, maximum_gap = NUMBERS["spacing"]
    try:
        spacing = int(fields.get("spacing",groups["uploads"]["spacing"]))
    except ValueError:
        raise ValueError("Enter the publishing gap in whole minutes.") from None
    if not minimum_gap<=spacing<=maximum_gap:
        raise ValueError(f"The publishing gap must be {minimum_gap}–{maximum_gap} minutes.")
    enabled = fields.get("refill")=="on"
    values = dict(groups["uploads"],refill=enabled,refill_low=low,refill_high=high,spacing=spacing)
    db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",(json.dumps(values),))
    if not enabled:
        db.execute("UPDATE factory_release_refill SET active=0,detail='Scheduler off.'")


def save_cleanup(db, fields):
    groups,versions = current(db)
    if int(fields["version"])!=versions.get("uploads",0):
        raise ValueError("Settings changed in another tab. Reload Storage and try again.")
    days = int(fields["days"])
    if days not in (3,4):
        raise ValueError("Choose three or four days after confirmed publishing.")
    values = dict(groups["uploads"],reclaim=fields.get("reclaim")=="on",grace_hours=days*24)
    db.execute("INSERT INTO portal_settings(section,data) VALUES('uploads',?)",(json.dumps(values),))


def upload_accounts(fields):
    if "accounts" in fields:
        return ids(fields["accounts"])
    return [key.removeprefix("destination:") for key,value in fields.items()
            if key.startswith("destination:") and value=="on"]


def policy(db, creator):
    saved = db.execute("SELECT * FROM portal_creator_policy WHERE creator=?",(creator,)).fetchone()
    return (dict(json.loads(saved["style"]),daily_limit=saved["daily_limit"],platforms=json.loads(saved["platforms"]))
            if saved else {"mode":"inherit","ids":[],"daily_limit":None,"platforms":[]})


def save_policy(db, creator, fields):
    selected = portal_templates.validate_policy(db,fields,inherit=True)
    production = current(db)[0]["production"]
    limit = int(fields["daily_limit"]) if fields.get("daily_limit") else None
    if limit is not None and not 1<=limit<=production["daily_per_creator"]:
        raise ValueError("The creator ceiling must fit within the factory’s per-creator ceiling.")
    platforms = ids(fields.get("platforms",""))
    if set(platforms)-set(PLATFORMS):
        raise ValueError("Choose supported publishing platforms.")
    db.execute("""INSERT INTO portal_creator_policy(creator,style,daily_limit,platforms) VALUES(?,?,?,?)
                  ON CONFLICT(creator) DO UPDATE SET style=excluded.style,daily_limit=excluded.daily_limit,
                  platforms=excluded.platforms""",(creator,json.dumps(selected),limit,json.dumps(platforms)))


def day(config, now=None):
    zone = factory_portal.tz(config["zone"])
    hours,minutes = map(int,config["reset"].split(":"))
    return ((now or datetime.now(timezone.utc)).astimezone(zone)-timedelta(hours=hours,minutes=minutes)).date().isoformat()


def eligible(db):
    rows = db.execute("""SELECT s.id,s.link,s.location,st.title,a.creator,c.name FROM streamer_streams st
                JOIN sources s ON s.id=st.source JOIN streamer_accounts a ON a.id=st.account
                JOIN streamer_creators c ON c.id=a.creator LEFT JOIN source_queue q ON q.source=s.id
                WHERE c.enabled AND s.state='queued'
                AND COALESCE(q.readiness,CASE WHEN st.live_status IN ('is_live','is_upcoming','post_live')
                                                      AND s.location='' THEN 'waiting' ELSE 'ready' END)='ready'
                AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source=s.id AND j.revision=s.revision
                  AND json_extract(j.settings,'$.method')='streamer')
                ORDER BY s.priority,COALESCE((SELECT MAX(p.day) FROM portal_admissions p WHERE p.creator=a.creator),''),
                s.id DESC""")
    return [row for row in rows if available_input(row)]


def available_input(row):
    preferred = row["location"] or row["link"]
    return Path(preferred).is_file() if row["location"] or Path(preferred).is_absolute() else preferred.startswith("https://")


def admission_state(db, config, now=None):
    date = day(config["resources"],now)
    by_creator = Counter()
    sources = set()
    for row in db.execute("SELECT creator,source FROM portal_admissions WHERE day=?",(date,)):
        by_creator[row["creator"]] += 1
        sources.add(row["source"])
    return date,by_creator,sources


def backfill_admissions(db):
    marker = "portal-admissions-from-legacy"
    if db.execute("SELECT 1 FROM metadata WHERE key=?",(marker,)).fetchone():
        return
    resources = current(db)[0]["resources"]
    rows = db.execute("""SELECT j.id,j.source,j.created,a.creator FROM jobs j
                         JOIN streamer_streams st ON st.source=j.source JOIN streamer_accounts a ON a.id=st.account
                         WHERE json_extract(j.settings,'$.method')='streamer'
                         AND json_extract(j.settings,'$.admission_day') IS NULL ORDER BY j.created,j.id""")
    for row in rows:
        submitted = datetime.fromisoformat(row["created"]).replace(tzinfo=timezone.utc)
        db.execute("INSERT OR IGNORE INTO portal_admissions VALUES(?,?,?,?)",
                   (day(resources,submitted),row["source"],row["creator"],row["id"]))
    db.execute("INSERT INTO metadata VALUES(?)",(marker,))


def hold_reason(db, config, source, creator, by_creator, sources, waiting):
    if config["resources"]["paused"]:
        return "New work is paused in Controls."
    if source in sources:
        return ""
    limits = config["production"]
    if len(sources)>=limits["daily_videos"]:
        return "Today’s total source-video ceiling has been reached."
    per_creator = min(limits["daily_per_creator"],policy(db,creator)["daily_limit"] or limits["daily_per_creator"])
    if by_creator[creator]>=per_creator:
        return "This creator has reached today’s video ceiling."
    if creator not in by_creator and len(by_creator)>=limits["daily_creators"]:
        return "Today’s distinct-creator ceiling has been reached."
    if by_creator[creator] and len(by_creator)<limits["daily_creators"] and any(
            row["creator"] not in by_creator and row["id"] not in sources for row in waiting):
        return "An eligible creator who has not been admitted today gets the next slot."
    return ""


def start(db, source, fields, now=None, *, buffer_admission=False):
    owner = db.execute("""SELECT s.revision,s.state,s.link,s.location,st.*,a.creator,c.enabled,c.name,c.credit,c.camera,
                COALESCE(q.readiness,CASE WHEN st.live_status IN ('is_live','is_upcoming','post_live')
                                  AND s.location='' THEN 'waiting' ELSE 'ready' END) AS readiness
                FROM sources s JOIN streamer_streams st ON st.source=s.id JOIN streamer_accounts a ON a.id=st.account
                JOIN streamer_creators c ON c.id=a.creator LEFT JOIN source_queue q ON q.source=s.id
                WHERE s.id=?""",(source,)).fetchone()
    if not owner:
        raise ValueError("Assign this source to a creator in Sources before starting.")
    prior = db.execute("""SELECT id,state FROM jobs WHERE source=? AND revision=? AND
                          json_extract(settings,'$.method')='streamer' ORDER BY id DESC LIMIT 1""",
                       (source,owner["revision"])).fetchone()
    rerun = fields.get("rerun_from","")
    part_start = fields.get("directory_part")
    if part_start is not None:
        active = db.execute("""SELECT id FROM jobs WHERE source=? AND revision=? AND state IN ('queued','running')
                               AND json_extract(settings,'$.method')='streamer' ORDER BY id DESC LIMIT 1""",
                            (source,owner["revision"])).fetchone()
        if active:
            return active["id"],False
        prior_part = db.execute("""SELECT id FROM jobs WHERE source=? AND revision=?
                                   AND json_extract(settings,'$.directory_part')=?
                                   ORDER BY id DESC LIMIT 1""",(source,owner["revision"],part_start)).fetchone()
        if prior_part:
            return prior_part["id"],False
    elif prior and (not rerun or rerun!=str(prior["id"]) or prior["state"] in ("queued","running")):
        return prior["id"],False
    if owner["state"]!="queued" or not owner["enabled"]:
        raise ValueError("Resume the creator and source before admitting a new attempt.")
    if owner["readiness"]!="ready":
        raise ValueError("This stream is waiting for a seekable replay.")
    if not available_input(owner):
        raise ValueError("The local recording is missing on the factory machine. Fix the source before starting.")
    config, versions = current(db)
    date,counts,sources = admission_state(db,config,now)
    if config["resources"]["paused"]:
        raise ValueError("New work is paused in Controls.")
    reason = "" if buffer_admission else hold_reason(db,config,source,owner["creator"],counts,sources,eligible(db))
    if reason:
        raise ValueError(reason)
    if not config["connections"]["hosted"] and fields.get("hosted")!="on":
        raise ValueError("Enable Sail analysis for this start or set its default in Controls.")
    production, resources, connections = (config[key] for key in ("production","resources","connections"))
    creator_style = policy(db,owner["creator"])
    choice = production if creator_style["mode"]=="inherit" else creator_style
    rotation = db.execute("SELECT COUNT(*) FROM jobs WHERE json_type(settings,'$.template_plan')='object'").fetchone()[0]
    template_plan = portal_templates.plan(db,choice,rotation)
    limits = {key:resources[key] for key in streamer_catalog.DEFAULTS if key in resources}
    limits["bandwidth"] = resources["bandwidth"]
    from .library import identify
    from . import replay_cache
    route = identify(owner["location"] or owner["link"])[2]
    full = route=="file" or (replay_cache.enabled() and identify(owner["link"])[2]=="youtube")
    if full or route!="file":
        from . import streamer_hybrid, streamer_story_v4, streamer_transfer
        limits["acquisition"] = "audio-first"
        limits["hybrid_version"] = streamer_hybrid.VERSION
        limits["hybrid_prompt"] = streamer_story_v4.VERSION
        limits["owner_viewing"] = True
        for name, low, high in (("coverage_start",0,172799),("coverage_minutes",1,resources["remote_minutes"])):
            if value := fields.get(name,"").strip():
                try:
                    point = int(value)
                except ValueError:
                    raise ValueError("Use whole seconds for the trial start and whole minutes for trial length.") from None
                if not low<=point<=high:
                    raise ValueError(f"The trial exceeds the allowed {name.replace('_',' ')} range.")
                limits[name] = point
        if limits.get("coverage_start") and "coverage_minutes" not in limits:
            raise ValueError("Enter a bounded trial length when the trial starts after source time zero.")
        if not full and identify(owner["link"])[2]=="youtube":
            remaining = streamer_transfer.REPLAY_LIMIT-limits.get("coverage_start",0)
            if remaining<=0:
                raise ValueError(streamer_transfer.REPLAY_LIMIT_MESSAGE)
            minutes = math.ceil(remaining/60)
            if limits.get("coverage_minutes",resources["remote_minutes"])>minutes:
                limits["coverage_minutes"] = minutes
        if part_start is not None:
            limits["directory_part"] = part_start
        if full:
            part = limits.get("coverage_start",0)
            if part%3600 or part>=replay_cache.MAX_DURATION:
                raise ValueError("Choose a valid one-hour part within the full replay.")
            if part and not db.execute("""SELECT 1 FROM jobs WHERE source=? AND revision=? AND state='complete'
                              AND json_extract(settings,'$.full_replay')=1
                              AND json_extract(settings,'$.directory_part')=?""",
                              (source,owner["revision"],part-3600)).fetchone():
                part = 0
            limits.update(full_replay=True,coverage_start=part,coverage_minutes=60,directory_part=part,asr_model="small")
    safe_override = fields.get("hourly_target","")
    hourly = int(safe_override) if safe_override else production["hourly_target"]
    if not 7<=hourly<=24:
        raise ValueError("Hourly target must be 7–24.")
    limits.update(target=streamer_catalog.DEFAULTS["target"], hourly_target=hourly,
                  editor_minutes=production["editor_minutes"],method="streamer",hosted=True,
                  sail_tier=connections["sail_tier"],
                  provider_cap_cents=connections["provider_cap_cents"],
                  stream=dict(owner),portal_version=max(versions.values(),default=0),portal_versions=versions,
                  admission_day=date,creator_daily_limit=creator_style["daily_limit"],
                  portal_overrides={"hourly_target":hourly} if safe_override else {},
                  admission_limits={key:production[key] for key in ("daily_videos","daily_creators","daily_per_creator")},
                  template_plan=template_plan)
    job = store.enqueue(db,source,fresh=True,overrides=limits)
    db.execute("INSERT OR IGNORE INTO portal_admissions VALUES(?,?,?,?)",(date,source,owner["creator"],job))
    return job,True
