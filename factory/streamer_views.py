"""Streamer roster, intake and review screens."""

import json

from . import views, streamer_run, streamer_asr
from .provider_meter import report as costs
from .streamer_catalog import DEFAULTS

h, form, button, pill = views.h, views.form, views.button, views.pill


def field(name, label, value="", attributes=""):
    return f'<label>{h(label)}<input name="{name}" value="{h(value)}" {attributes}></label>'


def roster_page(db, token):
    add = field("name", "Creator name", attributes="required maxlength=100")
    add += field("account", "YouTube account URL", attributes='required type=url placeholder="https://www.youtube.com/@creator"')
    add += field("game", "Game", "GTA V", "required maxlength=100")
    add += button("Add creator", "primary")
    sections = []
    tier_config = json.loads(db.execute("SELECT data FROM settings ORDER BY version DESC LIMIT 1").fetchone()[0])
    for row in db.execute("""SELECT c.*,a.url,a.checked,a.platform_id FROM streamer_creators c
                             JOIN streamer_accounts a ON a.creator=c.id ORDER BY c.name"""):
        cid = row["id"]
        camera = ",".join(map(str, json.loads(row["camera"] or "null") or []))
        post = field("camera", "Facecam crop (normalized x,y,width,height)", camera, 'placeholder="Auto-detect"')
        post += field("credit", "Credit on edits", row["credit"], "required maxlength=100")
        post += '<label><input type=checkbox name=enabled' + (" checked" if row["enabled"] else "") + '> Enabled</label>'
        options = form(f"/streamer/creator/{cid}/edit", token, post+button("Save creator layout"))
        discovery = form(f"/streamer/creator/{cid}/discover", token, button("Discover completed GTA streams"))
        stream = field("link", "Completed YouTube stream URL or absolute recording path", attributes="required")
        stream += field("location", "Local recording override for a new YouTube stream")
        stream += field("chat_path", "Optional YouTube replay chat JSONL absolute path")
        stream += '<label><input type="checkbox" name="archive_upload"> This creator upload is an archived full stream</label>'
        intake = form(f"/streamer/creator/{cid}/attach", token, stream+button("Add stream", "primary"))
        found = []
        for item in db.execute("""SELECT d.* FROM streamer_discovery d
                                  JOIN streamer_accounts a ON a.id=d.account
                                  WHERE a.creator=? ORDER BY d.rowid""", (cid,)):
            link = f'https://www.youtube.com/watch?v={item["video"]}'
            action = f'<input type="hidden" name="link" value="{h(link)}">' + button("Select stream")
            found.append(f'<li>{h(item["title"])}{form(f"/streamer/creator/{cid}/attach", token, action)}</li>')
        cards = []
        for item in db.execute("""SELECT st.*,s.link,s.state FROM streamer_streams st
                                  JOIN streamer_accounts a ON a.id=st.account JOIN sources s ON s.id=st.source
                                  WHERE a.creator=? ORDER BY st.id DESC""", (cid,)):
            source = item["source"]
            names = {"hourly_target": "Distinct qualified reels per full source hour", "editor_minutes":"Maximum editing minutes",
                     "remote_minutes": "Maximum source minutes",
                     "remote_height": "Maximum download height", "remote_mib": "Maximum archive MiB",
                     "reserve_mib": "Minimum free MiB", "asr_threads": "CPU threads"}
            recent = db.execute("""SELECT id, settings FROM jobs WHERE source=?
                                   AND json_extract(settings,'$.method')='streamer' ORDER BY id DESC LIMIT 1""",
                                (source,)).fetchone()
            previous = json.loads(recent["settings"]) if recent else DEFAULTS
            config = "".join(field(k, label, previous.get(k,DEFAULTS[k]),
                                   "type=number required min=7 max=24" if k=="hourly_target" else "type=number required min=1")
                             for k, label in names.items())
            config += '<label>Local ASR model<select name=asr_model><option value=base.en>Whisper Base English</option>'
            config += '<option value=small.en>Whisper Small English</option></select></label>'
            preferred = tier_config.get("sail_tier", "balanced")
            config += '<label>Sail tier<select name=tier>' + "".join(
                f'<option value={tier}{" selected" if tier == preferred else ""}>{tier.title()}</option>'
                for tier in ("balanced", "flex")) + '</select></label>'
            config += field("provider_cap_cents", "Sail reservation cap (US cents)",
                            tier_config.get("provider_cap_cents", 25), "type=number required min=1 max=1000")
            config += '<label><input type=checkbox name=hosted required> Enable Sail analysis for this run</label>'
            start = form(f"/streamer/stream/{source}/start", token, config+button("Archive and find clips", "primary"))
            review = f'<a href="/streamer/run/{recent["id"]}">Open latest run</a>' if recent else ""
            archive = f'<a href="/?source={source}#source-form">Library details</a>'
            cards.append(f"""<article class="source" id="roster-stream-{source}"><h3>{h(item['title'])}</h3>
              <p class="meta">{h(item['live_status'].replace('_',' ').title())} · Full archive retained</p>
              <div class="actions">{review}{archive}</div>
              <details><summary>Production settings</summary>{start}</details></article>""")
        sections.append(f"""<section class="panel"><div class="spread"><h2>{h(row['name'])}</h2>
          {pill('enabled' if row['enabled'] else 'paused')}</div><p class="meta">{h(row['game'])} · YouTube
          · <a href="{h(row['url'])}">{h(row['platform_id'])}</a> · Last discovery {h(row['checked'] or 'pending')}</p>
          <div class="actions">{discovery}</div>{f'<details><summary>Discovered streams ({len(found)})</summary><ul>{"".join(found)}</ul></details>' if found else ''}
          <details><summary>Add a stream or local recording</summary>{intake}</details>
          <details><summary>Creator layout and credit</summary>{options}</details>{''.join(cards)}</section>""")
    return '<div class="hero"><div><h1>Streamer clipping</h1><p>Archive completed streams and review vertical edits.</p></div></div>' + (
        '<section class="panel"><h2>Add a creator</h2>' + form("/streamer/creator/add", token, add) + '</section>') + ''.join(sections)


def run_page(db, root, token, job):
    row = db.execute("SELECT * FROM jobs WHERE id=? AND json_extract(settings,'$.method')='streamer'",
                     (job,)).fetchone()
    if not row:
        raise ValueError("Streamer run not found.")
    config = json.loads(row["settings"])
    path = streamer_run.directory(root, job)
    intro = f"""<div class="hero"><div><h1>{h(config['stream']['title'])}</h1>
      <p>{h(config['stream']['name'])} · Run {job} · Sail {h(config['sail_tier'].title())}</p>
      <p role="status">{h(row['progress'])}</p><p>{h(row['error'])}</p></div>
      <div class="actions"><a class="button" href="/streamer/run/{job}">Refresh</a>
      <a class="button" href="/factory?attempt={job}">Back to Factory stream</a></div></div>"""
    if not (path / "selection.json").is_file():
        return intro + f"<p>{h(row['stage'])} · {h(row['state'])}</p>"
    data = streamer_asr.read(path / "selection.json")
    if "hourly_target" in config:
        return intro + automatic_page(db,job,data)
    cards, rest, kept = [], [], 0
    for candidate in data["proposals"]:
        cid = candidate["id"]
        review_path = path / cid / "review.json"
        saved = streamer_asr.read(review_path) if review_path.is_file() else {}
        decision = saved.get("decision", "pending")
        kept += decision == "accepted"
        editor = f'<a href="/streamer/run/{job}/{cid}/edit">Edit versions</a>' if candidate.get("evidence") else ""
        if cid not in data["shortlist"] and cid != data["comparison"]:
            rest.append(f'<li>{h(cid)} · {h(candidate.get("hook") or candidate.get("reason", ""))} · {h(candidate["rejection"])} {editor}</li>')
            continue
        video = f"/streamer/run/{job}/{cid}"
        portrait = path / cid / "portrait.mp4"
        src = video + ("/portrait.mp4" if portrait.is_file() else "/source-context.mp4")
        context = (f'<a href="{video}/source-context.mp4">Play surrounding source footage</a>'
                   if (path / cid / "source-context.mp4").is_file() else "")
        choice = '<label>Decision<select name="decision">' + "".join(
            f'<option value="{value}"{" selected" if decision == value else ""}>{label}</option>'
            for value, label in (("rejected", "Reject"), ("accepted", "Accept"))
            if value != "accepted" or portrait.is_file()) + '</select></label>'
        choice += f'<label>Observed evidence<textarea name="reason" required>{h(saved.get("reason",""))}</textarea></label>'
        review = form(f"/streamer/run/{job}/{cid}/review", token, choice+button("Save clip decision","primary"))
        brief = "".join(f'<dt>{label}</dt><dd>{h(candidate[key])}</dd>' for key,label in (
            ("setup","Setup"), ("game_evidence","Gameplay check"), ("reaction","Reaction"),
            ("payoff","Payoff"), ("why_share","Why share"), ("post","Post copy")) if key in candidate)
        words = "".join(f'<li>{item["time"][0]:.2f}s · {h(item["quote"])}</li>' for item in candidate["evidence"])
        edition = streamer_asr.read(path / cid / "edition.json") if (path / cid / "edition.json").is_file() else {}
        cards.append((cid, f"""<article class="panel clip"><video controls playsinline preload="metadata"
          aria-label="{h(cid)}" src="{src}"></video><div class="clip-data">
          <div class="spread"><h2>{h(candidate.get('hook') or candidate.get('reason') or cid)}</h2>{pill(decision)}</div><p class="meta">{h(cid)} ·
          {views.timecode(candidate['start'])}–{views.timecode(candidate['end'])} · {h(edition.get('layout','Source only'))}</p>
          <p>{h(candidate['rejection'])}</p><dl>{brief}</dl><ul>{words}</ul><div class="actions">{context}{editor}
          {f'<a href="{video}/words.ass" download>Word captions</a>' if portrait.is_file() else ''}</div>
          {review}</div></article>"""))
    usage = costs(db,job)["provider_usage"]
    estimate = usage["known_estimate_usd"]
    estimate_label = "unavailable" if estimate is None else f"${estimate:.4f}"
    summary = (f'<p>{kept} verified clips / {data["target"]} target · {len(data["shortlist"])} rendered proposals · '
               f'{data["transcript_words"]} words · {len(data["chapters"])} chapters · Chat {h(data["chat"]["status"])}</p>'
               f'<p class="meta">Archive {data["asset"]["bytes"]:,} bytes · Sail {usage["submission_attempts"]} submissions · '
               f'Reserved ${usage["reserved_usd"]:.4f} · Estimated '
               f'{estimate_label}</p><div class="actions">'
               f'<a href="/streamer/run/{job}/archive">Play archive</a>'
               f'<a href="/streamer/run/{job}/transcript.json" download>Word transcript</a>'
               f'<a href="/streamer/run/{job}/selection.json" download>Selection report</a></div>')
    card_map = dict(cards)
    ordering = data["shortlist"] + ([data["comparison"]] if data["comparison"] else [])
    return intro + summary + "".join(card_map[c] for c in ordering if c in card_map) + (
        f'<details class="panel"><summary>Other held-back proposals ({len(rest)})</summary><ul>{"".join(rest)}</ul></details>')


def automatic_page(db,job,data):
    by_id = {item["id"]:item for item in data["proposals"]}
    rows = db.execute("""SELECT id,candidate,qualification,render_state,qa FROM streamer_editions
                         WHERE job=? AND render_state='exported' ORDER BY rowid""",(job,)).fetchall()
    base = f"/streamer/run/{job}"
    cards = []
    for row in rows:
        identity = row["candidate"]
        version = row["id"]
        candidate = by_id[identity]
        artifact = f"{base}/{identity}/edition/{version}/portrait.mp4"
        url = f"{base}/{identity}/edit?edition={version}"
        cards.append(f'<article class="panel clip"><video playsinline controls preload="none" src="{artifact}" '
                     f'aria-label="{h(identity)}"></video><div class="clip-data">'
                     f'<h2>{h(candidate.get("hook") or candidate.get("reason") or identity)}</h2>'
                     f'<p>{h(row["qualification"].title())} · {h(identity)}</p>'
                     f'<a href="{url}">Review version, shots and QC</a></div></article>')
    archive = (f'<a href="/factory?attempt={job}#source-ranges-{job}">Acquired source ranges</a>'
               if data["asset"].get("kind")=="audio-first" else f'<a href="{base}/archive">Play archive</a>')
    return (f'<section class="panel"><h2>Automatic editions</h2><p><a href="/factory/run/{job}">'
            'Open hourly funnel and events</a></p>'
            f'<div class="actions">{archive}'
            f'<a href="{base}/transcript.json">Word transcript</a></div>'
            + ("".join(cards) or "<p>Exports appear here as the worker completes them.</p>") + "</section>")
