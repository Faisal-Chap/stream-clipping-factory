"""Server-rendered operator screens. All user content is HTML-escaped."""

import json
from html import escape

from . import settings
from .report import job_report


def h(value):
    return escape(str(value), quote=True)


def timecode(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def page(title, active, body, message="", stream=None, attempt=None):
    tabs = [("/sources","Sources"),("/jobs","Jobs"),("/storage","Storage"),
            ("/controls","Controls &amp; settings"),("/templates","Edit templates")]
    nav = "".join(f'<a href="{url}"{" aria-current=page" if active == url else ""}>{label}</a>'
                  for url,label in tabs)
    desktop = f'<nav class="workspace-tabs" aria-label="Workspace">{nav}</nav>'
    notice = f'<p class="notice" role="status">{h(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · Stream Clipping Factory</title><link rel="stylesheet" href="/style.css">
<script src="/app.js" defer></script></head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="shell"><a class="wordmark" href="/sources"><span class="mark">VI</span><span>Media Factory</span></a>
{desktop}<span class="local"><i></i> Local workspace</span></header>
<main id="main">{notice}{body}</main></body></html>"""


def form(action, token, body, css=""):
    return (f'<form method="post" action="{action}" class="{css}">'
            f'<input type="hidden" name="csrf" value="{token}">{body}</form>')


def button(label, css="", attrs=""):
    return f'<button class="{css}" {attrs}>{label}</button>'


def pill(label):
    return f'<span class="pill {h(label.lower())}">{h(label.title())}</span>'


def count_label(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def library(db, token, source_id, input_values=None, *, detail_only=False):
    source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone() if source_id else None
    fields = input_values if input_values is not None else (dict(source) if source else {})

    def field(name, label, placeholder="", extra=""):
        return (f'<label>{label}<input name="{name}" value="{h(fields.get(name, ""))}" '
                f'placeholder="{placeholder}" maxlength="4096" {extra}></label>')

    source_form = field("link", "Source link or file path", "https://www.youtube.com/watch?v=…", "required")
    source_form += '<div class="pairs">' + field("creator", "Creator") + field("game", "Game", "Gaming") + '</div>'
    source_form += field("location", "Media attachment", "Direct video URL or absolute file path")
    source_form += field("priority", "Priority (1 runs first)", "100", 'type=number min=1 max=9999')
    source_form += f'<label>Notes<textarea name="notes" maxlength="4096">{h(fields.get("notes", ""))}</textarea></label>'
    source_form += '<div class="actions">' + button("Save changes" if source else "Add source", "primary") + (
        '<a href="/sources#unassigned">Back to unassigned recordings</a>' if source else "") + '</div>'
    if detail_only:
        action = f"/source/{source_id}/edit" if source else "/source/add"
        return ('<section class="panel operator-panel" id="source-form"><h2>Source details</h2>'
                +form(action,token,source_form)+'</section>')
    rows = []
    for row in db.execute("""SELECT s.*, (SELECT COUNT(*) FROM stories t WHERE t.source=s.id
                          AND t.preview IS NOT NULL) AS clips,
                          (SELECT error FROM jobs j WHERE j.source=s.id AND j.revision=s.revision
                           ORDER BY id DESC LIMIT 1) AS latest_error
                          FROM sources s ORDER BY (s.state='archived'), s.priority,s.id"""):
        sid, state = row["id"], row["state"]
        title = row["creator"] or row["link"]
        actions = f'<a href="/sources?source={sid}#source-form">Edit</a>'
        if state == "queued":
            actions += form(f"/source/{sid}/start", token, button("Start", "primary"))
            actions += form(f"/source/{sid}/pause", token, button("Pause", "subtle"))
        else:
            actions += form(f"/source/{sid}/resume", token, button("Resume source"))
        if state != "archived":
            actions += form(f"/source/{sid}/archive", token, button("Archive", "subtle"))
        clip_link = (f'<a href="/factory?stream={sid}&amp;review=unreviewed#legacy-work">{row["clips"]} previews</a>'
                     if row["clips"] else "0 previews")
        link = f'<div class="source-link">{h(row["link"])}</div>'
        error = f'<p class="error">{h(row["latest_error"])}</p>' if row["latest_error"] else ""
        rows.append(f"""<article class="source">
            <div class="spread"><strong>{h(title)}</strong>{pill(state)}</div>
            {link}<div class="meta">{h(row['game'] or 'Game unspecified')} · Priority {row['priority']} · {clip_link}</div>
            {error}<div class="actions">{actions}</div></article>""")
    listing = "".join(rows) or '<p class="empty">Add a source or import the starter table to begin.</p>'
    intake = form("/source/import", token, button("Import starter table"))
    bulk = form("/source/bulk", token, '<label>One URL or file path per line'
                '<textarea name="links" maxlength="32000" required></textarea></label>' + button("Add sources", "primary"))
    return f"""<div class="hero"><div><h1>Source library</h1>
        <p>Add recordings, set priority and start a local clipping run.</p></div>{intake}</div>
        <div class="columns"><section class="panel"><div class="section-title"><h2>Recordings</h2>
        <span>{count_label(len(rows), 'source')}</span></div>{listing}</section>
        <section class="panel editor" id="source-form"><h2>{'Edit source' if source else 'Add a source'}</h2>
        {form(f'/source/{source_id}/edit' if source else '/source/add', token, source_form)}
        <details><summary>Add several sources</summary>{bulk}</details></section></div>"""


def jobs(db, token):
    rows = list(db.execute("""SELECT j.*,s.state AS source_state,s.revision AS current
                             FROM jobs j JOIN sources s ON s.id=j.source ORDER BY j.id DESC"""))
    cards = []
    for row in rows:
        job, state = row["id"], row["state"]
        source = json.loads(row["snapshot"])
        config = json.loads(row["settings"])
        title = source["creator"] or f"Source {row['source']}"
        action = ""
        if state in ("running", "queued") and not row["stop"]:
            action = form(f"/job/{job}/stop", token, button("Stop after this step"))
        is_streamer = config["method"] == "streamer"
        if state in ("failed", "stopped") and row["source_state"] == "queued" and row["current"] == row["revision"]:
            action = form(f"/job/{job}/resume", token, button("Retry" if state == "failed" else "Resume job", "primary"))
            if not is_streamer:
                action += form(f"/job/{job}/restart", token, button("New run with current settings"))
        error = f'<p class="error">{h(row["error"])}</p>' if row["error"] else ""
        clips = (f'<a href="/streamer/run/{job}">Open streamer run</a>' if is_streamer else
                 f'<a href="/review?job={job}">Open review</a>' if row["analysis"] else "")
        cards.append(f"""<article class="source"><div class="spread"><strong>{h(title)} · Job {job}</strong>{pill(state)}</div>
          <div class="meta">{h(row['stage'])} · {h(config['method'].title())} · Attempt {row['attempt']}</div>
          <p>{h('Stop requested. ' if row['stop'] else '')}{h(row['progress'] or 'Waiting for worker')}</p>{error}
          <div class="actions">{action}{clips}<a href="/?source={row['source']}#source-form">Source details</a>
          <a href="/report/{job}" download>Export report</a></div></article>""")
    active = any(row["state"] in ("queued", "running") for row in rows)
    live = ' data-refresh="true"' if active else ""
    listing = "".join(cards) or '<p class="empty">Start a recording from the Library to create your first job.</p>'
    return f"""<div class="hero"><div><h1>Jobs</h1><p>Track acquisition, analysis and preview rendering.</p></div>
        <a class="button" href="/jobs">Refresh</a></div><section class="panel"{live}>
        <div class="section-title"><h2>Job history</h2><span>{count_label(len(rows), 'job')}</span></div>{listing}</section>"""


def story_card(story, info, token):
    clip, rank, plan = story["id"], story["rank"], story["plan"]
    checks = "".join(f'<button type="button" class="evidence" data-seek="{stamp-plan["start"]:.3f}">'
                     f'<img loading="lazy" src="/media/{clip}/frame/{i}" alt="">'
                     f'{timecode(stamp)}</button>' for i, stamp in enumerate(plan["evidence"]))
    labels = "".join(f'<li>{timecode(label["t"])} · {h(label["label"])} · margin {label["margin"]:.3f}</li>'
                     for label in plan["labels"])
    review_fields = "<label>Decision<select name=decision>" + "".join(
        f'<option value="{value}"{" selected" if story["decision"] == value else ""}>{caption}</option>'
        for value, caption in (("pending", "Pending"), ("accepted", "Accept"), ("rejected", "Reject"))
    ) + "</select></label>"
    review_fields += (f'<label>Review reason<textarea name="reason" maxlength="4096">{h(story["reason"])}</textarea></label>'
                      + button("Save review", "primary"))
    review_fields += f'<input type="hidden" name="job" value="{info["job"]}">'
    review_form = form(f"/review/{clip}/save", token, review_fields)
    context = ""
    if plan.get("judgment_version"):
        cues = "".join(f'<li>{timecode(cue["start"])}–{timecode(cue["end"])} · {h(cue["text"])}</li>'
                       for cue in plan["speech"])
        context = f"""<p class="meta">{h(plan['confidence'])} · {h(plan['speech_status'])}</p>
            <p>{h(plan.get('change') or '')}</p>{f'<ul>{cues}</ul>' if cues else ''}
            <div class="actions"><a href="/media/{clip}/context" target="_blank" rel="noopener">Play source context</a>
            <a href="/media/{clip}/alternate" target="_blank" rel="noopener">Play center-crop version</a></div>"""
    return f"""<article class="panel clip"><video controls playsinline preload="metadata"
      aria-label="Preview {clip}" src="{story['preview']}"></video><div class="clip-data">
      <div class="spread"><h2>Candidate {rank}</h2>{pill(story['decision'])}</div>
      <p class="meta">{h(info['creator'] or 'Creator unspecified')} · {h(info['game'] or 'Game unspecified')}
      · {timecode(plan['start'])}–{timecode(plan['end'])} · Signal score {plan['score']:.3f}</p>
      <dl><dt>Hook</dt><dd>{h(plan['hook'])}</dd><dt>Setup</dt><dd>{h(plan['setup'])}</dd>
      <dt>Payoff</dt><dd>{h(plan['payoff'])}</dd><dt>Selection</dt><dd>{h(plan['selection'])}</dd>
      <dt>Framing</dt><dd>{h(plan['framing'])}</dd><dt>Audio</dt><dd>{h(plan['audio'])}</dd>
      <dt>Narration</dt><dd>{h(plan['narration'])}</dd></dl>
      <div class="evidence-frames" aria-label="Evidence timestamps">{checks}</div>
      {f'<ul class="labels">{labels}</ul>' if labels else ''}
      {context}{review_form}</div></article>"""


def review(db, token, job_id=None, source_id=None):
    if job_id is None:
        query = "SELECT id FROM jobs WHERE analysis IS NOT NULL"
        if source_id:
            query += " AND source=?"
        row = db.execute(query + " ORDER BY id DESC LIMIT 1", (source_id,) if source_id else ()).fetchone()
        job_id = row["id"] if row else None
    info = job_report(db, job_id, include_rejected=False)
    intro = '<div class="hero"><div><h1>Clip review</h1><p>Play candidates and record an editorial decision.</p></div>'
    if not info:
        return intro + '</div><div class="panel empty">Preview candidates appear here after analysis.</div>'
    intro += f'<a class="button" href="/report/{job_id}" download>Export ranked report</a></div>'
    if info["state"] == "complete" and info["settings"]["method"] in ("baseline", "semantic"):
        intro += form(f'/compare/context/{info["source"]}', token, button("Run local context comparison"))
    rejected = "".join(f'<li>{timecode(r["start"])}–{timecode(r["end"])} · {r["score"]:.3f} · {h(r["rejection"])}</li>'
                       for r in info["rejected"])
    shortlist = "".join(story_card(story, info, token) for story in info["stories"])
    if not shortlist:
        shortlist = f'<div class="panel empty">{h(info["error"] or info["empty_reason"] or info["progress"])}</div>'
    prior = "".join(f'<a href="/factory?attempt={row["id"]}&amp;review=unreviewed#legacy-work">Job {row["id"]} · {h(row["method"].title())}</a>'
                    for row in db.execute("""SELECT id,json_extract(settings,'$.method') AS method FROM jobs
                          WHERE source=? AND analysis IS NOT NULL AND id<>? ORDER BY id DESC""",
                          (info["source"], job_id)))
    return (intro + f'<p class="meta">Job {job_id} · {h(info["settings"]["method"].title())} · '
            f'{h(info["samples"] or 0)} timeline samples · {h(info["state"].title())}</p>'
            + (f'<div class="actions related">{prior}</div>' if prior else "") + shortlist
            + (f'<details class="panel rejected"><summary>Windows held back ({info["rejected_count"]})</summary>'
               f'<ul>{rejected}</ul><p>Export the report for every ranked window.</p></details>' if rejected else ""))


def provider_cards(db, token, vault_available, tier):
    from .provider_connections import NAMES
    from .provider_api import GOOGLE_MODEL, SAIL_MODEL, TIERS

    cards = []
    for row in db.execute("SELECT * FROM provider_connections ORDER BY provider"):
        provider = row["provider"]
        pending = db.execute("SELECT stage,tier FROM provider_probes WHERE provider=? AND state='queued'",
                             (provider,)).fetchone()
        saved = bool(row["reference"])
        state = ("Setup required" if not vault_available else "Test queued" if pending
                 else "Verified" if row["verified"] else "Saved, untested" if saved else "Awaiting key")
        capability = "".join(f'<li>{h(result["tier"].title() or "Embeddings")} · {h(result["capability"])}'
                             f' · {h(result["verified"])} UTC</li>' for result in db.execute(
                                 "SELECT * FROM provider_capabilities WHERE provider=? AND reference=? ORDER BY tier",
                                 (provider, row["reference"])))
        usage = ("Text and video embeddings for hosted retrieval." if provider == "google"
                 else "Context judging via Balanced or Flex. Image capability is tested separately per tier.")
        status = f'<p class="error">{h(row["error"])}</p>' if row["error"] else ""
        if pending:
            status += f'<p role="status">Testing {h(pending["tier"] or "Google")} · {h(pending["stage"])}</p>'
        fields = f'<input type="hidden" name="revision" value="{row["revision"]}">'
        tier_field = ""
        if provider == "sail":
            tier_field = '<label>Test tier<select name="tier">' + "".join(
                f'<option value="{value}"{" selected" if value == tier else ""}>{value.title()}</option>'
                for value in TIERS) + '</select></label>'
        actions = ""
        if vault_available and pending:
            actions = form(f"/provider/{provider}/cancel", token, fields + button("Cancel local test", "subtle"))
        if vault_available and not pending:
            password = (f'<label>{h(NAMES[provider])} API key<input name="key" type="password" maxlength="512" '
                        'required autocomplete="off" spellcheck="false"></label>')
            op = "replace" if saved else "add"
            actions = form(f"/provider/{provider}/{op}", token, fields + (tier_field if saved else "")
                           + password + button("Replace key" if saved else "Add key", "primary"))
            if saved:
                actions += form(f"/provider/{provider}/test", token, fields + tier_field + button("Test key"))
                actions += form(f"/provider/{provider}/remove", token, fields + button("Remove local key", "subtle"))
        cards.append(f"""<section class="source" aria-label="{h(NAMES[provider])}">
            <div class="spread"><h3>{h(NAMES[provider])}</h3>{pill(state)}</div>
            <p class="meta">{h(GOOGLE_MODEL if provider == "google" else SAIL_MODEL)}</p><p>{h(usage)}</p>
            {status}{f'<ul>{capability}</ul>' if capability else ''}{actions}</section>""")
    return ("""<section class="panel"><div class="section-title"><h2>Provider connections</h2></div>
        <p class="provider-note">Keys stay in the local factory credential store. Tests use synthetic evidence
        and may incur a small charge. Local rankers run without keys.</p>""" + "".join(cards) + "</section>")


def settings_page(db, token, vault_available=False):
    config = settings.get(db)
    dependencies = "".join(f'<div class="source"><div class="spread"><strong>{h(name)}</strong>'
                           f'{pill("ready" if ready else "setup required")}</div>'
                           f'{"" if ready else f"<p>{h(hint)}</p>"}</div>'
                           for name, ready, hint in settings.checks())
    fields = f'<input type="hidden" name="version" value="{config["version"]}"><label>Ranker<select name="method">'
    for method, caption in settings.METHODS.items():
        fields += f'<option value="{method}"{" selected" if config["method"] == method else ""}>{caption}</option>'
    fields += (f'</select></label><label>Max previews per job<input type="number" min="1" max="10" '
               f'name="max_previews" value="{config["max_previews"]}" required></label>'
               '<h2>Download limits</h2><label>YouTube resolution limit<select name="remote_height">')
    for height in (144, 240, 360, 480, 720):
        fields += f'<option value="{height}"{" selected" if height == config["remote_height"] else ""}>{height}p</option>'
    fields += (f'</select></label><label>Remote video length limit (minutes)<input type="number" min="1" max="180" '
               f'name="remote_minutes" value="{config["remote_minutes"]}" required></label>'
               f'<label>Remote download limit (MiB)<input type="number" min="10" max="2048" '
               f'name="remote_mib" value="{config["remote_mib"]}" required></label>'
               '<p class="meta">Verified masters and local attachments can be reused without a download.</p>'
               f'<h2>Hosted analysis</h2><label class="toggle"><input type="checkbox" name="hosted"'
               f'{" checked" if config["hosted"] else ""}> Allow selected hosted rankers to send source evidence</label>'
               '<label>Sail job tier<select name="sail_tier">' + "".join(
                   f'<option value="{tier}"{" selected" if config["sail_tier"] == tier else ""}>{tier.title()}</option>'
                   for tier in ("balanced", "flex")) + '</select></label>'
               f'<label>Provider spend cap per job (US cents)<input type="number" min="1" max="1000" required '
               f'name="provider_cap_cents" value="{config["provider_cap_cents"]}"></label>'
               + button("Save settings", "primary"))
    return f"""<div class="hero"><div><h1>Settings</h1><p>Choose how new jobs rank and render candidates.</p></div></div>
        <div class="columns"><div>{provider_cards(db, token, vault_available, config["sail_tier"])}
        <section class="panel tools-panel"><div class="section-title"><h2>Local tools</h2></div>{dependencies}
        </section></div>
        <section class="panel editor"><h2>Job defaults</h2><p class="meta">Existing jobs keep their saved settings.</p>
        {form('/settings/save', token, fields)}</section></div>"""


def compare(db, token, pair):
    from . import blind_review

    intro = '<div class="hero"><div><h1>Blind comparison</h1><p>Assess the cuts before revealing the rankers.</p></div></div>'
    if not pair:
        labels = []
        for job in db.execute("SELECT id,snapshot,settings FROM jobs WHERE state='complete' ORDER BY id DESC"):
            snapshot, config = json.loads(job["snapshot"]), json.loads(job["settings"])
            name = settings.METHODS.get(config["method"], config["method"])
            labels.append(f'<option value="{job["id"]}">Job {job["id"]} · {h(snapshot["creator"])} · {h(name)}</option>')
        options = "".join(labels)
        fields = (f'<label>First job<select name="first">{options}</select></label>'
                  f'<label>Second job<select name="second">{options}</select></label>' + button("Start blind review", "primary"))
        return intro + '<section class="panel editor">' + form("/compare/create", token, fields) + "</section>"
    rows = blind_review.group(db, pair)
    index = next(idx for idx, row in enumerate(rows) if row["id"] == pair)
    row = rows[index]
    seen = sum(bool(other["reviewed"]) for other in rows)
    old = db.execute("SELECT * FROM blind_assessments WHERE pair=?", (pair,)).fetchone()
    done = (f'<p><a href="/blind/{pair}/results">Reveal assignments and export assessments</a></p>'
            if seen == len(rows) else "")

    def side(letter):
        upper = letter.upper()
        exists = row[f"story_{letter}"] is not None
        play = (f'<video class="blind-video" controls playsinline preload="metadata" '
                f'aria-label="Cut {upper}" src="/blind/{pair}/{letter}"></video>' if exists else '<p>No clip emitted.</p>')
        inputs = (f'<label>Opening question {upper}<textarea name="opening_{letter}">'
                  f'{h(old[f"opening_{letter}"] if old else "")}</textarea></label>'
                  f'<label>Payoff {upper}<textarea name="payoff_{letter}">'
                  f'{h(old[f"payoff_{letter}"] if old else "")}</textarea></label>')
        for name, caption in (("phone", "Legible on phone"), ("usable", "Usable story")):
            checked = " checked" if old and old[f"{name}_{letter}"] else ""
            inputs += (f'<label class="toggle"><input type="checkbox" name="{name}_{letter}"{checked}'
                       f'{" disabled" if not exists else ""}> {caption} {upper}</label>')
        return f'<section class="panel editor"><h2>Cut {upper}</h2>{play}{inputs}</section>'

    choices = "".join(f'<option value="{value}"{" selected" if row["verdict"] == value else ""}>{caption}</option>'
                      for value, caption in (("a", "A is stronger"), ("b", "B is stronger"),
                                             ("tie", "Tie"), ("weak", "Both weak")))
    reasons = "".join(f'<option value="{value}"{" selected" if row["reason"] == value else ""}>{caption}</option>'
                      for value, caption in blind_review.REASONS.items())
    fields = (f'<div class="columns blind">{side("a")}{side("b")}</div><div class="panel editor tools-panel">'
              f'<label>Verdict<select name="verdict" required><option value="">Choose</option>{choices}</select></label>'
              f'<label>Main reason<select name="reason" required><option value="">Choose</option>{reasons}</select></label>'
              f'<label>Notes<textarea name="note">{h(row["note"] or "")}</textarea></label>'
              + button("Save assessment", "primary") + "</div>")
    return intro + f'<p class="meta">Pair {index+1} of {len(rows)} · {seen} reviewed</p>{done}' + form(
        f"/compare/{pair}", token, fields)
