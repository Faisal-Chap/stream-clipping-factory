"""Clip review and source timeline editing for immutable portrait versions."""

import json
import secrets

from . import shorts_release, streamer_asr, streamer_edits, views
from .streamer_views import field

h, form, button, pill = views.h, views.form, views.button, views.pill


def draft(original, duration, setting, game):
    setup, payoff = original["evidence"][0], original["evidence"][-1]
    suggested_start = max(original["start"], setup["time"][0]-.45)
    suggested_end = min(duration, max(payoff["time"][1]+.15, suggested_start+3.1))
    # Suggest a picture change at the recorded reaction without cutting any unreviewed source audio.
    cuts = f"{suggested_start:.3f} | {suggested_end:.3f} | wide |"
    quoted = setup["quote"].strip()
    hook = quoted if 1 <= len(quoted) <= 55 else "A moment from the stream"
    return {"title": "Source draft", "hook": hook, "context": game[:45],
            "post": f"From {setting['credit']}'s {game} stream."[:400],
            "cuts": cuts, "crossfade": "0.04", "decision": "candidate",
            "rationale": "Suggested from the saved transcript. Check the picture and audio before qualification.",
            "captions": "", "beats": ""}


def form_values(plan):
    values = dict(plan)
    values["decision"] = dict(rejected="reject", qualified="qualify").get(plan["decision"], "candidate")
    values["cuts"] = "\n".join(f"{c['start']:.3f} | {c['end']:.3f} | {c['layout']} | "
                               + "; ".join(",".join(str(v) for v in k) for k in c["keys"])
                               + (f" | {c['time_cue']}" if c.get("time_cue") else "")
                               for c in plan.get("cuts", []))
    values["captions"] = "\n".join(f"{c['start']} | {c['end']} | {c['text']} | {c['emphasis']}"
                                   for c in plan.get("captions", []))
    beats = plan.get("beats", {})
    values["beats"] = ", ".join(str(beats[k]["source"]) for k in ("premise", "action", "payoff")) if beats else ""
    return values


def page(db, root, token, job, candidate, identity, posted):
    original, selection, _, setting = streamer_edits.source(root, db, job, candidate)
    game = db.execute("SELECT s.game FROM sources s JOIN jobs j ON j.source=s.id WHERE j.id=?", (job,)).fetchone()[0]
    prefix = f"/streamer/run/{job}"
    edit_url = f"{prefix}/{candidate}/edit"
    entries = list(db.execute("SELECT * FROM streamer_editions WHERE job=? AND candidate=? ORDER BY rowid DESC",
                              (job,candidate)))
    active = next((r for r in entries if r["id"] == identity), None)
    original_url = f"{prefix}/{candidate}/portrait.mp4"
    if not (root/"streamer-runs"/str(job)/candidate/"portrait.mp4").is_file():
        original_url = f"{prefix}/{candidate}/source-context.mp4"
        if not (root/"streamer-runs"/str(job)/candidate/"source-context.mp4").is_file():
            original_url = ""
    plan = json.loads(active["plan"]) if active else None
    values = posted if posted is not None else (
        form_values(plan) if plan else draft(original, selection["asset"]["duration"], setting, game))
    ranged = selection["asset"].get("kind")=="audio-first"
    if ranged and posted is None and not plan:
        if shots := original.get("shots"):
            values["cuts"] = "\n".join(f"{a:.3f} | {b:.3f} | wide |"+(" | Later" if i else "")
                                       for i,(a,b) in enumerate(shots))
        values["crossfade"] = 0
    camera = (plan or {}).get("camera")
    camera_text = ", ".join(str(v) for v in camera) if camera else ""
    game_text = ", ".join(str(v) for v in (plan or {}).get("game") or [])
    hud_text = ", ".join(str(v) for v in (plan or {}).get("hud") or [])
    original_play = (f'<div><h2>Original</h2><video class="phone-edit" controls playsinline '
                     f'preload="none" src="{original_url}" aria-label="Original portrait"></video></div>') if original_url else ""
    media_panel, status = "", ""
    if active:
        stage = active["render_state"]
        feedback = list(db.execute("SELECT * FROM streamer_edition_feedback WHERE edition=? ORDER BY id DESC",
                                   (identity,)))
        release = shorts_release.inspect(shorts_release.portrait_path(root, active)) if stage == "exported" else None
        restriction = shorts_release.restriction(db, identity) if release else ""
        eligible = bool(release and release["passed"] and not restriction)
        publishable = (eligible and active["qualification"]=="qualified" and bool(active["qa"])
                       and (not feedback or feedback[0]["verdict"]=="accepted"))
        status = (f'<section class="panel"><h2>{h(plan["title"])}</h2>'
                  f'<p role="status">{h(active["qualification"].title())} · {h(stage.title())}'
                  f'{" · Publishable" if publishable else ""}</p><p>{h(plan["rationale"])}</p>')
        if style := plan.get("planned_style"):
            decoded = db.execute("SELECT 1 FROM done_editions WHERE edition=?",(identity,)).fetchone()
            description = "Rendered style" if decoded else "Planned style"
            status += (f'<p>{description} · <a href="/templates?design={h(style["template"])}&amp;revision={style["revision"]}">'
                       f'{h(style["name"])} · Revision {style["revision"]}</a></p>')
        if stage == "exported":
            if release:
                status += f'<p>{h(shorts_release.describe(release))}</p>'
            if restriction:
                status += f'<p>Release hold · {h(restriction)}</p>'
            url = f"{prefix}/{candidate}/edition/{identity}"
            manifest = streamer_edits.directory(root, job, candidate, identity) / "manifest.json"
            if manifest.is_file():
                result = streamer_asr.read(manifest)
                layouts = " → ".join(shot["layout"].title() for shot in result["shots"])
                status += f'<p>Rendered shots · {h(layouts)}</p>'
                if result["source_size"][1] < 720:
                    status += f'<p class="meta">Source rendition · {result["source_size"][1]}p. Enlarged details remain source-limited.</p>'
                if result.get("panels"):
                    status += '<p>Standard split · 35% streamer · 65% gameplay</p>'
                    smallest = min((shot["camera_source_pixels"] for shot in result["shots"]),key=lambda p:p[0]*p[1])
                    status += f'<p class="meta">Smallest source camera crop · {smallest[0]}×{smallest[1]} px.</p>'
                for planned, rendered in zip(plan["cuts"], result["shots"]):
                    if planned["layout"] != rendered["layout"]:
                        status += f'<p class="meta">{h(rendered["note"])}</p>'
            if manifest.with_name("qc.json").is_file():
                qc = streamer_asr.read(manifest.with_name("qc.json"))
                status += f'<p>Automatic QC · {"Clear" if qc["passed"] else "Rework"}</p>'
                status += "".join(f'<p class="meta">{h(note)}</p>' for note in qc["issues"]+qc["warnings"])
            media_panel = (f'<div><h2>Version</h2><video class="phone-edit" controls playsinline preload="none" '
                           f'src="{url}/portrait.mp4" aria-label="Version portrait"></video>'
                           f'<div class="actions"><a href="{url}/portrait.mp4">Open MP4</a>'
                           f'<a href="{url}/captions.ass">Captions</a><a href="{url}/manifest.json">Edition manifest</a></div></div>')
            if plan["beats"]:
                status += "<p>" + " · ".join(f"{name.title()} +{plan['beats'][name]['edit']:.2f}s"
                                             for name in ("premise","action","payoff")) + "</p>"
            status += f'<p>{h(plan["post"])}</p>'
            if original_url.endswith("/portrait.mp4"):
                ballot_count = db.execute("SELECT count(*) FROM streamer_edition_ballots WHERE edition=?",
                                          (identity,)).fetchone()[0]
                if ballot_count:
                    status += (f'<details><summary>Comparison history · {ballot_count} ballots</summary>'
                               f'<a href="{prefix}/{candidate}/blind/{identity}">Open saved comparison evidence</a></details>')
            if publishable:
                status += f'<p>{h(active["qa"])}</p>'
            elif eligible and not active["qa"] and active["qualification"] == "qualified" and (not feedback or feedback[0]["verdict"]=="accepted"):
                status += form(f"{edit_url}/{identity}/qa", token,
                               '<label>Picture, audio and caption observations<textarea name="reason" required></textarea></label>'
                               + button("Confirm final QA"))
            if feedback:
                status += '<ul>' + "".join(f'<li>{h(item["verdict"])} · {h(item["reason"])}</li>' for item in feedback) + '</ul>'
            status += form(f"{edit_url}/{identity}/feedback",token,
                           '<label>Independent review<select name="verdict"><option value="accepted">Accept</option>'
                           '<option value="rework">Request rework</option><option value="rejected">Reject</option></select></label>'
                           '<label>Observed premise, payoff and playback quality<textarea name="reason" required></textarea></label>'
                           + "".join(f'<label><input type="checkbox" name="{key}"> {h(label)}</label>'
                                     for key,label in streamer_edits.REVIEW_CHECKS)
                           + button("Save edition review"))
        if stage == "failed":
            status += f'<p>{h(active["error"])}</p>' + form(f"{edit_url}/{identity}/retry", token, button("Retry export"))
        if stage in ("queued","rendering"):
            status += f'<a href="{edit_url}?edition={identity}">Check export progress</a>'
        status += '</section>'
    versions = "".join(f'<li><a href="{edit_url}?edition={row["id"]}">{h(json.loads(row["plan"])["title"])}</a>'
                       f' · {h(row["qualification"])} · {h(row["render_state"])}</li>' for row in entries)

    def area(name, label, rows):
        return f'<label>{h(label)}<textarea name="{name}" rows="{rows}">{h(values.get(name,""))}</textarea></label>'

    controls = (field("title","Edition name",values.get("title",""),"required maxlength=90") +
                field("hook","On-screen hook",values.get("hook",""),"maxlength=55") +
                field("context","Game and version",values.get("context",""),"maxlength=45") +
                area("post","Post copy",2) + area("cuts","Shots · source in | source out | layout | crop keyframes | time cue",5) +
                f'<p class="meta">Shorts run {shorts_release.RULE}. Use wide, focus or reaction. '
                'Crop keyframes use source seconds,x,y. Use Earlier or Replay when returning to prior footage.</p>' +
                field("camera_crop","Camera override for this edit",values.get("camera_crop",camera_text),
                      'placeholder="Auto-detect · or enter x, y, width, height"') +
                field("game_crop","Gameplay bounds · normalized x, y, width, height",values.get("game_crop",game_text)) +
                field("hud_crop","Optional HUD rectangle · normalized x, y, width, height",values.get("hud_crop",hud_text)) +
                '<div class="actions"><button type="button" data-edit-command="in">Set in</button>'
                '<button type="button" data-edit-command="out">Set out</button>'
                '<button type="button" data-edit-command="split">Split at playhead</button>'
                '<button type="button" data-edit-command="audition">Audition source shot</button></div>'
                + area("captions","Caption overrides · source in | source out | phrase | emphasis",5)
                + '<p class="meta">An empty phrase suppresses the automatic cue over that range.</p>'
                + field("beats","Evidence times · premise, action, payoff",values.get("beats",""))
                + field("crossfade","Original-audio crossfade (seconds)",values.get("crossfade",".04"),'type=number min=0 max=.08 step=.001')
                + area("rationale","What the picture and audio show",3)
                + '<label>Editorial decision<select name="decision">' + "".join(
                    f'<option value="{value}"{" selected" if values.get("decision") == value else ""}>{label}</option>'
                    for value,label in (("candidate","Render candidate"),("qualify","Evidence qualifies"),
                                        ("reject","Reject · save evidence only"))) + '</select></label>'
                + button("Save version", "primary"))
    sync = ""
    if original_play and media_panel and plan and (proof := plan.get("beats",{}).get("payoff")):
        original_edition = root/"streamer-runs"/str(job)/candidate/"edition.json"
        if original_edition.is_file():
            first,last = streamer_asr.read(original_edition)["source_range"]
            if first <= proof["source"] < last:
                sync = (f'<button type="button" data-cue-original="{proof["source"]-first:.3f}" '
                        f'data-cue-version="{proof["edit"]:.3f}">Cue both players at the source payoff</button>')
    archive_url = prefix+"/archive"
    offset, initial = 0,max(0,original["start"]-8)
    picker = ""
    if ranged:
        windows = selection["asset"]["windows"]
        if windows:
            chosen = next((i for i,w in enumerate(windows) if w["start"]<=original["start"]<w["end"]),0)
            offset, initial = windows[chosen]["start"],windows[chosen]["start"]
            archive_url = f"{prefix}/selection-range/{chosen}.mp4"
            options = "".join(f'<option value="{prefix}/selection-range/{i}.mp4" data-start="{w["start"]}" '
                              f'data-end="{w["end"]}"{" selected" if i==chosen else ""}>'
                              f'{views.timecode(w["start"])}–{views.timecode(w["end"])}</option>'
                              for i,w in enumerate(windows))
            picker = f'<label>Acquired source window<select data-edit-window>{options}</select></label>'
        else:
            archive_url = ""
    preview = (f'{picker}<div class="editor-preview">'
               f'<div><video controls playsinline preload="none" data-edit-source src="{archive_url}" '
               f'data-start="{initial:.3f}" data-offset="{offset:.6f}" aria-label="Source footage"></video>'
               '<button type="button" data-edit-command="source">Go to source handles</button>'
               '<output aria-live="polite" data-edit-time></output></div>'
               '<div><canvas data-edit-crop width="270" height="480" aria-label="35/65 camera and gameplay preview"></canvas>'
               '<output data-edit-framing aria-live="polite"></output>'
               '<p class="meta">Shift-click the source picture to add a crop position at the playhead.</p></div></div>'
               if archive_url else '<p>Source footage has not been acquired for this attempt.</p>')
    return f"""<div class="hero"><div><h1>Portrait editor</h1><p>Run {job} · {h(candidate)} · {h(original.get('hook') or original.get('reason') or '')}</p></div>
    <div class="actions"><a href="/factory?attempt={job}#reel-{job}-{candidate}">Back to Factory reel</a><a href="{edit_url}">New draft from source</a></div></div>
    {f'<section class="panel edit-comparison">{original_play}{media_panel}</section>' if original_play or media_panel else ''}
    {sync}
    {status}<section class="panel"><h2>Saved versions</h2><ul>{versions}</ul></section>
    <section class="panel" data-source-editor>
      <h2>Source timeline</h2>{preview}
      {form(edit_url,token,controls)}
    </section>"""


def blind_page(db, root, token, job, candidate, identity):
    streamer_edits.comparison(root, db, job, candidate, identity)
    ballot = secrets.token_hex(16)
    action = f"/streamer/run/{job}/{candidate}/blind/{identity}"
    players = "".join(f'<div><h2>{slot.upper()}</h2><video class="phone-edit" playsinline '
                      f'preload="none" controls aria-label="Clip {slot.upper()}" '
                      f'src="{action}/{ballot}/{slot}"></video></div>' for slot in ("a","b"))
    fields = f'<input type="hidden" name="ballot" value="{ballot}">'
    for key, prompt in (("mechanic","What was the unusual move or mechanic?"),
                        ("picture","Where could you see it?"),
                        ("reason","Which version would you keep watching, and why?")):
        fields += f'<label>{prompt}<textarea name="{key}" maxlength="1200" required></textarea></label>'
    fields += ('<label>Verdict<select name="verdict" required><option value="">Choose a verdict</option>'
               '<option value="a">A</option><option value="b">B</option><option value="tie">Tie</option>'
               '<option value="both_weak">Both weak</option></select></label>' + button("Save ballot", "primary"))
    return (f'<h1>Blind comparison</h1><section class="panel edit-comparison">{players}</section>'
            f'<section class="panel">{form(action,token,fields)}</section>')
