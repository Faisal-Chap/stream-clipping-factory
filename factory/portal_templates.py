"""Local design revisions and pinned, renderer-readable style assignments."""

import hashlib
import json
import re
import secrets
from pathlib import Path

from . import views

PACK = Path(__file__).resolve().parents[1]/"docs"/"videoedit"
CATALOG = json.loads((PACK/"catalog.v1.json").read_text(encoding="utf-8"))
ORIGINALS = tuple(item["id"] for item in CATALOG["templates"])
FONTS = {entry["id"]:entry["file"] for entry in
         json.loads((PACK/"fonts"/"manifest.v1.json").read_text(encoding="utf-8"))["fonts"]}
MODES = {"inherit":"Use factory default","fixed":"Fixed template","random":"Random from selection",
         "even":"Even across selection"}
h,form,button = views.h,views.form,views.button


def seed(db):
    for entry in CATALOG["templates"]:
        if db.execute("SELECT 1 FROM template_designs WHERE id=?",(entry["id"],)).fetchone():
            continue
        recipe = json.loads((PACK/entry["recipe"]).read_text(encoding="utf-8"))
        db.execute("INSERT INTO template_designs(id,original,active_revision) VALUES(?,?,1)",(entry["id"],entry["id"]))
        db.execute("INSERT INTO template_revisions(template,revision,recipe) VALUES(?,1,?)",
                   (entry["id"],json.dumps(recipe)))


def revision(db, template, number=None):
    entry = db.execute("""SELECT d.*,r.revision,r.recipe,r.saved FROM template_designs d
                           JOIN template_revisions r ON r.template=d.id
                           WHERE d.id=? AND (? IS NULL OR r.revision=?) ORDER BY r.revision DESC LIMIT 1""",
                       (template,number,number)).fetchone()
    if not entry:
        raise ValueError("That template revision is unavailable.")
    return entry,json.loads(entry["recipe"])


def active(db):
    rows = db.execute("""SELECT d.id,r.recipe,r.revision FROM template_designs d JOIN template_revisions r
                          ON r.template=d.id AND r.revision=d.active_revision WHERE NOT d.archived ORDER BY d.rowid""")
    return {r["id"]:dict(template=r["id"],revision=r["revision"],name=json.loads(r["recipe"])["name"]) for r in rows}


def validate_policy(db,fields,*,inherit):
    mode = fields.get("mode","")
    if mode not in MODES or mode=="inherit" and not inherit:
        raise ValueError("Choose a template policy.")
    ids = list(dict.fromkeys(x.strip() for x in fields.get("ids","").split(",") if x.strip()))
    if mode=="inherit":
        return {"mode":mode,"ids":[]}
    if mode=="fixed" and len(ids)!=1:
        raise ValueError("A fixed policy requires one active design.")
    if set(ids)-active(db).keys():
        raise ValueError("A selected design has been archived or is still a draft. Update the style choices.")
    return {"mode":mode,"ids":ids}


def choices(db,policy):
    known = active(db)
    ids = policy.get("ids") or [item for item in ORIGINALS if item in known]
    if not ids or any(item not in known for item in ids):
        raise ValueError("Template selection needs repair. Choose active designs.")
    return [known[item] for item in ids]


def policy_summary(db,policy):
    try:
        selected = choices(db,policy)
    except ValueError:
        return "Style selection needs repair"
    count = len(selected)
    if policy["mode"]=="fixed":
        return f'8 {selected[0]["name"]}'
    if policy["mode"]=="random":
        return f'8 draws from {count} styles, repeats possible'
    base,extra = divmod(8,count)
    return f'Even across {count} · {base} of each' + (f' plus {extra} rotating slots' if extra else '')


def policy_fields(db, policy, *, inherit):
    options = "".join(f'<option value="{key}"{" selected" if policy["mode"]==key else ""}>{label}</option>'
                      for key,label in MODES.items() if inherit or key!="inherit")
    named = "".join(f'<label class="toggle"><input type="checkbox" data-template-choice value="{h(row["template"])}"'
                    f'{" checked" if row["template"] in policy["ids"] else ""}> {h(row["name"])}</label>'
                    for row in active(db).values())
    return (f'<label>Template policy<select name="mode">{options}</select></label>'
            f'<fieldset data-template-choices><legend>Template selection</legend>{named}'
            f'<input type="hidden" name="ids" value="{h(", ".join(policy["ids"]))}"></fieldset>'
            '<p class="meta">An empty Even or Random selection uses the active designs in the Original four pack.</p>')


def plan(db, policy, rotation):
    return {"pack":CATALOG["pack_id"],"pack_version":CATALOG["version"],"mode":policy["mode"],
            "revisions":choices(db,policy),"rotation":rotation,"seed":secrets.token_hex(16)}


def assign(db,job,story,planned):
    if not planned:
        return None
    saved = db.execute("SELECT * FROM template_assignments WHERE job=? AND story=?",(job,story)).fetchone()
    if saved:
        selected = next(item for item in planned["revisions"] if item["template"]==saved["template"])
        slot = saved["slot"]
    else:
        slot = db.execute("SELECT COUNT(*) FROM template_assignments WHERE job=?",(job,)).fetchone()[0]
        items = planned["revisions"]
        if planned["mode"]=="fixed":
            index = 0
        elif planned["mode"]=="random":
            index = int(hashlib.sha256(f'{planned["seed"]}:{story}'.encode()).hexdigest(),16)%len(items)
        else:
            index = (slot+planned["rotation"])%len(items)
        selected = items[index]
        db.execute("INSERT INTO template_assignments VALUES(?,?,?,?,?)",
                   (job,story,slot,selected["template"],selected["revision"]))
    return dict(selected,slot=slot,pack=planned["pack"],pack_version=planned["pack_version"],seed=planned["seed"])


def asset(kind,key):
    if kind=="font" and key in FONTS:
        return PACK/"fonts"/FONTS[key]
    if kind not in ("proof","reference") or key not in ORIGINALS:
        raise ValueError("Unknown template asset.")
    item = next(entry for entry in CATALOG["templates"] if entry["id"]==key)
    recipe = json.loads((PACK/item["recipe"]).read_text(encoding="utf-8"))
    return PACK/recipe[kind]


def draft_values(recipe):
    if "draft_style" in recipe:
        return recipe["draft_style"]
    text = next((v for v in recipe.values() if isinstance(v,dict) and "font_id" in v),{})
    timing = recipe.get("timing",{})
    hold = timing.get("hold_range_ms",[1350,3400])
    return dict(text="A moment from the stream",font=text.get("font_id","montserrat"),
                size=text.get("size_px",64),fill=text.get("fill",text.get("role_colors",{}).get("body","#FFFFFF")),
                backing=recipe.get("card",{}).get("fill","#141216"),x=540,y=825,
                entry=timing.get("entry_ms",0),hold=hold[1],exit=timing.get("exit_ms",90),
                layers=", ".join(recipe["layers_back_to_front"]))


def save(db,action,fields,template=None):
    if action in ("activate","archive"):
        row,_ = revision(db,template,int(fields["revision"]))
        if action=="activate":
            db.execute("UPDATE template_designs SET active_revision=?,archived=0 WHERE id=?",(row["revision"],template))
        else:
            db.execute("UPDATE template_designs SET archived=1 WHERE id=?",(template,))
        return template,row["revision"]
    if action in ("create","duplicate"):
        base = fields.get("base") or template or ORIGINALS[0]
        number = int(fields["revision"]) if fields.get("revision") else None
        parent, recipe = revision(db,base,number)
        template = "design-"+secrets.token_hex(6)
        number = 1
        name = fields.get("name","").strip() or f'{recipe["name"]} copy'
        if len(name)>90:
            raise ValueError("Use a design name of up to 90 characters.")
        recipe["name"] = name
        recipe["draft_style"] = draft_values(recipe)
        db.execute("INSERT INTO template_designs(id,original) VALUES(?,?)",(template,parent["original"]))
    else:
        prior,recipe = revision(db,template)
        if int(fields["version"])!=prior["revision"]:
            raise ValueError("Another revision was saved. Reopen the current draft before editing.")
        name,text,font = (fields[key].strip() for key in ("name","text","font"))
        if not name or len(name)>90 or not text or len(text)>95 or font not in FONTS:
            raise ValueError("Use a design name, sample text up to 95 characters and a packaged font.")
        if not asset("font",font).is_file() or not all(asset(kind,prior["original"]).is_file() for kind in ("proof","reference")):
            raise ValueError("A packaged reference or font is missing.")
        bounds = {"x":(64,1016),"y":(96,1670),"size":(24,160),"entry":(0,10000),
                  "hold":(200,15000),"exit":(0,3000)}
        numeric = {}
        for key,(low,high) in bounds.items():
            numeric[key] = int(fields[key])
            if not low<=numeric[key]<=high:
                raise ValueError(f"{key.title()} must be between {low} and {high}.")
        fills = {key:fields[key] for key in ("fill","backing")}
        if not all(re.fullmatch(r"#[0-9a-fA-F]{6}",value) for value in fills.values()):
            raise ValueError("Choose valid text and backing colors.")
        layers = [item.strip() for item in fields["layers"].split(",")]
        original_layers = recipe["layers_back_to_front"]
        if sorted(layers)!=sorted(original_layers) or layers[0]!="game":
            raise ValueError("Keep the recipe’s named layers, with gameplay behind the overlays.")
        recipe.update(name=name,draft_style=dict(text=text,font=font,layers=", ".join(layers),**numeric,**fills))
        number = prior["revision"]+1
    recipe["id"] = template
    if number>1 or template not in ORIGINALS:
        recipe["local_revision"] = number
        recipe["composition"] = CATALOG["composition"]
    db.execute("INSERT INTO template_revisions(template,revision,recipe) VALUES(?,?,?)",
               (template,number,json.dumps(recipe,ensure_ascii=False)))
    return template,number


def option(value,label,current):
    return f'<option value="{h(value)}"{" selected" if value==current else ""}>{h(label)}</option>'


def page(db,token,query):
    rows = list(db.execute("""SELECT d.*,r.recipe,r.revision FROM template_designs d JOIN template_revisions r
                              ON r.template=d.id WHERE r.revision=(SELECT MAX(a.revision) FROM template_revisions a
                              WHERE a.template=d.id) ORDER BY d.rowid"""))
    cards = []
    for row in rows:
        recipe = json.loads(row["recipe"])
        label = "Archived" if row["archived"] else "Active" if row["active_revision"]==row["revision"] else (
            f'Active revision {row["active_revision"]} · latest draft' if row["active_revision"] else "Draft")
        visual = (f'<img src="/templates/asset/proof/{row["original"]}" alt="{h(recipe["name"])} built-in proof" loading="lazy">'
                  if row["revision"]==1 and row["id"] in ORIGINALS else
                  f'<canvas width="270" height="480" data-template-preview="{h(json.dumps(draft_values(recipe)))}"></canvas>')
        cards.append(f'<article class="design-card">{visual}<h2>{h(recipe["name"])}</h2><p>{label} · '
                     f'Revision {row["revision"]}</p><a href="/templates?design={row["id"]}">Open design</a></article>')
    create = form("/templates/create",token,'<label>Design name<input name="name" required maxlength="90"></label>'
                  '<label>Base layout<select name="base">'+"".join(option(item,item,None) for item in ORIGINALS)+
                  '</select></label>'+button("Create draft","primary"))
    overview = (f'<div class="hero"><div><h1>Edit templates</h1><p>{len(rows)} templates · Original four pack included</p></div></div>'
                f'<details class="panel operator-panel"><summary>Create template</summary>{create}</details>')
    if not query.get("design"):
        return overview+f'<div class="design-grid">{"".join(cards)}</div>'
    row,recipe = revision(db,query["design"],int(query["revision"]) if query.get("revision") else None)
    historical = row["revision"] != revision(db,row["id"])[0]["revision"]
    path = "/templates/"+row["id"]
    versions = "".join(f'<a href="/templates?design={row["id"]}&amp;revision={n[0]}">Revision {n[0]}</a> · '
                        for n in db.execute("SELECT revision FROM template_revisions WHERE template=? ORDER BY revision DESC",(row["id"],)))
    fields = draft_values(recipe)
    inputs = f'<input type="hidden" name="version" value="{row["revision"]}">'
    inputs += f'<label>Design name<input name="name" required maxlength="90" value="{h(recipe["name"])}"></label>'
    inputs += f'<label>Sample headline<input name="text" required maxlength="95" value="{h(fields["text"])}"></label>'
    inputs += '<label>Font<select name="font">'+"".join(option(k,k,fields["font"]) for k in FONTS)+'</select></label>'
    for key,label in (("size","Headline size (px)"),("x","Headline X (px)"),("y","Headline Y (px)"),("entry","Entry (ms)"),
                      ("hold","Hold (ms)"),("exit","Exit (ms)"),("fill","Text color"),("backing","Backing color"),("layers","Layer order")):
        kind = "color" if key in ("fill","backing") else "text" if key=="layers" else "number"
        inputs += f'<label>{label}<input name="{key}" type="{kind}" value="{h(fields[key])}" required></label>'
    edit = form(path+"/save",token,inputs+button("Save revision","primary"),"template-fields") if not historical else ""
    flags = f'<input type="hidden" name="revision" value="{row["revision"]}">'
    actions = (form(path+"/activate",token,flags+button("Activate for new runs"))+
               form(path+"/archive",token,flags+button("Archive for future selection"))+
               form(path+"/duplicate",token,flags+button("Duplicate as draft")))
    return (overview+f'<p><a href="/templates">All templates</a> · <a href="/sources?style={row["id"]}">Creators using this design</a></p>'
            f'<section class="panel operator-panel template-designer"><div><h2>{h(recipe["name"])} · Revision {row["revision"]}</h2>'
            f'<p>{versions}</p><p>Planned style · moving-video integration pending</p>'
            f'<div class="workspace-actions">{actions}</div>{edit}'
            f'<details><summary>Saved recipe and original reference</summary><pre>{h(json.dumps(recipe,indent=2,ensure_ascii=False))}</pre>'
            f'<a href="/templates/asset/reference/{row["original"]}">Original reference</a></details></div>'
            f'<div><h3>Current still preview</h3><canvas width="270" height="480" data-template-preview="{h(json.dumps(fields))}"></canvas>'
            '<p>Sample panels show text, color, font and position. Timing and layer effects await video integration.</p>'
            f'<details><summary>Built-in proof</summary><img class="design-proof" alt="Built-in proof" '
            f'src="/templates/asset/proof/{row["original"]}"></details></div></section>')
