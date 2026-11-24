"""Operator control groups and publishing settings."""

import json
from collections import Counter
from datetime import datetime, time, timedelta

from . import factory_portal, indiepost, portal_settings as config, portal_templates, settings, shorts_release, views

h,form,button = views.h,views.form,views.button
LABELS = {
    "hourly_target":"Qualified reels per full source hour","daily_videos":"Total source videos per day",
    "daily_creators":"Distinct creators per day","daily_per_creator":"Source videos per creator per day",
    "editor_minutes":"Editor time budget (minutes)","zone":"Timezone","reset":"Daily reset (local time)",
    "window_start":"Window opens","window_end":"Window closes","paused":"Pause new work",
    "concurrency":"Requested worker concurrency (currently 1)","remote_minutes":"Maximum audio coverage per remote attempt (minutes)",
    "remote_height":"Maximum archive height (pixels)","remote_mib":"Maximum archive size (MiB)",
    "reserve_mib":"Free storage reserve (MiB)","asr_threads":"Local ASR threads","asr_model":"Local ASR model",
    "bandwidth":"Audio-first transfer ceiling (KiB/s, 0 for none)","enabled":"Allow publishing",
    "accounts":"Destination accounts","release":"Release mode","spacing":"Minimum publishing gap (minutes)",
    "local_release":"Use the factory's daily release policy","daily_stories":"Distinct stories per local day",
    "provider_daily":"IndiePost post groups per local day","refill":"Automatically refill the buffer",
    "refill_low":"Start refill at or below","refill_high":"Stop refill at or above",
    "reclaim":"Reclaim fully posted portraits","grace_hours":"Reclamation grace period (hours)",
    "account_daily":"Posts per account per day","caption":"Default caption","title":"Default title",
    "youtube_privacy":"YouTube visibility","tiktok_privacy":"TikTok privacy","hosted":"Allow Sail for new admissions by default",
    "sail_tier":"Sail tier","provider_cap_cents":"Sail reservation cap per run (US cents)",
    "google_embeddings":"Use Google embeddings when supported (planned for streamer selection)",
}
GROUPS = {"resources":"Processing","uploads":"Publishing","connections":"Connections"}
HELP = {
    "hourly_target":"Sets a target for legacy workflows. V4 accepts only the stories that qualify.",
    "daily_videos":"Caps distinct sources admitted per day. More hourly runs from an admitted source share its slot.",
    "daily_creators":"Caps how many different creators can receive a new source slot each day.",
    "daily_per_creator":"Caps daily source slots for each creator.",
    "editor_minutes":"Sets the time budget for the editing workflow.",
    "zone":"Sets the local clock for job dates and daily limits.",
    "reset":"Sets the local time when daily processing limits roll over.",
    "window_start":"Stores the opening time for the processing window. Automatic enforcement is pending.",
    "window_end":"Stores the closing time for the processing window. Automatic enforcement is pending.",
    "paused":"Blocks new work. The current job can finish.",
    "concurrency":"Stores the requested number of workers. The local worker currently runs one job at a time.",
    "remote_minutes":"Caps remote audio coverage per run. Hourly starts use this limit if it is below 60.",
    "remote_height":"Caps the resolution requested when retrieving source footage.",
    "remote_mib":"Caps how much remote media an attempt can retrieve.",
    "reserve_mib":"Leaves this much free disk space available before accepting media transfers.",
    "asr_threads":"More threads let local transcription use more CPU.",
    "asr_model":"A larger local model can improve transcription accuracy and takes more compute.",
    "bandwidth":"Lower values throttle audio-first media transfers. Zero leaves them uncapped.",
    "enabled":"Allows approved releases to be sent to configured destinations.",
    "accounts":"Limits the social accounts the factory can send to.",
    "release":"Chooses immediate publication or a scheduled time for an approved send.",
    "spacing":"Spaces new Factory reels and posts on each destination account by at least this many minutes.",
    "local_release":"Lets the factory dispatch due deliveries using its daily release policy.",
    "daily_stories":"Caps distinct reels released by the local policy each day.",
    "provider_daily":"Caps the IndiePost groups the local policy can submit each day.",
    "refill":"Lets the factory start eligible sources when the release buffer runs low.",
    "refill_low":"Starts refilling when the ready buffer reaches this count or falls below it.",
    "refill_high":"Stops refilling once the ready buffer reaches this count.",
    "reclaim":"Allows fully delivered portrait files to be reclaimed after the grace period.",
    "grace_hours":"Keeps fully delivered portraits on disk for at least this many hours.",
    "account_daily":"Caps daily posts for each destination account.",
    "caption":"Supplies the default caption on new publishing actions.",
    "title":"Supplies the default title on new publishing actions.",
    "youtube_privacy":"Sets who can see new YouTube uploads.",
    "tiktok_privacy":"Sets who can see new TikTok uploads.",
    "hosted":"Allows hosted Sail analysis when starting a new run.",
    "sail_tier":"Sets the default provider tier. Audio-first replay runs currently use Flex.",
    "provider_cap_cents":"Stops new Sail requests when the run reaches this reservation budget.",
    "google_embeddings":"Stores the preference for workflows that support Google embeddings.",
}


list_destinations = indiepost.accounts


def connection_state(db):
    return db.execute("SELECT * FROM indiepost_connection").fetchone()


def connection_actions(db, token, vault_available):
    state = connection_state(db)
    account_list = json.loads(state["accounts"])
    tools = ""
    if vault_available:
        key = '<label>IndiePost API key<input name="key" type="password" required maxlength="512" autocomplete="off"></label>'
        tools = form("/connections/indiepost/key",token,key+button("Replace key" if state["reference"] else "Add key"))
        if state["reference"]:
            tools += (form("/connections/indiepost/test",token,button("Test by listing accounts"))+
                      form("/connections/indiepost/remove",token,button("Remove local key")))
    rows = "".join(f'<li>{h(indiepost.PLATFORM_NAMES[a["platform"]])} · {h(a["name"])}</li>' for a in account_list)
    return (f'<h3>IndiePost</h3><p>Workspace · {h(indiepost.WORKSPACE)}</p>'
            f'<p>{h(state["status"])} · Last test {h(state["last_test"] or "pending")}</p>'
            f'<ul>{rows}</ul>{tools}')


def publishing_status(values, accounts):
    if not values["enabled"]:
        state = "Publishing off"
    elif values["local_release"]:
        state = "Publishing on · Factory dispatch"
    else:
        state = "Publishing on · Send from Done"
    selected = sum(account["id"] in values["accounts"] for account in accounts)
    return (f'<p><strong>{state}</strong> · {selected} of {len(accounts)} connected destinations selected. '
            '<a href="#connections">IndiePost connection</a> · '
            '<a href="/factory?work=done">Open Done</a></p>')


def field(key, values, accounts):
    label,value = LABELS[key],values[key]
    if key in config.NUMBERS:
        lo,hi = config.NUMBERS[key]
        return f'<label>{label}<input type="number" name="{key}" min="{lo}" max="{hi}" required value="{h(value)}"></label>'
    if isinstance(value,bool):
        return f'<label class="toggle"><input type="checkbox" name="{key}"{" checked" if value else ""}> {label}</label>'
    if key in config.OPTIONS or key in ("zone","sail_tier"):
        choices = config.OPTIONS.get(key) or (factory_portal.ZONES if key=="zone" else ("balanced","flex"))
        options = []
        for choice in choices:
            name = indiepost.privacy({key: choice},key.removesuffix("_privacy")) if key.endswith("_privacy") else choice
            options.append(portal_templates.option(choice,name,value))
        return f'<label>{label}<select name="{key}">'+"".join(options)+'</select></label>'
    if key=="accounts":
        labels = []
        for account in accounts:
            platform = account["platform"]
            public = ' <span class="meta">· Public reel</span>' if platform in ("instagram","facebook") else ""
            checked = " checked" if account["id"] in value else ""
            labels.append(f'<label class="toggle"><input type="checkbox" name="destination:{h(account["id"])}"'
                          f'{checked}> {h(indiepost.PLATFORM_NAMES[platform])} · {h(account["name"])}{public}</label>')
        return (f'<fieldset><legend>{label}</legend>{"".join(labels)}</fieldset>' if labels
                else '<p>Test the IndiePost key to list connected destination accounts.</p>')
    if key=="caption":
        return f'<label>{label}<textarea name="{key}" maxlength="2000">{h(value)}</textarea></label>'
    kind = "time" if key in ("window_start","window_end","reset") else "text"
    return f'<label>{label}<input name="{key}" type="{kind}" value="{h(value)}"></label>'


def admission_preview(db, settings):
    date,counts,sources = config.admission_state(db,settings)
    production = settings["production"]
    eligible = config.eligible(db)
    allowed,held = [],[]
    for row in eligible:
        reason = config.hold_reason(db,settings,row["id"],row["creator"],counts,sources,eligible)
        if not reason and not settings["connections"]["hosted"]:
            reason = "Awaiting manual Sail consent."
        if reason:
            held.append((row,reason))
        else:
            allowed.append(row)
    if allowed:
        next_item = allowed[0]
        line = f'<p>Next eligible · {h(next_item["name"])} · {h(next_item["title"])}.</p>'
    else:
        line = "<p>No eligible unstarted source fits the current settings.</p>"
    if held:
        item,reason = held[0]
        line += f'<p>Held · {h(item["name"])} · {h(item["title"])}. {h(reason)}</p>'
    supply = len(set(counts)|{row["creator"] for row in eligible})
    if supply<production["daily_creators"]:
        line += f'<p>{supply} creators have admitted or eligible videos. Unused creator slots stay open.</p>'
    admitted = "".join(f'<li>{h(row["name"])} · {h(row["title"] or "Recording")}</li>' for row in db.execute(
        """SELECT c.name,st.title FROM portal_admissions p JOIN streamer_creators c ON c.id=p.creator
           LEFT JOIN streamer_streams st ON st.source=p.source WHERE p.day=? ORDER BY p.first_job""",(date,)))
    if admitted:
        line += f'<details><summary>Admitted today</summary><ul>{admitted}</ul></details>'
    video_slots = production["daily_videos"]-len(sources)
    creator_slots = production["daily_creators"]-len(counts)
    return (f'<p>Local admission day {date} · {len(sources)} of {production["daily_videos"]} videos · '
            f'{len(counts)} of {production["daily_creators"]} creators. '
            f'{max(0,video_slots)} video slots and {max(0,creator_slots)} creator slots remain.</p>{line}')


def schedule_preview(db, root, values, accounts, now=None):
    selected = [a for a in accounts if a["id"] in values["accounts"]]
    if not selected:
        return "<p>Select tested destination accounts to preview a queue.</p>"
    zone = factory_portal.tz(values["zone"])
    current = (now or datetime.now(zone)).astimezone(zone).replace(second=0,microsecond=0)
    window_open,window_close = (time.fromisoformat(values[k]) for k in ("window_start","window_end"))
    cursors = {a["id"]:current for a in selected}
    daily = {}
    rows = []
    groups = set()

    def align(slot):
        wall = slot.time()
        opening = datetime.combine(slot.date(),window_open,zone)
        if window_open<window_close and wall>=window_close:
            return opening+timedelta(days=1)
        if window_open<window_close and wall<window_open or window_close<window_open and window_close<=wall<window_open:
            return opening
        return slot
    entries = db.execute("""SELECT e.*,c.id AS creator,c.name AS creator_name,d.story FROM streamer_editions e
                            JOIN jobs j ON j.id=e.job JOIN streamer_streams st ON st.source=j.source
                            JOIN streamer_accounts a ON a.id=st.account JOIN streamer_creators c ON c.id=a.creator
                            LEFT JOIN done_editions d ON d.edition=e.id
                            WHERE e.render_state='exported' AND e.qualification='qualified' AND e.qa IS NOT NULL
                            AND COALESCE((SELECT verdict FROM streamer_edition_feedback f WHERE f.edition=e.id
                                          ORDER BY id DESC LIMIT 1),'accepted')='accepted'
                            ORDER BY e.rowid""")
    for row in entries:
        if not shorts_release.eligible(db,root,row):
            continue
        plan = json.loads(row["plan"])
        platforms = config.policy(db,row["creator"])["platforms"]
        targets = [a for a in selected if a["platform"] in platforms]
        for account in targets:
            aid = account["id"]
            if row["story"] is not None and any(
                delivery["account"]==aid.casefold() and
                (delivery["state"]!="failed" or delivery["first_submit"])
                for delivery in indiepost.deliveries(db,row["story"])
            ):
                continue
            slot = cursors[aid]
            if values["release"]=="scheduled":
                slot = align(slot)
            if daily.get((aid,slot.date()),0)>=values["account_daily"]:
                slot = datetime.combine(slot.date()+timedelta(days=1),time.min,zone)
                if values["release"]=="scheduled":
                    slot = align(slot)
            daily[(aid,slot.date())] = daily.get((aid,slot.date()),0)+1
            cursors[aid] = slot+timedelta(minutes=values["spacing"])
            groups.add((row["id"],slot))
            title = values["title"] or plan.get("title","Edition")
            caption = values["caption"] or plan.get("post","")
            privacy = indiepost.privacy(values,account["platform"])
            url = f'/streamer/run/{row["job"]}/{row["candidate"]}/edit?edition={row["id"]}'
            rows.append(f'<li><time>{slot.strftime("%d %b %H:%M")}</time> · {h(account["name"])} · '
                        f'<a href="{url}">{h(title)}</a> · {h(privacy)} · No delivery attempt'
                        f'<details><summary>Caption preview</summary><p>{h(caption)}</p></details></li>')
    if not rows:
        return '<p>No current final-QA editions match the selected accounts and creators’ destination policies.</p>'
    peak = max(Counter(slot.date() for _,slot in groups).values())
    limit = '<p>Projected volume exceeds the documented Free daily post group limit.</p>' if peak>100 else ""
    return (f'<p>{len(groups)} local post groups · {len(rows)} account deliveries · Peak {peak} groups in one local day · '
            f'{h(values["zone"])}</p>{limit}<ul>{"".join(rows)}</ul>')


def page(db, root, token, vault_available, query, posted=None):
    import time
    from . import flashdata_profiles
    settings_data,versions = config.current(db)
    account_rows = json.loads(connection_state(db)["accounts"])
    blocks = []
    for key,title in GROUPS.items():
        settings_group = settings_data[key]
        data = dict(settings_group)
        if posted is not None and key==query.get("group"):
            for name,original in data.items():
                if isinstance(original,bool):
                    data[name] = posted.get(name)=="on"
                elif name in posted and name not in ("mode","ids","accounts"):
                    data[name] = posted[name]
            if key=="uploads":
                data["accounts"] = config.upload_accounts(posted)
        fields = (f'<input type="hidden" name="version" value="{versions.get(key,0)}">'
                  '<input type="hidden" name="common" value="on">')
        fields += "".join(f'<div class="control-setting">{field(name,data,account_rows)}'
                          f'<p class="control-help">{h(HELP[name])}</p></div>'
                          for name in config.COMMON_FIELDS[key])
        if posted is not None and key==query.get("group"):
            fields = f'<p role="alert">{h(query.get("message",""))}</p>'+fields
        controls = form(f"/controls/{key}/save",token,fields+button(f"Save {title.lower()}","primary"),"control-fields")
        intro,extra = "",""
        if key=="resources":
            extra = '<p><a href="/storage#scheduler">Buffer scheduler</a></p>'
        if key=="uploads":
            intro = publishing_status(settings_group,account_rows)
        if key=="connections":
            show_checks = query.get("diagnostics")=="1"
            diagnostics = ('<ul>'+"".join(f'<li>{h(name)} · {("Ready" if ready else h(hint))}</li>'
                                           for name,ready,hint in settings.checks())+'</ul>' if show_checks else
                           '<p><a href="/controls?diagnostics=1#connections">Check local tools</a></p>')
            extra = ('<p>Sail receives transcript text and selected JPEG frames. Audio processing stays local.</p>'
                     +connection_actions(db,token,vault_available)+views.provider_cards(db,token,vault_available,settings_group["sail_tier"])
                     +f'<details{" open" if show_checks else ""}><summary>Local tools and advanced diagnostics</summary>{diagnostics}</details>')
        blocks.append(f'<section class="panel operator-panel" id="{key}"><h2>{title}</h2>'
                      f'<p class="meta">Saved version {versions.get(key,0)} · Applies to future admissions or previews.</p>'
                      f'{intro}{controls}{extra}</section>')
    values = flashdata_profiles.settings(db)
    spend = flashdata_profiles.usage(db,time.time())
    fields = []
    for key,label in (("preferred","Preferred video height"),("minimum","Minimum video height"),
                      ("timeout","Attempt wait in seconds"),("attempts","Maximum submissions per interval"),
                      ("budget_cents","Monthly budget in USD cents"),("cache_hours","Metadata freshness in hours")):
        low,high = flashdata_profiles.LIMITS[key]
        fields.append(f'<label>{label}<input type="number" name="{key}" '
                      f'min="{low}" max="{high}" value="{values[key]}" required></label>')
    usage = (f'${spend["credits"]*flashdata_profiles.CREDIT_USD:.4f} recorded · '
             f'${spend["held"]*flashdata_profiles.CREDIT_USD:.4f} reserved · '
             f'{spend["bytes"]/1e9:.3f} GB delivered · {spend["outstanding"]} outstanding')
    controls = form("/controls/flashdata/save",token,"".join(fields)+button("Save FlashData settings","primary"),"control-fields")
    blocks.append(f'<section class="panel operator-panel" id="flashdata"><h2>FlashData</h2><p>{usage}</p>{controls}</section>')
    return ('<div class="hero"><h1>Controls &amp; settings</h1></div><nav class="control-jump" aria-label="Control groups">'
            +"".join(f'<a href="#{key}">{title}</a>' for key,title in GROUPS.items())+'<a href="#flashdata">FlashData</a></nav>'
            +'<div class="operator-controls">'+"".join(blocks)+'</div>')
